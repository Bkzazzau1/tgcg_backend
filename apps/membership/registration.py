"""Member registration: self-registration with a PVC, coordinator-assisted
registration, and setting a member's sign-in PIN.

Every step validates all input first and only then burns the SMS code, so a
typo in (say) the VIN does not cost the member their code. Views calling
these functions must be `transaction.non_atomic_requests` (see
apps.accounts.verification.consume_code).
"""

import re
import uuid

from django.conf import settings
from django.contrib.auth.hashers import identify_hasher, is_password_usable
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone
from PIL import Image, UnidentifiedImageError

from apps.access import audit, engine
from apps.accounts.models import User
from apps.accounts.verification import PhoneVerification, consume_code, require_phone, send_code
from apps.core.choices import (
    GeographyLevel,
    MembershipState,
    RegistrationChannel,
    Role,
    VerificationStatus,
)
from apps.core.models import Sequence
from apps.core.permissions import Capability
from apps.geography.models import PollingUnit
from apps.geography.scope import SCOPE_FIELDS, scope_contains_any

from . import identity
from .models import Member

PURPOSE = PhoneVerification.Purpose
_DELIM = re.compile(
    r"^\s*(\d{1,2})\s*[/\-. ]\s*(\d{1,2})\s*[/\-. ]\s*(\d{1,2})\s*[/\-. ]\s*(\d{1,3})\s*$"
)
_WEAK_PINS = {"123456", "654321", "123123", "112233", "121212", "696969", "159753"}
_IMAGE_FORMATS = {"JPEG", "PNG", "WEBP"}
# Assisted registrations without a PVC must still be placed at least this precisely.
NO_PVC_LEVELS = (GeographyLevel.LGA, GeographyLevel.WARD, GeographyLevel.POLLING_UNIT)


# --- validation helpers ------------------------------------------------------


def polling_unit_from_delim(delim: str) -> PollingUnit:
    """Resolve the polling unit code on a PVC (marked CODE, older cards DELIM;
    e.g. 18-23-01-001) to a polling unit."""
    match = _DELIM.match(delim or "")
    if not match:
        raise ValidationError(
            {"delim": "Enter the polling unit code from the PVC (marked CODE), e.g. 18-23-01-001."}
        )
    state, lga, ward, unit = match.groups()
    code = f"{int(state):02d}-{int(lga):02d}-{int(ward):02d}-{int(unit):03d}"
    polling_unit = (
        PollingUnit.objects.select_related("ward__lga__state__zone").filter(code=code).first()
    )
    if polling_unit is None:
        raise ValidationError({"delim": f"Polling unit {code} is not in the INEC register."})
    return polling_unit


def _registration_unit(delim, chosen) -> tuple[PollingUnit, bool]:
    """The unit from the PVC's code or, failing that, one found by name in the
    lists; True when it came from the lists."""
    if (delim or "").strip():
        return polling_unit_from_delim(delim), False
    chosen = (chosen or "").strip()
    if not chosen:
        raise ValidationError(
            {"delim": "Enter the polling unit code from your PVC, or find your polling unit."}
        )
    unit = PollingUnit.objects.select_related("ward__lga__state__zone").filter(code=chosen).first()
    if unit is None:
        raise ValidationError({"polling_unit": "That polling unit was not found. Find it again."})
    return unit, True


def validate_pin(pin: str) -> str:
    pin = (pin or "").strip()
    if not re.fullmatch(r"\d{6}", pin):
        raise ValidationError({"pin": "The PIN must be exactly 6 digits."})
    digits = [int(d) for d in pin]
    steps = {b - a for a, b in zip(digits, digits[1:], strict=False)}
    if len(set(pin)) == 1 or steps in ({1}, {-1}) or pin in _WEAK_PINS:
        raise ValidationError({"pin": "This PIN is too easy to guess. Choose another."})
    return pin


def validate_image(upload, field: str, *, required: bool):
    if not upload:
        if required:
            raise ValidationError({field: "A photo is required."})
        return
    limit = settings.MEMBER_PHOTO_MAX_MB * 1024 * 1024
    if upload.size > limit:
        raise ValidationError(
            {field: f"The image must be under {settings.MEMBER_PHOTO_MAX_MB} MB."}
        )
    try:
        image = Image.open(upload)
        image.verify()
        image_format = image.format
    except (UnidentifiedImageError, OSError, SyntaxError) as exc:
        raise ValidationError({field: "Upload a JPEG, PNG or WEBP photo."}) from exc
    finally:
        upload.seek(0)
    if image_format not in _IMAGE_FORMATS:
        raise ValidationError({field: "Upload a JPEG, PNG or WEBP photo."})


def ensure_available(phone: str, vin: str | None = None) -> None:
    """Reject a phone or VIN that already belongs to a member or an account."""
    if Member.objects.filter(phone_number=phone).exists():
        raise ValidationError({"phone_number": "This phone number is already registered."})
    if User.objects.filter(phone_number=phone).exists():
        raise ValidationError(
            {"phone_number": "This phone number belongs to a staff account. Use another number."}
        )
    if vin and Member.objects.filter(vin=vin).exists():
        raise ValidationError({"vin": "This PVC is already registered to a member."})


def _clean_vin(vin):
    vin = "".join((vin or "").split()).upper()
    return vin or None


def _require_consent(consent):
    if consent is not True:
        raise ValidationError(
            {"consent": "The member must agree to how TGCG uses their personal data."}
        )


def _membership_number() -> str:
    return f"TGCG-{Sequence.next_value('member'):06d}"


def _save_member(member: Member) -> Member:
    member.membership_number = _membership_number()
    member.full_clean()
    try:
        with transaction.atomic():
            member.save()
    except IntegrityError as exc:
        raise ValidationError("This phone number or PVC was registered a moment ago.") from exc
    return member


def _member_account(member: Member, pin: str) -> User:
    user = User(
        access_id=member.membership_number,
        full_name=member.full_name,
        phone_number=member.phone_number,
        email=member.email,
        role=Role.MEMBER,
        scope_level=member.scope_level,
        **{field: getattr(member, field) for field in SCOPE_FIELDS},
    )
    user.set_password(pin)  # PIN rules are checked by validate_pin, not password validators
    user.full_clean(exclude=["password"])
    user.save()
    member.user = user
    member.save(update_fields=["user", "updated_at"])
    return user


# --- self-registration (app) ----------------------------------------------------


def request_registration_code(*, phone_number: str, vin: str | None = None) -> int:
    phone = require_phone(phone_number)
    ensure_available(phone, _clean_vin(vin))
    return send_code(phone, PURPOSE.REGISTER)


def self_register(
    *,
    phone_number,
    vin,
    full_name,
    pin,
    consent,
    delim="",
    polling_unit="",
    code="",
    gender="",
    date_of_birth=None,
    occupation="",
    email="",
    photo=None,
    pvc_image=None,
    disabilities=(),
    disability_other="",
):
    """Register a member in the app. Returns (member, user).

    As on USSD, joining needs the phone, the PVC's VIN and polling unit code,
    plus the name and PIN of the app account; a selfie, a card photo and the
    other details are optional. Someone who cannot read the code may give a
    `polling_unit` found by name instead; that is recorded, because it may not
    be where INEC registered them. A given SMS code is checked and confirms
    the phone. Without SMS (REGISTRATION_PHONE_CODE_REQUIRED off) the member
    joins with no code and the phone stays unconfirmed, as its check records.
    """
    _require_consent(consent)
    phone = require_phone(phone_number)
    vin = _clean_vin(vin)
    if not vin:
        raise ValidationError({"vin": "Enter the VIN from your PVC."})
    code = (code or "").strip()
    if not code and settings.REGISTRATION_PHONE_CODE_REQUIRED:
        raise ValidationError({"code": "Enter the 6-digit code sent to your phone."})
    pin = validate_pin(pin)
    unit, from_list = _registration_unit(delim, polling_unit)
    validate_image(photo, "photo", required=False)
    validate_image(pvc_image, "pvc_image", required=False)
    ensure_available(phone, vin)

    member = Member(
        full_name=full_name,
        phone_number=phone,
        email=email or "",
        vin=vin,
        gender=gender or "",
        date_of_birth=date_of_birth,
        occupation=occupation or "",
        disabilities=list(disabilities or []),
        disability_other=disability_other or "",
        photo=photo,
        pvc_image=pvc_image,
        registration_channel=RegistrationChannel.SELF,
        scope_level=GeographyLevel.POLLING_UNIT,
        polling_unit=unit,
        polling_unit_from_list=from_list,
    )
    member.full_clean(exclude=["membership_number"])

    if code:
        consume_code(phone, PURPOSE.REGISTER, code)
    with transaction.atomic():
        now = timezone.now()
        if code:
            member.phone_verified_at = now
        member.consent_at = now
        _save_member(member)
        user = _member_account(member, pin)
        member = identity.record_checks(member)
    return member, user


# --- coordinator-assisted registration ------------------------------------------


def assisted_register(
    *,
    actor,
    phone_number,
    code,
    full_name,
    gender,
    consent,
    photo,
    vin=None,
    delim=None,
    scope=None,
    date_of_birth=None,
    occupation="",
    email="",
    pvc_image=None,
    client_id=None,
    disabilities=(),
    disability_other="",
) -> Member:
    """A coordinator registers someone in person. The PVC is optional."""
    phone = require_phone(phone_number)
    if client_id:
        existing = Member.objects.filter(client_id=client_id, created_by=actor).first()
        if existing is not None:
            return existing  # offline retry
    _require_consent(consent)
    vin = _clean_vin(vin)
    validate_image(photo, "photo", required=True)
    validate_image(pvc_image, "pvc_image", required=False)

    if vin:
        location = {
            "scope_level": GeographyLevel.POLLING_UNIT,
            "polling_unit": polling_unit_from_delim(delim),
        }
    else:
        location = scope or {}
        if location.get("scope_level") not in NO_PVC_LEVELS:
            raise ValidationError(
                {"scope": "Without a PVC, place the member at their LGA, ward or polling unit."}
            )
    ensure_available(phone, vin)

    member = Member(
        full_name=full_name,
        phone_number=phone,
        email=email or "",
        vin=vin,
        gender=gender,
        date_of_birth=date_of_birth,
        occupation=occupation or "",
        disabilities=list(disabilities or []),
        disability_other=disability_other or "",
        photo=photo,
        pvc_image=pvc_image or "",
        registration_channel=RegistrationChannel.ASSISTED,
        client_id=client_id,
        created_by=actor,
        **location,
    )
    member.full_clean(exclude=["membership_number"])
    if not scope_contains_any(
        actor, member, (Capability.REGISTER_MEMBERS, Capability.MANAGE_MEMBERSHIP)
    ):
        raise PermissionDenied("The member's location is outside your operational scope.")

    consume_code(phone, PURPOSE.REGISTER, code)
    with transaction.atomic():
        now = timezone.now()
        member.phone_verified_at = now
        member.consent_at = now
        _save_member(member)
        member = identity.record_checks(member)
    return member


# --- self-registration by USSD ---------------------------------------------------


def ussd_register(
    *,
    phone_number,
    session_id,
    full_name,
    polling_unit,
    gender,
    pin,
    date_of_birth=None,
    vin=None,
) -> Member:
    """Register a member who dialled the USSD code (see apps.membership.ussd).

    The mobile network supplied the phone number, so it needs no SMS code.
    There is no photo of the member or the card, so the member waits for a
    coordinator's review.
    """
    phone = require_phone(phone_number)
    client_id = f"ussd:{session_id}"[:64]
    existing = Member.objects.filter(client_id=client_id, phone_number=phone).first()
    if existing is not None:
        return existing  # the provider sent the last step again
    pin = validate_pin(pin)
    vin = _clean_vin(vin)
    ensure_available(phone, vin)

    member = Member(
        full_name=full_name,
        phone_number=phone,
        vin=vin,
        gender=gender,
        date_of_birth=date_of_birth,
        registration_channel=RegistrationChannel.USSD,
        client_id=client_id,
        scope_level=GeographyLevel.POLLING_UNIT,
        polling_unit=polling_unit,
    )
    member.full_clean(exclude=["membership_number"])
    with transaction.atomic():
        now = timezone.now()
        member.phone_verified_at = now
        member.consent_at = now
        _save_member(member)
        _member_account(member, pin)
        member = identity.record_checks(member)
    return member


# --- minimal USSD membership and later app account -----------------------------


@transaction.atomic
def ussd_register_minimal(*, phone_number, draft_id, polling_unit, vin) -> Member:
    """Join after the USSD confirmation and data-use agreement.

    The trusted callback caller obtains consent before invoking this service.
    A name and app account can be added later; no credentials are invented.
    A saved draft UUID identifies completion retries across provider sessions.
    """
    phone = require_phone(phone_number)
    try:
        client_id = f"ussd:{uuid.UUID(str(draft_id))}"
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValidationError("Registration progress could not be found. Dial again.") from exc
    if existing := Member.objects.select_for_update().filter(phone_number=phone).first():
        return existing
    vin = _clean_vin(vin)
    if not vin:
        raise ValidationError({"vin": "Enter the VIN on your PVC to join."})
    unit = (
        PollingUnit.objects.select_related("ward__lga__state__zone")
        .filter(pk=getattr(polling_unit, "pk", None))
        .first()
    )
    if unit is None:
        raise ValidationError({"polling_unit": "Enter a polling unit in the INEC register."})
    member = Member(
        full_name="",
        phone_number=phone,
        vin=vin,
        registration_channel=RegistrationChannel.USSD,
        client_id=client_id,
        scope_level=GeographyLevel.POLLING_UNIT,
        polling_unit=unit,
    )
    try:
        # The savepoint rolls back all registration effects on a uniqueness race.
        with transaction.atomic():
            ensure_available(phone, vin)
            now = timezone.now()
            member.phone_verified_at = now
            member.consent_at = now
            _save_member(member)
            return identity.record_checks(member)
    except (IntegrityError, ValidationError):
        if existing := Member.objects.select_for_update().filter(phone_number=phone).first():
            return existing
        raise


def _validate_pin_hash(pin_hash: str) -> None:
    """Accept a recognized Django hash, never a raw PIN or unusable password."""
    try:
        if not isinstance(pin_hash, str) or len(pin_hash) > 128 or not pin_hash:
            raise ValueError
        if not is_password_usable(pin_hash):
            raise ValueError
        decoded = identify_hasher(pin_hash).decode(pin_hash)
        if not decoded.get("hash", decoded.get("checksum")):
            raise ValueError
        if "salt" in decoded and not decoded["salt"]:
            raise ValueError
        if "iterations" in decoded and decoded["iterations"] < 1:
            raise ValueError
    except (ValueError, TypeError, AssertionError, KeyError) as exc:
        raise ValidationError({"pin": "Choose and confirm a new app PIN."}) from exc


@transaction.atomic
def ussd_complete_profile(*, member: Member, full_name, pin_hash) -> Member:
    """Give an existing USSD member an app account, without changing credentials.

    The caller validates the chosen PIN and confirms it against this hash.
    Completion retries are read-only once the member has an account.
    """
    # Lock only the member row; its optional user relation would add an OUTER
    # JOIN that PostgreSQL cannot lock on the nullable side.
    member = Member.objects.select_for_update().get(pk=member.pk)
    if member.registration_channel != RegistrationChannel.USSD:
        raise ValidationError("This membership's app account must be set up in the app.")
    if member.user_id:
        return member
    if member.membership_state == MembershipState.DEACTIVATED:
        raise ValidationError("This membership has ended.")
    name = " ".join((full_name or "").split())
    words = [word for word in name.split() if sum(letter.isalpha() for letter in word) >= 2]
    if (
        len(name) > 150
        or len(words) < 2
        or any(not (letter.isalpha() or letter in " .'-") for letter in name)
    ):
        raise ValidationError({"full_name": "Enter at least two names, in letters."})
    if member.full_name and member.full_name.casefold() != name.casefold():
        raise ValidationError({"full_name": "A name is already recorded. Contact a coordinator."})
    _validate_pin_hash(pin_hash)
    if User.objects.filter(phone_number=member.phone_number).exists():
        raise ValidationError("This number already has an account. Contact support.")
    name_added = not member.full_name
    back_to_review = name_added and member.verification_status == VerificationStatus.VERIFIED
    if name_added:
        member.full_name = name
    if back_to_review:
        member.verification_status = VerificationStatus.PENDING_REVIEW
        member.verified_at = None
        member.verified_by = None
    member.full_clean()
    user = User(
        access_id=member.membership_number,
        full_name=member.full_name,
        phone_number=member.phone_number,
        email=member.email,
        role=Role.MEMBER,
        scope_level=member.scope_level,
        password=pin_hash,
        **{field: getattr(member, field) for field in SCOPE_FIELDS},
    )
    user.full_clean(exclude=["password"])
    try:
        with transaction.atomic():
            user.save()
            member.user = user
            member.save()
    except IntegrityError as exc:
        raise ValidationError("This number already has an account. Contact support.") from exc
    member = identity.record_checks(member)
    audit.record(
        actor=user,
        action="member.ussdProfileCompleted",
        target_type="member",
        target_id=member.membership_number,
        target_label=member.full_name,
        changes={
            "fields": ["full_name", "app_account"] if name_added else ["app_account"],
            "back_to_review": back_to_review,
        },
    )
    if back_to_review:
        engine.clear_cache()
    return member


# --- members from a chapter's own list ------------------------------------------


@transaction.atomic
def register_imported(
    *,
    actor,
    full_name: str,
    phone_number: str,
    password: str,
    source: str,
    polling_unit=None,
    ward=None,
    lga=None,
    state=None,
    vin=None,
) -> Member:
    """A member from a chapter's own list, imported by a National Administrator.

    The chapter checked these people itself and holds their agreement to TGCG
    keeping their details, so both are recorded on its word: the identity as
    verified by `actor`, consent as of now, with `source` naming the list. The
    phone is not confirmed by SMS, and the system's own checks record what
    they can, as for anyone. The first password is the phone number as
    written on the list; the member changes it later. The member is placed at
    their polling unit, or at the ward, LGA or state when the list gives no
    more, with the VIN of their voter's card when the list has a valid one.
    """
    if polling_unit is not None:
        place = {"scope_level": GeographyLevel.POLLING_UNIT, "polling_unit": polling_unit}
    elif ward is not None:
        place = {"scope_level": GeographyLevel.WARD, "ward": ward}
    elif lga is not None:
        place = {"scope_level": GeographyLevel.LGA, "lga": lga}
    elif state is not None:
        place = {"scope_level": GeographyLevel.STATE, "state": state}
    else:
        raise ValueError("An imported member needs a polling unit, ward, LGA or state.")
    phone = require_phone(phone_number)
    vin = _clean_vin(vin)
    ensure_available(phone, vin)
    now = timezone.now()
    member = Member(
        full_name=full_name,
        phone_number=phone,
        vin=vin,
        registration_channel=RegistrationChannel.IMPORT,
        consent_at=now,
        **place,
    )
    member.full_clean(exclude=["membership_number"])
    _save_member(member)
    _member_account(member, password)
    member.verification_status = VerificationStatus.VERIFIED
    member.verified_at = now
    member.verified_by = actor
    member.save(update_fields=["verification_status", "verified_at", "verified_by", "updated_at"])
    member = identity.record_checks(member, actor=actor)
    audit.record(
        actor=actor,
        action="member.imported",
        target_type="member",
        target_id=member.membership_number,
        target_label=member.full_name,
        reason=source,
        changes={"identity": "checked manually by the chapter", "consent": "through the chapter"},
    )
    return member


# --- member PIN (first sign-in for assisted members, or forgotten PIN) ---------


def request_pin_code(*, phone_number: str) -> int:
    phone = require_phone(phone_number)
    if not Member.objects.filter(phone_number=phone).exists():
        # Same response either way, so the endpoint cannot list members.
        return settings.OTP_TTL_MINUTES * 60
    return send_code(phone, PURPOSE.SET_PIN)


def set_pin(*, phone_number, code, pin):
    """Create or reset a member's PIN. Returns (member, user)."""
    phone = require_phone(phone_number)
    pin = validate_pin(pin)
    member = Member.objects.select_related("user").filter(phone_number=phone).first()
    if member is None:
        raise ValidationError({"code": "The code is wrong or has expired. Request a new one."})
    if member.membership_state == MembershipState.DEACTIVATED:
        raise ValidationError({"phone_number": "This membership has ended."})
    if member.user is None and User.objects.filter(phone_number=phone).exists():
        raise ValidationError(
            {"phone_number": "This phone number belongs to a staff account. Contact support."}
        )
    if member.user is None and not member.full_name:
        raise ValidationError(
            {"full_name": "Dial TGCG USSD again to add your name and app PIN first."}
        )

    consume_code(phone, PURPOSE.SET_PIN, code)
    with transaction.atomic():
        if member.user is None:
            user = _member_account(member, pin)
        else:
            user = member.user
            user.set_password(pin)
            user.failed_login_attempts = 0
            user.locked_until = None
            user.save(update_fields=["password", "failed_login_attempts", "locked_until"])
        if member.phone_verified_at is None:
            member.phone_verified_at = timezone.now()
            member.save(update_fields=["phone_verified_at", "updated_at"])
    return member, user

"""Member registration: self-registration with a PVC, coordinator-assisted
registration, and setting a member's sign-in PIN.

Every step validates all input first and only then burns the SMS code, so a
typo in (say) the VIN does not cost the member their code. Views calling
these functions must be `transaction.non_atomic_requests` (see
apps.accounts.verification.consume_code).
"""

import re

from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone
from PIL import Image, UnidentifiedImageError

from apps.accounts.models import User
from apps.accounts.verification import PhoneVerification, consume_code, require_phone, send_code
from apps.core.choices import GeographyLevel, MembershipState, RegistrationChannel, Role
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
    """Resolve the PVC DELIM code (e.g. 18/23/01/001) to a polling unit."""
    match = _DELIM.match(delim or "")
    if not match:
        raise ValidationError({"delim": "Enter the DELIM code from the PVC, e.g. 18/23/01/001."})
    state, lga, ward, unit = match.groups()
    code = f"{int(state):02d}-{int(lga):02d}-{int(ward):02d}-{int(unit):03d}"
    polling_unit = (
        PollingUnit.objects.select_related("ward__lga__state__zone").filter(code=code).first()
    )
    if polling_unit is None:
        raise ValidationError({"delim": f"Polling unit {code} is not in the INEC register."})
    return polling_unit


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
    code,
    vin,
    delim,
    full_name,
    gender,
    date_of_birth=None,
    occupation="",
    email="",
    pin,
    consent,
    photo,
    pvc_image,
):
    """Register a member from their scanned PVC. Returns (member, user)."""
    _require_consent(consent)
    phone = require_phone(phone_number)
    vin = _clean_vin(vin)
    if not vin:
        raise ValidationError({"vin": "Scan your PVC to register."})
    pin = validate_pin(pin)
    polling_unit = polling_unit_from_delim(delim)
    validate_image(photo, "photo", required=True)
    validate_image(pvc_image, "pvc_image", required=True)
    ensure_available(phone, vin)

    member = Member(
        full_name=full_name,
        phone_number=phone,
        email=email or "",
        vin=vin,
        gender=gender,
        date_of_birth=date_of_birth,
        occupation=occupation or "",
        photo=photo,
        pvc_image=pvc_image,
        registration_channel=RegistrationChannel.SELF,
        scope_level=GeographyLevel.POLLING_UNIT,
        polling_unit=polling_unit,
    )
    member.full_clean(exclude=["membership_number"])

    consume_code(phone, PURPOSE.REGISTER, code)
    with transaction.atomic():
        now = timezone.now()
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

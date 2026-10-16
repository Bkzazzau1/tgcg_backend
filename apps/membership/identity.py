"""Identity verification: record what was actually checked; people review the rest.

A member is verified automatically when their profile is complete and rests
on their voter's card: the PVC details were captured from a photo of the
card, and the server itself found the name and VIN given on that photo; a
selfie was taken; the gender and date of birth are given; and nobody else has
the same name and birth date. That is TGCG's policy (decided on 16 October
2026), not proof of identity. There is still no authoritative INEC source to
validate a PVC against, no liveness detection and no trusted reference photo,
so those checks stay recorded as unsupported, and every other member goes to
a coordinator's review. Nothing is recorded as passed unless it was
performed: reading a PVC is not validating it, and a selfie alone is not
biometric verification.
"""

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.crypto import salted_hmac

from apps.access import audit, engine
from apps.access.models import (
    AssignmentStatus,
    DelegationGrantArea,
    GrantStatus,
    RoleAssignmentArea,
)
from apps.core.choices import RegistrationChannel, Role, VerificationStatus
from apps.core.permissions import Capability

from . import card_match, pvc_reading
from .models import Member, VerificationCheck

C = Capability
K = VerificationCheck.Kind
R = VerificationCheck.Result
V = VerificationStatus

# The checks automatic verification rests on, with a gender and date of birth given.
AUTOMATIC = (K.PVC_CAPTURED, K.CARD_MATCH, K.SELFIE, K.DUPLICATES)
# Named in the audit trail, so each automatic decision says which rule made it.
AUTOMATIC_POLICY = "cardSelfieNoDuplicate"

NOT_AVAILABLE = {
    K.PVC_VALIDATED: (
        "No authoritative INEC source is connected; only the VIN's format and the polling "
        "unit code were checked."
    ),
    K.LIVENESS: "Liveness detection is not available.",
    K.FACE_MATCH: "No trusted reference photo is available to compare the selfie with.",
}


def _record(member, kind, result, detail, basis=""):
    VerificationCheck.objects.update_or_create(
        member=member,
        kind=kind,
        defaults={
            "result": result,
            "detail": detail[:300],
            "basis": basis,
            "checked_at": timezone.now(),
            "checked_by": None,  # the system performed it
        },
    )


def possible_duplicates(member) -> int:
    """How many others have the same name and date of birth.

    Phone numbers and VINs are unique already; this catches the same person
    registered again with a different phone and no PVC.
    """
    return (
        Member.objects.filter(
            date_of_birth=member.date_of_birth, full_name__iexact=member.full_name
        )
        .exclude(pk=member.pk)
        .count()
    )


def _audit(actor, action, member, reason="", changes=None):
    audit.record(
        actor=actor,
        action=action,
        target_type="member",
        target_id=member.membership_number,
        target_label=member.full_name,
        changes=changes,
        reason=reason,
    )


def assess(member) -> dict[str, tuple[str, str]]:
    """Every check but the card comparison, as things stand: {kind: (result,
    detail)}. Nothing is recorded and no card is read."""
    found = {}
    if member.vin and member.polling_unit_id and member.polling_unit_from_list:
        detail = "VIN entered; the polling unit was chosen from the list, not the code on the PVC."
        found[K.PVC_CAPTURED] = R.PENDING, detail
    elif member.vin and member.polling_unit_id and member.pvc_image:
        found[K.PVC_CAPTURED] = R.PASSED, "VIN and polling unit code read from the card."
    elif member.vin and member.polling_unit_id:
        detail = "VIN and polling unit code entered without a photo of the card."
        found[K.PVC_CAPTURED] = R.PENDING, detail
    elif member.vin:
        found[K.PVC_CAPTURED] = R.PENDING, "VIN recorded without a polling unit code."
    elif member.registration_channel == RegistrationChannel.USSD:
        found[K.PVC_CAPTURED] = R.FAILED, "Polling unit code given by USSD, without the VIN."
    else:
        found[K.PVC_CAPTURED] = R.FAILED, "Registered without a PVC."

    if not member.date_of_birth:
        detail = "No date of birth, so others with the same name could not be compared."
        found[K.DUPLICATES] = R.PENDING, detail
    elif duplicates := possible_duplicates(member):
        detail = f"{duplicates} other member(s) have the same name and date of birth."
        found[K.DUPLICATES] = R.PENDING, detail
    else:
        detail = "Phone number and VIN are unique; nobody else has the same name and birth date."
        found[K.DUPLICATES] = R.PASSED, detail

    if member.phone_verified_at and member.registration_channel == RegistrationChannel.USSD:
        detail = "The mobile network supplied the number when the member dialled the USSD code."
        found[K.PHONE] = R.PASSED, detail
    elif member.phone_verified_at:
        found[K.PHONE] = R.PASSED, "A code sent by SMS was entered."
    else:
        found[K.PHONE] = R.PENDING, "The phone number has not been confirmed by SMS."

    if member.photo:
        detail = "A selfie was captured. This alone is not biometric verification."
        found[K.SELFIE] = R.PASSED, detail
    else:
        found[K.SELFIE] = R.FAILED, "No selfie was captured."

    for kind, detail in NOT_AVAILABLE.items():
        found[kind] = R.UNSUPPORTED, detail
    return found


def read_card(member) -> str:
    """The text on the member's stored card photo, read by this server's
    reader (pvc_reading: Google Cloud Vision when configured)."""
    with member.pvc_image.open("rb") as photo:
        return pvc_reading.read_text(photo)


def card_basis(member) -> str:
    """A keyed fingerprint of what a card comparison rests on: the name, the
    VIN and the card photo. It shows when any of them changed, but reveals
    none of them, even to someone holding the database."""
    value = "\n".join(
        (" ".join(card_match.name_words(member.full_name)), member.vin, member.pvc_image.name)
    )
    return salted_hmac("membership.cardMatch", value, algorithm="sha256").hexdigest()


def card_compared(member) -> bool:
    """Whether a comparison of the card covers the name, VIN and photo as they are."""
    if not (member.pvc_image and member.vin):
        return False
    stored = member.verification_checks.filter(kind=K.CARD_MATCH).first()
    return stored is not None and stored.basis == card_basis(member)


def _compare_card(member, read: bool) -> tuple[str, str, str] | None:
    """The card comparison as (result, detail, basis), or None to keep the one
    on record, which still covers the details. The card is read only when
    `read` and no comparison covers them; a reading that fails for now keeps
    no basis, so the card is read again next time."""
    if not (member.pvc_image and member.vin):
        return R.PENDING, "No card photo to compare the name and VIN with.", ""
    if card_compared(member):
        return None
    if not read:
        return R.PENDING, "The card photo has not been compared with the name and VIN.", ""
    basis = card_basis(member)
    try:
        text = read_card(member)
    except (pvc_reading.ReaderUnavailable, pvc_reading.ReadFailed):
        return R.PENDING, "The card photo could not be read this time.", ""
    except OSError:
        return R.PENDING, "The card photo could not be opened.", basis
    matched, detail = card_match.compare(text, member.full_name, member.vin)
    return (R.PASSED if matched else R.PENDING), detail, basis


def meets_policy(member, results: dict[str, str]) -> bool:
    """Whether the automatic policy verifies the member, given every check's result."""
    return bool(member.gender and member.date_of_birth) and all(
        results.get(kind) == R.PASSED for kind in AUTOMATIC
    )


def _run_checks(member, read: bool = False) -> dict[str, str]:
    """Record the result of every check, as it stands now."""
    for kind, (result, detail) in assess(member).items():
        _record(member, kind, result, detail)
    if card := _compare_card(member, read):
        _record(member, K.CARD_MATCH, *card)
    return dict(member.verification_checks.values_list("kind", "result"))


@transaction.atomic
def record_checks(member: Member, actor=None) -> Member:
    """Run the checks the system can perform and set the verification status.

    A decision already made (verified, by a coordinator or automatically, or
    rejected) is kept. Otherwise the member is verified automatically when
    the policy allows (see above), or else awaits a coordinator's review. For
    a member awaiting a decision, the card photo is read when no comparison
    covers their name, VIN and photo as they are now.
    `actor`: whoever asked for the checks to run (None: registration or the system).
    """
    member = Member.objects.select_for_update().get(pk=member.pk)
    before = member.verification_status
    undecided = before not in (V.VERIFIED, V.REJECTED)
    results = _run_checks(member, read=undecided)
    automatic = undecided and meets_policy(member, results)
    if undecided:
        if automatic:
            member.verification_status = V.VERIFIED
            member.verified_at = timezone.now()
            member.verified_by = None  # the policy decided, not a person
        else:
            member.verification_status = V.PENDING_REVIEW
        member.save(
            update_fields=["verification_status", "verified_at", "verified_by", "updated_at"]
        )
    changes = {"from": before, "to": member.verification_status, "checks": results}
    if automatic:
        changes["policy"] = AUTOMATIC_POLICY
    action = "member.verifiedAutomatically" if automatic else "member.checksRecorded"
    _audit(actor, action, member, changes=changes)
    if automatic:
        engine.clear_cache()  # roles given to the member now apply
    return member


def verified_automatically(member) -> bool:
    """Verified under the policy rather than by a person: only the policy
    leaves a verified member without a reviewer (an import names its administrator)."""
    return member.verification_status == V.VERIFIED and member.verified_by_id is None


def role_areas(member) -> list:
    """Every area where the member holds a role: appointments that have not
    ended (suspended ones come back), delegated authority, and the area of a
    staff account of theirs."""
    now = timezone.now()
    areas = [
        *RoleAssignmentArea.objects.filter(
            Q(assignment__ends_at__isnull=True) | Q(assignment__ends_at__gt=now),
            assignment__member=member,
            assignment__status__in=[AssignmentStatus.ACTIVE, AssignmentStatus.SUSPENDED],
        ),
        *DelegationGrantArea.objects.filter(
            Q(grant__ends_at__isnull=True) | Q(grant__ends_at__gt=now),
            grant__recipient=member,
            grant__status=GrantStatus.ACTIVE,
        ),
    ]
    if member.user_id and member.user.role != Role.MEMBER:
        areas.append(member.user)
    return areas


def require_reviewer(actor, member):
    """Holds verifyMemberIdentity for the member's area, and for every area
    where they hold a role, under one authority that did not come from them:
    a coordinator decides the identities of the people in their area, never of
    someone whose role reaches beyond it. Never for oneself or a National
    Administrator."""
    from apps.access.services import authority_over_member, is_administrator_account

    authority_over_member(actor, C.VERIFY_MEMBER_IDENTITY, member, also=role_areas(member))
    if member.user_id and is_administrator_account(member.user):
        raise PermissionDenied(
            "A National Administrator's standing changes only through the "
            "two-administrator approval."
        )


@transaction.atomic
def approve(*, actor, member: Member, notes: str) -> Member:
    """A coordinator confirms the identity after reviewing what the system could not."""
    member = Member.objects.select_for_update().get(pk=member.pk)
    require_reviewer(actor, member)
    if member.verification_status == V.VERIFIED:
        raise ValidationError("This member is already verified.")
    if not notes.strip():
        raise ValidationError({"notes": "Record what you checked and how."})
    results = _run_checks(member)  # as things stand now, not when they last ran
    failed = sorted(kind for kind, result in results.items() if result == R.FAILED)
    if failed:
        labels = ", ".join(K(kind).label.lower() for kind in failed)
        raise ValidationError(f"Resolve the failed checks first: {labels}.")
    before = member.verification_status
    member.verification_status = V.VERIFIED
    member.verified_at = timezone.now()
    member.verified_by = actor
    member.save(update_fields=["verification_status", "verified_at", "verified_by", "updated_at"])
    _audit(
        actor,
        "member.verified",
        member,
        reason=notes.strip(),
        changes={"from": before, "checks": results},
    )
    return member


@transaction.atomic
def reject(*, actor, member: Member, reason: str) -> Member:
    """The identity could not be confirmed (or no longer can): no roles until it is."""
    member = Member.objects.select_for_update().get(pk=member.pk)
    require_reviewer(actor, member)
    if member.verification_status == V.REJECTED:
        raise ValidationError("This member's identity is already rejected.")
    if not reason.strip():
        raise ValidationError({"reason": "Say why the identity was not confirmed."})
    before = member.verification_status
    member.verification_status = V.REJECTED
    member.verified_at = None
    member.verified_by = actor
    member.save(update_fields=["verification_status", "verified_at", "verified_by", "updated_at"])
    _audit(actor, "member.rejected", member, reason=reason.strip(), changes={"from": before})
    engine.clear_cache()
    return member

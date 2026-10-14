"""Identity verification: record what was actually checked; people review the rest.

A member is verified automatically only when every check has passed. Today
there is no authoritative INEC source to validate a PVC against, no liveness
detection and no trusted reference photo, so those checks are recorded as
unsupported and every member goes to a coordinator's review. Nothing is
recorded as passed unless it was performed: reading a PVC is not validating
it, and a selfie alone is not biometric verification.
"""

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.access import audit, engine
from apps.access.models import (
    AssignmentStatus,
    DelegationGrantArea,
    GrantStatus,
    RoleAssignmentArea,
)
from apps.core.choices import RegistrationChannel, Role, VerificationStatus
from apps.core.permissions import Capability

from .models import Member, VerificationCheck

C = Capability
K = VerificationCheck.Kind
R = VerificationCheck.Result
V = VerificationStatus

# Every check must pass before a member is verified without a person's review.
REQUIRED = tuple(K.values)

NOT_AVAILABLE = {
    K.PVC_VALIDATED: (
        "No authoritative INEC source is connected; only the VIN's format and the polling "
        "unit code were checked."
    ),
    K.LIVENESS: "Liveness detection is not available.",
    K.FACE_MATCH: "No trusted reference photo is available to compare the selfie with.",
}


def _record(member, kind, result, detail):
    VerificationCheck.objects.update_or_create(
        member=member,
        kind=kind,
        defaults={
            "result": result,
            "detail": detail[:300],
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


def _run_checks(member) -> dict[str, str]:
    """Record the result of every check, as it stands now."""
    if member.vin and member.polling_unit_id and member.polling_unit_from_list:
        detail = "VIN entered; the polling unit was chosen from the list, not the code on the PVC."
        _record(member, K.PVC_CAPTURED, R.PENDING, detail)
    elif member.vin and member.polling_unit_id and member.pvc_image:
        _record(member, K.PVC_CAPTURED, R.PASSED, "VIN and polling unit code read from the card.")
    elif member.vin and member.polling_unit_id:
        detail = "VIN and polling unit code entered without a photo of the card."
        _record(member, K.PVC_CAPTURED, R.PENDING, detail)
    elif member.vin:
        _record(member, K.PVC_CAPTURED, R.PENDING, "VIN recorded without a polling unit code.")
    elif member.registration_channel == RegistrationChannel.USSD:
        detail = "Polling unit code given by USSD, without the VIN."
        _record(member, K.PVC_CAPTURED, R.FAILED, detail)
    else:
        _record(member, K.PVC_CAPTURED, R.FAILED, "Registered without a PVC.")

    if not member.date_of_birth:
        detail = "No date of birth, so others with the same name could not be compared."
        _record(member, K.DUPLICATES, R.PENDING, detail)
    elif duplicates := possible_duplicates(member):
        detail = f"{duplicates} other member(s) have the same name and date of birth."
        _record(member, K.DUPLICATES, R.PENDING, detail)
    else:
        detail = "Phone number and VIN are unique; nobody else has the same name and birth date."
        _record(member, K.DUPLICATES, R.PASSED, detail)

    if member.phone_verified_at and member.registration_channel == RegistrationChannel.USSD:
        detail = "The mobile network supplied the number when the member dialled the USSD code."
        _record(member, K.PHONE, R.PASSED, detail)
    elif member.phone_verified_at:
        _record(member, K.PHONE, R.PASSED, "A code sent by SMS was entered.")
    else:
        _record(member, K.PHONE, R.PENDING, "The phone number has not been confirmed by SMS.")

    if member.photo:
        detail = "A selfie was captured. This alone is not biometric verification."
        _record(member, K.SELFIE, R.PASSED, detail)
    else:
        _record(member, K.SELFIE, R.FAILED, "No selfie was captured.")

    for kind, detail in NOT_AVAILABLE.items():
        _record(member, kind, R.UNSUPPORTED, detail)
    return dict(member.verification_checks.values_list("kind", "result"))


@transaction.atomic
def record_checks(member: Member, actor=None) -> Member:
    """Run the checks the system can perform and set the verification status.

    A coordinator's decision (verified or rejected) is kept; otherwise the
    member is verified only if every check passed, or else awaits review.
    `actor`: whoever asked for the checks to run (None: registration).
    """
    member = Member.objects.select_for_update().get(pk=member.pk)
    results = _run_checks(member)
    before = member.verification_status
    if before not in (V.VERIFIED, V.REJECTED):
        if all(results.get(kind) == R.PASSED for kind in REQUIRED):
            member.verification_status = V.VERIFIED
            member.verified_at = timezone.now()
            member.verified_by = None
        else:
            member.verification_status = V.PENDING_REVIEW
        member.save(
            update_fields=["verification_status", "verified_at", "verified_by", "updated_at"]
        )
    _audit(
        actor,
        "member.checksRecorded",
        member,
        changes={"from": before, "to": member.verification_status, "checks": results},
    )
    return member


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

"""Membership and accreditation rules. Views stay thin; all writes go here."""

from datetime import timedelta

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.access import audit, engine
from apps.core.choices import (
    AccreditationStatus,
    GeographyLevel,
    MembershipState,
    VerificationStatus,
)
from apps.core.models import Sequence
from apps.core.permissions import Capability
from apps.geography.scope import scope_contains_any

from .models import (
    AccreditationEvent,
    AccreditedAgent,
    Member,
    PollingUnitChangeRequest,
    VolunteerApplication,
)

S = AccreditationStatus
C = Capability

# Allowed accreditation transitions. Revocation is final.
TRANSITIONS = {
    S.PENDING: {S.APPROVED, S.REVOKED},
    S.APPROVED: {S.SUSPENDED, S.REVOKED},
    S.SUSPENDED: {S.APPROVED, S.REVOKED},
    S.REVOKED: set(),
}
REASON_REQUIRED = {S.SUSPENDED, S.REVOKED}


def _require_scope(actor, target, what, *permissions):
    """One of `permissions` must be held by a grant covering `target`."""
    if not scope_contains_any(actor, target, permissions):
        raise PermissionDenied(f"The {what} is outside your operational scope.")


def _save_unique(instance):
    """Validate and save, turning DB uniqueness races into ValidationError."""
    instance.full_clean()
    try:
        with transaction.atomic():
            instance.save()
    except IntegrityError as exc:
        raise ValidationError(
            "This record conflicts with an existing one (duplicate phone, "
            "client id, or active accreditation)."
        ) from exc


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


# Details a verified identity rests on: changing them sends it back to review.
IDENTITY_FIELDS = ("full_name", "date_of_birth")


@transaction.atomic
def update_member(*, actor, member: Member, reason: str = "", **fields) -> Member:
    """Correct a member's details. Changing what a verified identity rests on
    sends it back to review, so that needs the authority to review identities,
    and a reason; a National Administrator's identity is never changed here."""
    from apps.access.services import authority_over_member, is_administrator_account
    from apps.geography.scope import SCOPE_FIELDS

    member = Member.objects.select_for_update(of=("self",)).select_related("user").get(pk=member.pk)
    authority_over_member(actor, C.MANAGE_MEMBERSHIP, member)
    before = {name: getattr(member, name) for name in fields}
    where = (member.scope_level, *(getattr(member, f"{f}_id") for f in SCOPE_FIELDS))
    for name, value in fields.items():
        setattr(member, name, value)
    member.full_clean(exclude=["membership_number"])
    _require_scope(actor, member, "member's new registration area", C.MANAGE_MEMBERSHIP)
    moved = where != (member.scope_level, *(getattr(member, f"{f}_id") for f in SCOPE_FIELDS))
    if moved and member.vin and not engine.is_national_administrator(actor):
        # A PVC holder's polling unit comes from their voter card.
        raise ValidationError(
            {"scope": "This member's polling unit comes from their PVC; use a change request."}
        )
    changed = sorted(name for name in fields if getattr(member, name) != before[name])
    identity_changed = any(name in IDENTITY_FIELDS for name in changed)
    if identity_changed and member.user_id and is_administrator_account(member.user):
        raise PermissionDenied(
            "A National Administrator's identity details cannot be changed here."
        )
    reverify = identity_changed and member.verification_status == VerificationStatus.VERIFIED
    if reverify:
        authority_over_member(actor, C.VERIFY_MEMBER_IDENTITY, member)
        if not reason.strip():
            raise ValidationError(
                {"reason": "Say why: the change sends a verified identity back to review."}
            )
        member.verification_status = VerificationStatus.PENDING_REVIEW
        member.verified_at = None
        member.verified_by = None
    _save_unique(member)
    if changed or moved:
        _audit(
            actor,
            "member.updated",
            member,
            reason.strip(),
            changes={"fields": changed, "moved": moved, "back_to_review": reverify},
        )
    if reverify:
        engine.clear_cache()
    return member


# The photos a member may add themselves after joining.
OWN_PHOTOS = {"photo": "selfie", "pvc_image": "PVC photo"}


@transaction.atomic
def set_own_photo(*, member: Member, field: str, upload) -> Member:
    """A member adds the selfie or PVC photo they did not give when joining, or
    replaces one before their identity is checked. After that only a
    coordinator changes it, so what a reviewer approved stays on record."""
    from . import identity, registration

    registration.validate_image(upload, field, required=True)
    member = Member.objects.select_for_update().select_related("user").get(pk=member.pk)
    replacing = bool(getattr(member, field))
    if replacing and member.verification_status == VerificationStatus.VERIFIED:
        raise ValidationError(
            {field: "Your identity has been checked, so only a coordinator can change this photo."}
        )
    setattr(member, field, upload)
    member.save(update_fields=[field, "updated_at"])
    _audit(
        member.user,
        "member.photoReplaced" if replacing else "member.photoAdded",
        member,
        changes={"kind": OWN_PHOTOS[field]},  # which photo, never the image itself
    )
    return identity.record_checks(member, actor=member.user)


@transaction.atomic
def accredit(*, actor, member: Member, **fields) -> AccreditedAgent:
    client_id = fields.get("client_id")
    if client_id:
        existing = AccreditedAgent.objects.filter(client_id=client_id).first()
        if existing is not None:
            return existing

    _require_scope(actor, member, "member", C.ACCREDIT_AGENTS)
    if member.accreditations.filter(status__in=[S.PENDING, S.APPROVED]).exists():
        raise ValidationError({"member": "This member already has an active accreditation."})

    agent = AccreditedAgent(member=member, created_by=actor, status=S.PENDING, **fields)
    agent.full_clean(exclude=["agent_id", "member"])
    _require_scope(actor, agent, "assignment", C.ACCREDIT_AGENTS)
    agent.agent_id = f"AG-{Sequence.next_value('agent'):05d}"
    agent.status_changed_at = timezone.now()
    _save_unique(agent)
    AccreditationEvent.objects.create(
        agent=agent, from_status="", to_status=S.PENDING, actor=actor, reason="Accredited"
    )
    return agent


@transaction.atomic
def change_status(
    *, actor, agent: AccreditedAgent, status: str, reason: str = ""
) -> AccreditedAgent:
    agent = AccreditedAgent.objects.select_for_update().get(pk=agent.pk)
    _require_scope(actor, agent, "agent", C.ACCREDIT_AGENTS)
    reason = reason.strip()

    if status == agent.status:
        raise ValidationError(
            {"status": f"The agent is already {agent.get_status_display().lower()}."}
        )
    if status not in TRANSITIONS[agent.status]:
        raise ValidationError({"status": f"Cannot change from {agent.status} to {status}."})
    if status in REASON_REQUIRED and not reason:
        raise ValidationError({"reason": "A reason is required to suspend or revoke."})
    if status == S.APPROVED and not agent.is_ready:
        raise ValidationError(
            {"status": "Training and biometric enrolment must be completed before approval."}
        )
    if status == S.APPROVED and not agent.member.can_hold_roles:
        raise ValidationError(
            {
                "status": "Only a member with a verified identity and an active membership "
                "can be approved as an agent."
            }
        )

    previous = agent.status
    agent.status = status
    agent.status_changed_at = timezone.now()
    _save_unique(agent)
    AccreditationEvent.objects.create(
        agent=agent, from_status=previous, to_status=status, actor=actor, reason=reason
    )
    return agent


@transaction.atomic
def update_readiness(*, actor, agent: AccreditedAgent, **fields) -> AccreditedAgent:
    _require_scope(actor, agent, "agent", C.ACCREDIT_AGENTS, C.MANAGE_AGENT_ASSIGNMENTS)
    if agent.status == S.REVOKED:
        raise ValidationError("A revoked accreditation cannot be updated.")
    for name, value in fields.items():
        setattr(agent, name, value)
    _save_unique(agent)
    return agent


# --- volunteering -------------------------------------------------------------


@transaction.atomic
def apply_to_volunteer(
    *, member: Member, interests: list[str], message: str = ""
) -> VolunteerApplication:
    """A member offers to help with party work. It does not make them an agent."""
    if member.membership_state != MembershipState.ACTIVE:
        raise ValidationError("Your membership is not active.")
    chosen = list(dict.fromkeys(interests or []))
    if not chosen:
        raise ValidationError({"interests": "Choose at least one way you can help."})
    if not set(chosen) <= set(VolunteerApplication.Interest.values):
        raise ValidationError({"interests": "Choose from the listed kinds of party work."})
    if member.volunteering.filter(status=VolunteerApplication.Status.PENDING).exists():
        raise ValidationError(
            "You already have an offer with your coordinators. Withdraw it to send a new one."
        )
    return VolunteerApplication.objects.create(
        member=member, interests=chosen, message=message.strip()
    )


@transaction.atomic
def withdraw_volunteering(*, member: Member, application: VolunteerApplication):
    """The member takes back an offer their coordinators have not answered yet."""
    application = VolunteerApplication.objects.select_for_update().get(pk=application.pk)
    if application.member_id != member.pk:
        raise PermissionDenied("This offer belongs to another member.")
    if application.status != VolunteerApplication.Status.PENDING:
        raise ValidationError(f"This offer is already {application.status}.")
    application.status = VolunteerApplication.Status.WITHDRAWN
    application.save(update_fields=["status", "updated_at"])
    return application


def _lock_application(actor, application):
    application = (
        VolunteerApplication.objects.select_for_update()
        .select_related("member")
        .get(pk=application.pk)
    )
    _require_scope(actor, application.member, "member", C.REGISTER_MEMBERS, C.MANAGE_MEMBERSHIP)
    if application.status != VolunteerApplication.Status.PENDING:
        raise ValidationError(f"This offer is already {application.status}.")
    return application


@transaction.atomic
def accept_volunteer(*, actor, application: VolunteerApplication, reason: str = ""):
    """Accept a member's offer of party work. Agents are accredited separately."""
    application = _lock_application(actor, application)
    application.status = VolunteerApplication.Status.ACCEPTED
    application.decided_by = actor
    application.decided_at = timezone.now()
    application.decision_reason = reason.strip()
    application.save()
    return application


@transaction.atomic
def decline_volunteer(*, actor, application: VolunteerApplication, reason: str):
    application = _lock_application(actor, application)
    if not reason.strip():
        raise ValidationError({"reason": "Tell the member why their offer was declined."})
    application.status = VolunteerApplication.Status.DECLINED
    application.decided_by = actor
    application.decided_at = timezone.now()
    application.decision_reason = reason.strip()
    application.save()
    return application


# --- membership standing ----------------------------------------------------------

MS = MembershipState


def _lock_standing(actor, member) -> Member:
    """Lock a member whose standing `actor` may change: not themselves, inside
    the actor's area, and never a National Administrator."""
    from apps.access.services import authority_over_member, is_administrator_account

    member = Member.objects.select_for_update(of=("self",)).select_related("user").get(pk=member.pk)
    authority_over_member(actor, C.SUSPEND_MEMBERS, member)
    if member.user_id and is_administrator_account(member.user):
        raise PermissionDenied(
            "A National Administrator is suspended or removed only through the "
            "two-administrator approval."
        )
    return member


def _set_standing(actor, member, state, reason):
    member.membership_state = state
    member.state_changed_at = timezone.now()
    member.state_changed_by = actor
    member.state_reason = reason
    member.save(
        update_fields=[
            "membership_state",
            "state_changed_at",
            "state_changed_by",
            "state_reason",
            "updated_at",
        ]
    )
    engine.clear_cache()


@transaction.atomic
def suspend_membership(*, actor, member: Member, reason: str) -> Member:
    """Block all operational access at once; appointments stay on record."""
    from apps.access.services import end_sessions

    member = _lock_standing(actor, member)
    if member.membership_state != MS.ACTIVE:
        state = member.membership_state
        raise ValidationError(
            {"state": f"Only an active membership can be suspended (it is {state})."}
        )
    if not reason.strip():
        raise ValidationError({"reason": "Say why the membership is suspended."})
    _set_standing(actor, member, MS.SUSPENDED, reason.strip())
    if member.user_id:
        end_sessions(member.user)
    _audit(actor, "member.suspended", member, reason.strip())
    return member


@transaction.atomic
def deactivate_membership(*, actor, member: Member, reason: str) -> Member:
    """End the membership: the account can no longer sign in. History is kept."""
    from apps.access.services import end_sessions
    from apps.accounts.models import User

    member = _lock_standing(actor, member)
    if member.membership_state == MS.DEACTIVATED:
        raise ValidationError({"state": "This membership is already deactivated."})
    if not reason.strip():
        raise ValidationError({"reason": "Say why the membership is deactivated."})
    before = member.membership_state
    _set_standing(actor, member, MS.DEACTIVATED, reason.strip())
    if member.user_id:
        User.objects.filter(pk=member.user_id).update(is_active=False)
        end_sessions(member.user)
    _audit(actor, "member.deactivated", member, reason.strip(), changes={"from": before})
    return member


@transaction.atomic
def restore_membership(*, actor, member: Member, reason: str = "") -> Member:
    """Lift a suspension or deactivation. Only appointments that are otherwise
    still valid authorize anything again."""
    from apps.accounts.models import User

    member = _lock_standing(actor, member)
    before = member.membership_state
    if before == MS.ACTIVE:
        raise ValidationError({"state": "This membership is already active."})
    _set_standing(actor, member, MS.ACTIVE, reason.strip())
    if before == MS.DEACTIVATED and member.user_id:
        User.objects.filter(pk=member.user_id).update(is_active=True)
    _audit(actor, "member.restored", member, reason.strip(), changes={"from": before})
    return member


# --- registered polling unit ------------------------------------------------------

PU = PollingUnitChangeRequest.Status
# Requests a member may make in 30 days (each stores a card photo).
CHANGES_PER_MONTH = 5


def _unit_code(unit):
    return unit.code if unit is not None else None


@transaction.atomic
def request_polling_unit_change(
    *, member: Member, delim: str, pvc_image, reason: str = ""
) -> PollingUnitChangeRequest:
    """The member asks to move to the polling unit on their updated voter card."""
    from .registration import polling_unit_from_delim, validate_image

    member = Member.objects.select_for_update().get(pk=member.pk)
    if member.membership_state != MS.ACTIVE:
        raise ValidationError("Your membership is not active.")
    unit = polling_unit_from_delim(delim)
    if unit.pk == member.polling_unit_id:
        raise ValidationError({"delim": "This is already your registered polling unit."})
    validate_image(pvc_image, "pvc_image", required=True)
    if member.polling_unit_changes.filter(status=PU.PENDING).exists():
        raise ValidationError("A polling unit change is already awaiting review.")
    month_ago = timezone.now() - timedelta(days=30)
    if member.polling_unit_changes.filter(created_at__gte=month_ago).count() >= CHANGES_PER_MONTH:
        raise ValidationError("Too many change requests this month. Ask your coordinator.")
    change = PollingUnitChangeRequest(
        member=member,
        from_polling_unit=member.polling_unit,
        delim=delim.strip(),
        pvc_image=pvc_image,
        reason=reason.strip(),
        scope_level=GeographyLevel.POLLING_UNIT,
        polling_unit=unit,
    )
    change.normalize_scope()
    change.save()
    _audit(
        member.user,
        "member.pollingUnitChangeRequested",
        member,
        change.reason,
        changes={"from": _unit_code(change.from_polling_unit), "to": unit.code},
    )
    return change


@transaction.atomic
def withdraw_polling_unit_change(*, member: Member, change) -> PollingUnitChangeRequest:
    change = PollingUnitChangeRequest.objects.select_for_update().get(pk=change.pk)
    if change.member_id != member.pk:
        raise PermissionDenied("This request belongs to another member.")
    if change.status != PU.PENDING:
        raise ValidationError(f"This request is already {change.status}.")
    change.status = PU.WITHDRAWN
    change.save(update_fields=["status", "updated_at"])
    _audit(
        member.user,
        "member.pollingUnitChangeWithdrawn",
        member,
        changes={"to": change.polling_unit.code, "request": str(change.uuid)},
    )
    return change


def _lock_change(actor, change) -> PollingUnitChangeRequest:
    change = (
        # Keep the member locked while deciding, without locking nullable
        # polling-unit outer joins (unsupported by PostgreSQL).
        PollingUnitChangeRequest.objects.select_for_update(of=("self", "member"))
        .select_related("member", "polling_unit", "from_polling_unit")
        .get(pk=change.pk)
    )
    from apps.access.services import authority_over_member

    authority_over_member(
        actor,
        C.REVIEW_POLLING_UNIT_CHANGES,
        change.member,
        area=change,
        what="requested polling unit",
    )
    if change.status != PU.PENDING:
        raise ValidationError(f"This request is already {change.status}.")
    return change


def _decide(change, actor, status, reason):
    change.status = status
    change.decided_by = actor
    change.decided_at = timezone.now()
    change.decision_reason = reason
    change.save()


@transaction.atomic
def approve_polling_unit_change(*, actor, change, reason: str = "") -> PollingUnitChangeRequest:
    """The updated card was checked: the member's registered polling unit moves.

    Operational appointments are not changed.
    """
    from apps.geography.scope import SCOPE_FIELDS

    change = _lock_change(actor, change)
    member = Member.objects.select_for_update().get(pk=change.member_id)
    if member.membership_state != MS.ACTIVE:
        raise ValidationError("This membership is not active.")
    before = _unit_code(member.polling_unit)
    member.scope_level = GeographyLevel.POLLING_UNIT
    member.polling_unit = change.polling_unit
    member.normalize_scope()
    member.pvc_image = change.pvc_image
    member.polling_unit_from_list = False  # it now comes from the updated card
    member.save(
        update_fields=[
            "scope_level",
            *SCOPE_FIELDS,
            "pvc_image",
            "polling_unit_from_list",
            "updated_at",
        ]
    )
    _decide(change, actor, PU.APPROVED, reason.strip())
    _audit(
        actor,
        "member.pollingUnitChanged",
        member,
        reason.strip(),
        changes={"from": before, "to": change.polling_unit.code, "request": str(change.uuid)},
    )
    return change


@transaction.atomic
def reject_polling_unit_change(*, actor, change, reason: str) -> PollingUnitChangeRequest:
    change = _lock_change(actor, change)
    if not reason.strip():
        raise ValidationError({"reason": "Tell the member why the change was not accepted."})
    _decide(change, actor, PU.REJECTED, reason.strip())
    _audit(
        actor,
        "member.pollingUnitChangeRejected",
        change.member,
        reason.strip(),
        changes={"to": change.polling_unit.code, "request": str(change.uuid)},
    )
    return change

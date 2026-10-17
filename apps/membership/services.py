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
    Role,
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
    PvcUpdate,
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


# Details from the PVC a member may add themselves after joining.
OWN_PVC_DETAILS = ("gender", "date_of_birth")


@transaction.atomic
def update_own_details(*, member: Member, actor=None, **fields) -> Member:
    """A member edits their email and occupation, and adds the gender and date
    of birth left out when joining. Like the photos, those two can be changed
    until the identity is checked; after that only a coordinator changes them.
    Only the edited fields are saved, so a suspension or review decided
    meanwhile is never written back over. `actor`: the person who registered
    the member, completing their registration (see require_completing)."""
    from . import identity

    by = actor or member.user
    member = Member.objects.select_for_update(of=("self",)).select_related("user").get(pk=member.pk)
    verified = member.verification_status == VerificationStatus.VERIFIED
    for name in OWN_PVC_DETAILS:
        if name in fields and verified and getattr(member, name) not in ("", None):
            if fields[name] != getattr(member, name):
                raise ValidationError(
                    {name: "Your identity has been checked, so only a coordinator can change this."}
                )
    changed = sorted(name for name, value in fields.items() if getattr(member, name) != value)
    for name, value in fields.items():
        setattr(member, name, value)
    member.full_clean()
    member.save(update_fields=[*fields, "updated_at"])
    details = [name for name in changed if name in OWN_PVC_DETAILS]
    # Field names only: the values are personal data. Every change someone
    # else makes is recorded.
    if recorded := (changed if actor is not None else details):
        _audit(by, "member.detailsChanged", member, changes={"fields": recorded})
    if details:
        member = identity.record_checks(member, actor=by)
    return member


@transaction.atomic
def set_own_selfie(*, member: Member, upload, actor=None) -> Member:
    """A member adds the selfie they did not take when joining, or replaces it
    before their identity is checked. After that only a coordinator changes
    it, so what a reviewer approved stays on record. (The PVC photo comes
    with the member's one PVC scan: see scan_own_pvc.) `actor`: as for
    update_own_details."""
    from . import identity, registration

    by = actor or member.user
    registration.validate_image(upload, "photo", required=True)
    member = Member.objects.select_for_update(of=("self",)).select_related("user").get(pk=member.pk)
    replacing = bool(member.photo)
    if replacing and member.verification_status == VerificationStatus.VERIFIED:
        raise ValidationError(
            {"photo": "Your identity has been checked, so only a coordinator can change it."}
        )
    member.photo = upload
    member.save(update_fields=["photo", "updated_at"])
    _audit(
        by,
        "member.photoReplaced" if replacing else "member.photoAdded",
        member,
        changes={"kind": "selfie"},  # which photo, never the image itself
    )
    return identity.record_checks(member, actor=by)


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


# --- the member's one PVC scan after joining -------------------------------------------

PV = PvcUpdate.Status

# What a PVC scan can replace, with the labels a reviewer sees.
PVC_FIELDS = {
    "full_name": "Name",
    "vin": "VIN",
    "gender": "Gender",
    "date_of_birth": "Date of birth",
    "occupation": "Occupation",
    "polling_unit": "Polling unit",
}
# A detail the card did not show (left blank) keeps the member's own.
PVC_OPTIONAL = ("gender", "date_of_birth", "occupation")


def _pvc_value(record, name):
    return getattr(record, "polling_unit_id" if name == "polling_unit" else name)


def pvc_changes(member: Member, update: PvcUpdate) -> list[str]:
    """The member's details the update would change, in PVC_FIELDS order."""
    return [
        name
        for name in PVC_FIELDS
        if _pvc_value(update, name) not in ("", None)
        and _pvc_value(update, name) != _pvc_value(member, name)
    ]


def can_scan_pvc(member: Member) -> bool:
    """One PVC scan per member: none once their card is on record (scanned or
    uploaded when joining, or an earlier scan), nor while one waits."""
    return not member.pvc_image and not member.pvc_updates.filter(status=PV.PENDING).exists()


def _apply_pvc_update(member: Member, update: PvcUpdate) -> None:
    from apps.geography.scope import SCOPE_FIELDS

    member.full_name = update.full_name
    member.vin = update.vin
    for name in PVC_OPTIONAL:
        if getattr(update, name) not in ("", None):
            setattr(member, name, getattr(update, name))
    member.pvc_image = update.pvc_image
    member.scope_level = GeographyLevel.POLLING_UNIT
    member.polling_unit = update.polling_unit
    member.normalize_scope()
    member.polling_unit_from_list = False  # the unit now comes from the card
    member.full_clean(exclude=["membership_number"])
    member.save(
        update_fields=[
            "full_name",
            "vin",
            *PVC_OPTIONAL,
            "pvc_image",
            "scope_level",
            *SCOPE_FIELDS,
            "polling_unit_from_list",
            "updated_at",
        ]
    )
    if member.user_id and member.user.role == Role.MEMBER:
        # A member's own sign-in shows the name from their card too.
        member.user.full_name = member.full_name
        member.user.save(update_fields=["full_name"])


@transaction.atomic
def scan_own_pvc(
    *,
    member: Member,
    pvc_image,
    full_name: str,
    vin: str,
    delim: str,
    gender: str = "",
    date_of_birth=None,
    occupation: str = "",
    actor=None,
) -> PvcUpdate:
    """The member's one PVC scan after joining: the details read off the card,
    which they checked, replace the ones they typed. They apply at once unless
    the identity is checked; then they wait for a coordinator's second check.
    `actor`: as for update_own_details."""
    from . import identity, registration

    member = Member.objects.select_for_update(of=("self",)).select_related("user").get(pk=member.pk)
    by = actor or member.user
    if member.membership_state != MS.ACTIVE:
        raise ValidationError("Your membership is not active.")
    if member.pvc_image:
        raise ValidationError(
            "Your PVC is already on record, and a card is scanned only once. Ask a "
            "coordinator if its details need correcting."
        )
    if member.pvc_updates.filter(status=PV.PENDING).exists():
        raise ValidationError("Your PVC scan is already waiting for a coordinator.")
    registration.validate_image(pvc_image, "pvc_image", required=True)
    vin = "".join((vin or "").split()).upper()
    if Member.objects.filter(vin=vin).exclude(pk=member.pk).exists():
        raise ValidationError({"vin": "This PVC is registered to another member."})
    verified = member.verification_status == VerificationStatus.VERIFIED
    update = PvcUpdate(
        member=member,
        pvc_image=pvc_image,
        full_name=" ".join((full_name or "").split()),
        vin=vin,
        gender=gender or "",
        date_of_birth=date_of_birth,
        occupation=(occupation or "").strip(),
        status=PV.PENDING if verified else PV.APPLIED,
        scope_level=GeographyLevel.POLLING_UNIT,
        polling_unit=registration.polling_unit_from_delim(delim),
    )
    update.normalize_scope()
    update.full_clean()
    update.changed_fields = pvc_changes(member, update)
    update.save()
    changes = {"request": str(update.uuid), "fields": update.changed_fields}
    if verified:
        _audit(by, "member.pvcUpdateRequested", member, changes=changes)
        return update
    _apply_pvc_update(member, update)
    _audit(by, "member.pvcUpdateApplied", member, changes=changes)
    identity.record_checks(member, actor=by)
    return update


@transaction.atomic
def withdraw_pvc_update(*, member: Member, update, actor=None) -> PvcUpdate:
    update = PvcUpdate.objects.select_for_update().get(pk=update.pk)
    if update.member_id != member.pk:
        raise PermissionDenied("This scan belongs to another member.")
    if update.status != PV.PENDING:
        raise ValidationError(f"This scan is already {update.status}.")
    update.status = PV.WITHDRAWN
    update.save(update_fields=["status", "updated_at"])
    _audit(
        actor or member.user,
        "member.pvcUpdateWithdrawn",
        member,
        changes={"request": str(update.uuid)},
    )
    return update


# --- completing a registration for someone ---------------------------------------

# Once the identity is decided, the details are the member's and their coordinators'.
CHECKED = (VerificationStatus.VERIFIED, VerificationStatus.REJECTED)


def can_complete(actor, member: Member) -> bool:
    """Whoever registered a member may complete the registration until the
    identity is checked (decided on 17 October 2026)."""
    return (
        member.created_by_id == actor.pk
        and member.verification_status not in CHECKED
        and member.membership_state == MS.ACTIVE
    )


def require_completing(actor, member: Member) -> None:
    if member.created_by_id != actor.pk:
        raise PermissionDenied("You did not register this member.")
    if member.verification_status in CHECKED:
        raise PermissionDenied(
            "This member's identity has been checked, so only they or a coordinator "
            "change their details now."
        )
    if member.membership_state != MS.ACTIVE:
        raise PermissionDenied("This membership is not active.")


def has_signed_in(member: Member) -> bool:
    return bool(member.user_id and member.user.last_login)


@transaction.atomic
def correct_registration(
    *, actor, member: Member, full_name=None, phone_number=None, scope=None
) -> Member:
    """The person who registered a member corrects the name, phone number or
    place they gave. Once the PVC is on record, the name and place come from
    it; the phone number, which is also the first password, changes only
    until the member first signs in."""
    from apps.accounts.verification import require_phone
    from apps.geography.scope import SCOPE_FIELDS

    from . import identity, registration

    member = Member.objects.select_for_update(of=("self",)).select_related("user").get(pk=member.pk)
    require_completing(actor, member)
    changed = []
    if full_name is not None:
        name = " ".join(full_name.split())
        if name != member.full_name:
            if member.vin:
                raise ValidationError({"full_name": "The name now comes from the member's PVC."})
            member.full_name = name
            changed.append("full_name")
    if scope is not None:
        where = (member.scope_level, *(getattr(member, f"{f}_id") for f in SCOPE_FIELDS))
        for name, value in scope.items():
            setattr(member, name, value)
        member.normalize_scope()
        if where != (member.scope_level, *(getattr(member, f"{f}_id") for f in SCOPE_FIELDS)):
            if member.vin:
                raise ValidationError({"scope": "The place now comes from the member's PVC."})
            changed.append("place")
    if phone_number is not None:
        phone = require_phone(phone_number)
        if phone != member.phone_number:
            if has_signed_in(member):
                raise ValidationError(
                    {"phone_number": "The member has signed in with this number, so it stays."}
                )
            registration.ensure_available(phone)
            member.phone_number = phone
            changed.append("phone_number")
    if not changed:
        return member
    _save_unique(member)
    if member.user_id:
        user = member.user
        user.full_name = member.full_name
        user.phone_number = member.phone_number
        fields = ["full_name", "phone_number"]
        if "phone_number" in changed:
            # The first password follows the number.
            user.set_password(registration.first_password(member.phone_number))
            fields.append("password")
        user.save(update_fields=fields)
    _audit(actor, "member.registrationCorrected", member, changes={"fields": changed})
    if member.verification_status == VerificationStatus.PENDING_REVIEW:
        member = identity.record_checks(member, actor=actor)
    return member


def _lock_pvc_update(actor, update) -> PvcUpdate:
    """Whoever may check this member's identity decides; when the card moves
    them, the same authority must reach the polling unit on it."""
    from apps.access.services import authority_over_member

    from . import identity

    update = (
        PvcUpdate.objects.select_for_update(of=("self", "member"))
        .select_related("member__user", "polling_unit")
        .get(pk=update.pk)
    )
    member = update.member
    identity.require_reviewer(actor, member)
    if update.polling_unit_id != member.polling_unit_id:
        authority_over_member(
            actor,
            C.VERIFY_MEMBER_IDENTITY,
            member,
            area=update,
            also=[member, *identity.role_areas(member)],
            what="polling unit on the card",
        )
    if update.status != PV.PENDING:
        raise ValidationError(f"This update is already {update.status}.")
    return update


@transaction.atomic
def approve_pvc_update(*, actor, update, reason: str = "") -> PvcUpdate:
    """The second check: the new details match the card, so they replace the
    member's. A checked identity stays checked, now on this reviewer's word."""
    from . import identity

    update = _lock_pvc_update(actor, update)
    member = (
        Member.objects.select_for_update(of=("self",))
        .select_related("user")
        .get(pk=update.member_id)
    )
    if member.membership_state != MS.ACTIVE:
        raise ValidationError("This membership is not active.")
    if Member.objects.filter(vin=update.vin).exclude(pk=member.pk).exists():
        raise ValidationError({"vin": "This PVC is now registered to another member."})
    fields = pvc_changes(member, update)
    _apply_pvc_update(member, update)
    if member.verification_status == VerificationStatus.VERIFIED:
        member.verified_at = timezone.now()
        member.verified_by = actor
        member.save(update_fields=["verified_at", "verified_by", "updated_at"])
    _decide(update, actor, PV.APPROVED, reason.strip())
    _audit(
        actor,
        "member.pvcUpdateApproved",
        member,
        reason.strip(),
        changes={"request": str(update.uuid), "fields": fields},
    )
    identity.record_checks(member, actor=actor)
    engine.clear_cache()
    return update


@transaction.atomic
def reject_pvc_update(*, actor, update, reason: str) -> PvcUpdate:
    update = _lock_pvc_update(actor, update)
    if not reason.strip():
        raise ValidationError({"reason": "Tell the member why the new details were not accepted."})
    _decide(update, actor, PV.REJECTED, reason.strip())
    _audit(
        actor,
        "member.pvcUpdateRejected",
        update.member,
        reason.strip(),
        changes={"request": str(update.uuid)},
    )
    return update

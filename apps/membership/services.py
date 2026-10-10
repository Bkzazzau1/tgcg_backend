"""Membership and accreditation rules. Views stay thin; all writes go here."""

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.core.choices import AccreditationStatus, GeographyLevel
from apps.core.models import Sequence
from apps.geography.scope import scope_contains

from .models import AccreditationEvent, AccreditedAgent, Member, VolunteerApplication

S = AccreditationStatus

# Allowed accreditation transitions. Revocation is final.
TRANSITIONS = {
    S.PENDING: {S.APPROVED, S.REVOKED},
    S.APPROVED: {S.SUSPENDED, S.REVOKED},
    S.SUSPENDED: {S.APPROVED, S.REVOKED},
    S.REVOKED: set(),
}
REASON_REQUIRED = {S.SUSPENDED, S.REVOKED}


def _require_scope(actor, target, what):
    if not scope_contains(actor, target):
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


@transaction.atomic
def update_member(*, actor, member: Member, **fields) -> Member:
    _require_scope(actor, member, "member")
    for name, value in fields.items():
        setattr(member, name, value)
    member.full_clean(exclude=["membership_number"])
    _require_scope(actor, member, "member's new registration area")
    _save_unique(member)
    return member


@transaction.atomic
def accredit(*, actor, member: Member, **fields) -> AccreditedAgent:
    client_id = fields.get("client_id")
    if client_id:
        existing = AccreditedAgent.objects.filter(client_id=client_id).first()
        if existing is not None:
            return existing

    _require_scope(actor, member, "member")
    if member.accreditations.filter(status__in=[S.PENDING, S.APPROVED]).exists():
        raise ValidationError({"member": "This member already has an active accreditation."})

    agent = AccreditedAgent(member=member, created_by=actor, status=S.PENDING, **fields)
    agent.full_clean(exclude=["agent_id", "member"])
    _require_scope(actor, agent, "assignment")
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
    _require_scope(actor, agent, "agent")
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
    _require_scope(actor, agent, "agent")
    if agent.status == S.REVOKED:
        raise ValidationError("A revoked accreditation cannot be updated.")
    for name, value in fields.items():
        setattr(agent, name, value)
    _save_unique(agent)
    return agent


# --- volunteering -------------------------------------------------------------


@transaction.atomic
def apply_to_volunteer(*, member: Member, message: str = "") -> VolunteerApplication:
    if member.polling_unit_id is None:
        raise ValidationError(
            "Add your polling unit (scan your PVC or ask a coordinator) before volunteering."
        )
    if member.accreditations.filter(status__in=[S.PENDING, S.APPROVED]).exists():
        raise ValidationError("You already have an active accreditation.")
    if member.volunteering.filter(status=VolunteerApplication.Status.PENDING).exists():
        raise ValidationError("You already have a pending application.")
    return VolunteerApplication.objects.create(member=member, message=message.strip())


def _lock_application(actor, application):
    application = (
        VolunteerApplication.objects.select_for_update()
        .select_related("member")
        .get(pk=application.pk)
    )
    _require_scope(actor, application.member, "member")
    if application.status != VolunteerApplication.Status.PENDING:
        raise ValidationError(f"This application is already {application.status}.")
    return application


@transaction.atomic
def accept_volunteer(*, actor, application: VolunteerApplication, reason: str = ""):
    """Accept and open a pending polling-unit accreditation for the member."""
    application = _lock_application(actor, application)
    member = application.member
    agent = accredit(
        actor=actor,
        member=member,
        role=application.role,
        scope_level=GeographyLevel.POLLING_UNIT,
        polling_unit=member.polling_unit,
        registered_phone_number=member.phone_number,
    )
    application.status = VolunteerApplication.Status.ACCEPTED
    application.agent = agent
    application.decided_by = actor
    application.decided_at = timezone.now()
    application.decision_reason = reason.strip()
    application.save()
    return application


@transaction.atomic
def decline_volunteer(*, actor, application: VolunteerApplication, reason: str):
    application = _lock_application(actor, application)
    if not reason.strip():
        raise ValidationError({"reason": "Tell the member why their application was declined."})
    application.status = VolunteerApplication.Status.DECLINED
    application.decided_by = actor
    application.decided_at = timezone.now()
    application.decision_reason = reason.strip()
    application.save()
    return application

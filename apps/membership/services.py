"""Membership and accreditation rules. Views stay thin; all writes go here."""

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.core.choices import AccreditationStatus
from apps.core.models import Sequence
from apps.geography.scope import scope_contains

from .models import AccreditationEvent, AccreditedAgent, Member

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
def create_member(*, actor, **fields) -> Member:
    client_id = fields.get("client_id")
    if client_id:
        existing = Member.objects.filter(client_id=client_id).first()
        if existing is not None:
            return existing  # offline retry: idempotent

    member = Member(created_by=actor, **fields)
    member.full_clean(exclude=["membership_number"])
    _require_scope(actor, member, "member's registration area")
    member.membership_number = f"TGCG-{Sequence.next_value('member'):06d}"
    _save_unique(member)
    return member


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

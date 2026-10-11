"""Writing and checking the security audit log."""

from django.db import transaction
from django.utils import timezone

from apps.core.models import Sequence

from .models import AuditEvent

# Never written to the log, whatever a caller passes in `changes`.
SENSITIVE_KEYS = frozenset(
    {"phone_number", "phone", "vin", "photo", "pvc_image", "pin", "password", "code", "email"}
)


def _without_sensitive(value):
    if isinstance(value, dict):
        return {
            key: _without_sensitive(item)
            for key, item in value.items()
            if key not in SENSITIVE_KEYS
        }
    if isinstance(value, list | tuple):
        return [_without_sensitive(item) for item in value]
    return value


def record(
    *,
    actor,
    action: str,
    target_type: str,
    target_id,
    target_label: str = "",
    scope=None,
    changes=None,
    reason: str = "",
    provenance=None,
) -> AuditEvent:
    """Append an event. Runs inside the caller's transaction when there is one."""
    actor = actor if getattr(actor, "is_authenticated", False) else None
    with transaction.atomic():
        # A lock row serialises writers so two events never share a predecessor.
        Sequence.objects.select_for_update().get_or_create(name="audit-chain")
        last = AuditEvent.objects.order_by("-id").only("hash").first()
        event = AuditEvent(
            created_at=timezone.now(),
            actor=actor,
            actor_label=actor.access_id if actor else "system",
            action=action,
            target_type=target_type,
            target_id=str(target_id),
            target_label=target_label[:200],
            scope=_without_sensitive(scope or []),
            changes=_without_sensitive(changes or {}),
            reason=reason,
            provenance=_without_sensitive(provenance or {}),
            previous_hash=last.hash if last else "",
        )
        event.hash = event.compute_hash()
        event.save()
    return event


def verify_chain() -> int | None:
    """The id of the first event whose hash or link is wrong, or None if intact."""
    previous = ""
    for event in AuditEvent.objects.order_by("id").iterator():
        if event.previous_hash != previous or event.compute_hash() != event.hash:
            return event.pk
        previous = event.hash
    return None

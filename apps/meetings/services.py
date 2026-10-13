"""Who may see, call, join and end meetings. All changes go through here."""

from datetime import timedelta

from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import F, Q
from django.utils import timezone

from apps.access import audit, engine
from apps.accounts.models import User
from apps.core.models import Sequence
from apps.core.permissions import Capability, has_capability
from apps.geography.scope import covering_q, default_to_actor_scope, scope_contains

from . import livekit
from .models import Admission, Meeting

C = Capability
M = Meeting
MAX_INVITEES = 200
FURTHEST_AHEAD = timedelta(days=90)
# A join pass is used once, to connect; LiveKit renews it while connected.
PASS_SECONDS = 30 * 60


def visible_meetings(user):
    """Meetings the user called or is invited to, and meetings for everyone
    in an area their own area lies inside. A national officer does not see
    every ward's meetings: only national ones, unless invited."""
    in_area = Q(pk__in=[])
    for area in engine.areas_for(user, C.JOIN_MEETING):
        in_area |= covering_q(area)
    return Meeting.objects.filter(
        Q(host=user) | Q(invitees=user) | (Q(audience=M.Audience.AREA) & in_area)
    ).distinct()


def can_join(user, meeting) -> bool:
    if meeting.host_id == user.pk:
        return True
    if not has_capability(user, C.JOIN_MEETING):
        return False
    if any(person.pk == user.pk for person in meeting.invitees.all()):
        return True
    return meeting.audience == M.Audience.AREA and any(
        engine.area_contains(meeting, area) for area in engine.areas_for(user, C.JOIN_MEETING)
    )


def can_manage(user, meeting) -> bool:
    """The host, or someone who may call meetings over the whole area."""
    return meeting.host_id == user.pk or scope_contains(user, meeting, permission=C.START_MEETING)


def _audit(actor, action, meeting, reason="", changes=None):
    audit.record(
        actor=actor,
        action=action,
        target_type="meeting",
        target_id=meeting.reference,
        target_label=meeting.title,
        changes=changes,
        reason=reason,
    )


def _invitees(host, access_ids) -> list:
    wanted = {access_id.strip().upper() for access_id in access_ids if access_id.strip()}
    if len(wanted) > MAX_INVITEES:
        raise ValidationError({"invitees": f"Invite at most {MAX_INVITEES} people."})
    people = list(User.objects.filter(access_id__in=wanted, is_active=True))
    unknown = sorted(wanted - {person.access_id for person in people})
    if unknown:
        raise ValidationError({"invitees": f"No active account for: {', '.join(unknown)}."})
    return [person for person in people if person.pk != host.pk]


@transaction.atomic
def create_meeting(*, actor, invitees=(), **fields) -> Meeting:
    """Calls a meeting for an area inside the actor's own (theirs by default)."""
    default_to_actor_scope(actor, fields, C.START_MEETING)
    now = timezone.now()
    fields["starts_at"] = fields.get("starts_at") or now
    meeting = Meeting(host=actor, **fields)
    meeting.full_clean(exclude=["reference"])
    if not scope_contains(actor, meeting, permission=C.START_MEETING):
        raise PermissionDenied("You can only call meetings for areas inside your own.")
    if meeting.starts_at < now - timedelta(minutes=5):
        raise ValidationError({"starts_at": "A meeting cannot start in the past."})
    if meeting.starts_at > now + FURTHEST_AHEAD:
        raise ValidationError({"starts_at": "Meetings can be planned up to 90 days ahead."})
    people = _invitees(actor, invitees)
    if meeting.audience == M.Audience.INVITED and not people:
        raise ValidationError({"invitees": "Invite someone, or open the meeting to the area."})
    meeting.reference = f"MTG-{Sequence.next_value('meeting'):06d}"
    meeting.save()
    meeting.invitees.set(people)
    _audit(
        actor,
        "meeting.created",
        meeting,
        changes={
            "kind": meeting.kind,
            "audience": meeting.audience,
            "starts_at": meeting.starts_at.isoformat(),
            "invitees": len(people),
        },
    )
    return meeting


@transaction.atomic
def admit(*, actor, meeting) -> dict:
    """A join pass for the meeting's LiveKit room, while it is open."""
    if not can_join(actor, meeting):
        raise PermissionDenied("This meeting is not open to you.")
    status = meeting.status
    if status == M.Status.CANCELLED:
        raise ValidationError("This meeting was cancelled.")
    if status == M.Status.ENDED:
        raise ValidationError("This meeting has ended.")
    if status == M.Status.SCHEDULED:
        raise ValidationError("Joining opens 15 minutes before the start.")
    token = livekit.join_token(
        identity=actor.access_id,
        name=actor.full_name,
        room=meeting.room_name,
        video=meeting.kind == M.Kind.VIDEO,
        seconds=PASS_SECONDS,
    )
    admission, created = Admission.objects.get_or_create(meeting=meeting, user=actor)
    if not created:
        Admission.objects.filter(pk=admission.pk).update(
            passes=F("passes") + 1, last_admitted_at=timezone.now()
        )
    return {
        "url": settings.LIVEKIT_URL,
        "token": token,
        "room": meeting.room_name,
        "expires_at": timezone.now() + timedelta(seconds=PASS_SECONDS),
    }


def _lock(meeting) -> Meeting:
    return Meeting.objects.select_for_update().get(pk=meeting.pk)


def _close_room(meeting):
    room = meeting.room_name
    transaction.on_commit(lambda: livekit.close_room(room))


@transaction.atomic
def end_meeting(*, actor, meeting) -> Meeting:
    """Ends a meeting that has opened, for everyone in it."""
    meeting = _lock(meeting)
    if not can_manage(actor, meeting):
        raise PermissionDenied("Only the host or a coordinator over this area can end it.")
    status = meeting.status
    if status == M.Status.SCHEDULED:
        raise ValidationError("This meeting has not opened yet; cancel it instead.")
    if status in (M.Status.ENDED, M.Status.CANCELLED):
        raise ValidationError(f"This meeting is already {status}.")
    meeting.ended_at = timezone.now()
    meeting.ended_by = actor
    meeting.save(update_fields=["ended_at", "ended_by", "updated_at"])
    _audit(actor, "meeting.ended", meeting)
    _close_room(meeting)
    return meeting


@transaction.atomic
def cancel_meeting(*, actor, meeting, reason: str) -> Meeting:
    """Calls a meeting off before it ends; anyone already in it is disconnected."""
    meeting = _lock(meeting)
    if not can_manage(actor, meeting):
        raise PermissionDenied("Only the host or a coordinator over this area can cancel it.")
    reason = reason.strip()
    if not reason:
        raise ValidationError({"reason": "Say why the meeting is cancelled."})
    status = meeting.status
    if status in (M.Status.ENDED, M.Status.CANCELLED):
        raise ValidationError(f"This meeting is already {status}.")
    meeting.cancelled_at = timezone.now()
    meeting.cancel_reason = reason
    meeting.save(update_fields=["cancelled_at", "cancel_reason", "updated_at"])
    _audit(actor, "meeting.cancelled", meeting, reason=reason)
    if status == M.Status.LIVE:
        _close_room(meeting)
    return meeting

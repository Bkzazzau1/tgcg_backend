"""Check-ins, duty sessions, location pings, and learning polling-unit locations."""

from datetime import timedelta

from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from apps.access import engine
from apps.access.models import AssignmentStatus, RoleAssignmentArea, RoleDefinition, RoleStatus
from apps.access.services import identity_verified, in_good_standing
from apps.accounts.models import User
from apps.core.choices import GeographyLevel, MembershipState, Role, VerificationStatus
from apps.core.permissions import FIELD_ROLES, Capability, has_capability
from apps.geography.gps import LocationStatus, assess
from apps.geography.models import PollingUnit
from apps.geography.scope import ScopeRef, scope_contains, within_scope_q

from .models import AgentCheckIn, DutySession, LocationPing

C = Capability
# Memberships that share no location and count for nothing.
STOPPED = [MembershipState.SUSPENDED, MembershipState.DEACTIVATED]

NOT_VERIFIED = (
    "Agents and observers work only once their identity is verified. "
    "Ask your coordinator to verify you."
)


def _require_field_duty(user):
    """Agents, observers and polling unit executives: whoever holds fieldDuty."""
    if user.role in FIELD_ROLES:
        _require_standing(user)  # says why an agent's own role does not count yet
    if not has_capability(user, C.FIELD_DUTY):
        raise PermissionDenied(
            "Only agents, observers and polling unit executives share location from the app."
        )
    _require_standing(user)


def duty_polling_unit(user) -> PollingUnit | None:
    """Where a field worker checks in: the one polling unit where they hold
    fieldDuty (an agent account's own unit, or the unit of an appointment)."""
    units = {
        area.polling_unit_id
        for area in engine.areas_for(user, C.FIELD_DUTY)
        if area.scope_level == GeographyLevel.POLLING_UNIT and area.polling_unit_id
    }
    if len(units) != 1:
        return None
    return PollingUnit.objects.get(pk=units.pop())


def field_workers(scope) -> dict[int, int]:
    """Who works at the polling units in `scope`, as {user id: polling unit id}:
    agent accounts at their unit, and verified members appointed to a polling
    unit with fieldDuty, such as a chapter's polling unit executives."""
    workers = dict(
        User.objects.filter(
            role=Role.POLLING_UNIT_AGENT,
            is_active=True,
            polling_unit__isnull=False,
            member__verification_status=VerificationStatus.VERIFIED,
        )
        .filter(within_scope_q(scope))
        .exclude(member__membership_state__in=STOPPED)
        .values_list("pk", "polling_unit_id")
    )
    roles = [
        role.pk
        for role in RoleDefinition.objects.filter(status=RoleStatus.ACTIVE)
        if C.FIELD_DUTY in role.permissions
    ]
    now = timezone.now()
    appointed = (
        RoleAssignmentArea.objects.filter(
            within_scope_q(scope),
            scope_level=GeographyLevel.POLLING_UNIT,
            assignment__role_id__in=roles,
            assignment__status=AssignmentStatus.ACTIVE,
            assignment__starts_at__lte=now,
            assignment__member__verification_status=VerificationStatus.VERIFIED,
            assignment__member__user__is_active=True,
        )
        .filter(Q(assignment__ends_at__isnull=True) | Q(assignment__ends_at__gt=now))
        .exclude(assignment__member__membership_state__in=STOPPED)
        .values_list(
            "assignment__member__user_id", "polling_unit_id", "assignment__restricted_permissions"
        )
    )
    for user_id, unit_id, restricted in appointed:
        # Restrictions belong to this appointment and its own areas. A null
        # restriction keeps the role's permissions; an empty list grants none.
        if restricted is not None and C.FIELD_DUTY not in restricted:
            continue
        workers.setdefault(user_id, unit_id)
    return workers


def _require_standing(user):
    if not in_good_standing(user):
        raise PermissionDenied("Your membership is suspended; field reporting is off.")
    if not identity_verified(user):
        raise PermissionDenied(NOT_VERIFIED)


@transaction.atomic
def check_in(*, agent, gps, client_id=None) -> AgentCheckIn:
    """An agent or polling unit executive arriving at their polling unit."""
    _require_field_duty(agent)
    # Read the unit now: its location may have just been confirmed.
    unit = duty_polling_unit(agent)
    if unit is None:
        raise PermissionDenied("Checking in is for someone with a role at one polling unit.")
    if not gps:
        raise ValidationError({"gps": "Turn on location to check in."})
    if client_id:
        existing = AgentCheckIn.objects.filter(client_id=client_id, agent=agent).first()
        if existing is not None:
            return existing
    check = AgentCheckIn(agent=agent, polling_unit=unit, client_id=client_id)
    check.apply_fix(gps, unit)
    check.save()
    return check


@transaction.atomic
def confirm_check_in(*, actor, check_in: AgentCheckIn) -> AgentCheckIn:
    """A coordinator vouches for a check-in. If the polling unit has no
    location yet, this check-in's GPS becomes the unit's location."""
    check = (
        AgentCheckIn.objects.select_for_update().select_related("polling_unit").get(pk=check_in.pk)
    )
    unit = check.polling_unit
    unit_scope = ScopeRef.resolve("pollingUnit", unit.code)
    if not scope_contains(actor, unit_scope, permission=Capability.CONFIRM_AGENT_LOCATION):
        raise PermissionDenied("This polling unit is outside your operational scope.")
    if check.agent_id == actor.pk:
        raise PermissionDenied("You cannot confirm your own check-in.")
    if check.confirmed_at:
        raise ValidationError("This check-in is already confirmed.")
    if not in_good_standing(check.agent):
        raise ValidationError("This agent's membership is suspended; the check-in cannot count.")
    if not identity_verified(check.agent):
        raise ValidationError("This agent's identity is not verified; the check-in cannot count.")
    if check.location_status in (LocationStatus.SUSPECT, LocationStatus.NO_GPS):
        raise ValidationError("A check-in with a faked or missing location cannot be confirmed.")

    now = timezone.now()
    if unit.latitude is None:
        if check.gps_accuracy_m is None or check.gps_accuracy_m > settings.GPS_MAX_ACCURACY_M:
            raise ValidationError(
                "This GPS reading is not accurate enough to fix the polling unit's location. "
                "Ask the agent to check in again outdoors."
            )
        unit.latitude, unit.longitude = check.latitude, check.longitude
        unit.location_source = "checkIn"
        unit.location_note = ""
        unit.location_accuracy_m = check.gps_accuracy_m
        unit.location_confirmed_by = actor
        unit.location_confirmed_at = now
        unit.mapping_status = "pinned"
        unit.save(
            update_fields=[
                "latitude",
                "longitude",
                "location_source",
                "location_note",
                "location_accuracy_m",
                "location_confirmed_by",
                "location_confirmed_at",
                "mapping_status",
            ]
        )
    check.location_status, check.distance_to_pu_m = assess(
        unit, check.latitude, check.longitude, check.gps_accuracy_m, check.gps_is_mocked
    )
    check.confirmed_by = actor
    check.confirmed_at = now
    check.save()
    return check


def _device_time(at):
    """Phone time for an offline duty change, clamped to the last day and now."""
    now = timezone.now()
    if at is None or at > now:
        return now
    return max(at, now - timedelta(days=1))


PROJECT_ONLY = "Location is shared only during a project you take part in."


def end_participation_duty(*, participation, at) -> None:
    """Stop sharing location for this project work (the person withdrew or was removed)."""
    for session in DutySession.objects.filter(participation=participation, ended_at__isnull=True):
        session.ended_at = max(at, session.started_at)
        session.save(update_fields=["ended_at"])


def end_project_duty(*, project, at) -> None:
    for session in DutySession.objects.filter(
        participation__project=project, ended_at__isnull=True
    ):
        session.ended_at = max(at, session.started_at)
        session.save(update_fields=["ended_at"])


def close_finished_sessions(user=None) -> None:
    """End sharing whose project has finished: location stops with the project."""
    now = timezone.now()
    sessions = (
        DutySession.objects.filter(ended_at__isnull=True, participation__isnull=False)
        .filter(
            Q(participation__project__ends_at__lte=now)
            | Q(participation__project__cancelled_at__isnull=False)
        )
        .select_related("participation__project")
    )
    if user is not None:
        sessions = sessions.filter(user=user)
    for session in sessions:
        finished = min(session.participation.project.finished_at, now)
        session.ended_at = max(finished, session.started_at)
        session.save(update_fields=["ended_at"])


def start_duty(*, user, gps=None, at=None) -> DutySession:
    """Start sharing location for the project the person takes part in now.
    Their app does this by itself while the project runs; outside a project
    nobody shares location. `at` is the phone time, for changes synced later."""
    from apps.projects.services import current_participation

    _require_standing(user)
    close_finished_sessions(user)
    started = _device_time(at)
    participation = current_participation(user, started) or current_participation(user)
    if participation is None:
        raise PermissionDenied(PROJECT_ONLY)
    started = max(started, participation.project.starts_at)
    session = DutySession.objects.filter(user=user, ended_at__isnull=True).first()
    if session is not None and session.participation_id != participation.pk:
        session.ended_at = max(started, session.started_at)  # sharing for something else
        session.save(update_fields=["ended_at"])
        session = None
    if session is None:
        try:
            with transaction.atomic():
                session = DutySession.objects.create(
                    user=user, started_at=started, participation=participation
                )
        except IntegrityError:  # two "start" calls at once
            session = DutySession.objects.get(user=user, ended_at__isnull=True)
    if gps:
        record_pings(user=user, fixes=[gps])
    return session


def stop_duty(*, user, at=None) -> DutySession | None:
    session = DutySession.objects.filter(user=user, ended_at__isnull=True).first()
    if session:
        session.ended_at = max(_device_time(at), session.started_at)
        session.save(update_fields=["ended_at"])
    return session


def _place_unit(session, user):
    """Where a ping is judged: the polling unit the person works at in the
    project (none when the project does not check places), or for sharing
    from before projects, their own polling unit."""
    participation = session.participation
    if participation is None:
        return duty_polling_unit(user)
    if not participation.project.check_place:
        return None
    return participation.polling_unit if participation.polling_unit_id else None


@transaction.atomic
def record_pings(*, user, fixes) -> int:
    """Store queued pings that fall inside one of the user's duty sessions.

    Pings outside any session are dropped: location is only shared on duty,
    which ends with the project.
    """
    _require_standing(user)
    close_finished_sessions(user)
    if not fixes:
        return 0
    earliest = min(f["captured_at"] for f in fixes)
    sessions = list(
        DutySession.objects.filter(user=user, started_at__lte=timezone.now())
        .exclude(ended_at__lt=earliest - timedelta(minutes=1))
        .select_related("participation__project", "participation__polling_unit")
    )
    places = {}
    pings = []
    for fix in fixes:
        captured = fix["captured_at"]
        session = next(
            (
                s
                for s in sessions
                # Allow a minute of clock drift between phone and server.
                if s.started_at - timedelta(minutes=1) <= captured
                and (s.ended_at is None or captured <= s.ended_at + timedelta(minutes=1))
            ),
            None,
        )
        if session is None:
            continue
        if session.pk not in places:
            places[session.pk] = _place_unit(session, user)
        ping = LocationPing(session=session)
        ping.apply_fix(fix, places[session.pk])
        pings.append(ping)
    LocationPing.objects.bulk_create(pings)
    return len(pings)


def purge_old_pings(days: int | None = None) -> int:
    days = days or settings.LOCATION_PING_RETENTION_DAYS
    deleted, _ = LocationPing.objects.filter(
        received_at__lt=timezone.now() - timedelta(days=days)
    ).delete()
    return deleted

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
        .values_list("assignment__member__user_id", "polling_unit_id")
    )
    for user_id, unit_id in appointed:
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


def start_duty(*, user, gps=None, at=None) -> DutySession:
    """Start sharing location. `at` is the phone time, for changes synced later."""
    _require_field_duty(user)
    started = _device_time(at)
    try:
        with transaction.atomic():
            session, created = DutySession.objects.get_or_create(
                user=user, ended_at__isnull=True, defaults={"started_at": started}
            )
    except IntegrityError:  # two "start" taps at once
        session = DutySession.objects.get(user=user, ended_at__isnull=True)
    if gps:
        record_pings(user=user, fixes=[gps])
    return session


def stop_duty(*, user, at=None) -> DutySession | None:
    _require_field_duty(user)
    session = DutySession.objects.filter(user=user, ended_at__isnull=True).first()
    if session:
        session.ended_at = max(_device_time(at), session.started_at)
        session.save(update_fields=["ended_at"])
    return session


@transaction.atomic
def record_pings(*, user, fixes) -> int:
    """Store queued pings that fall inside one of the user's duty sessions.

    Pings outside any session are dropped: location is only shared on duty.
    """
    _require_field_duty(user)
    if not fixes:
        return 0
    earliest = min(f["captured_at"] for f in fixes)
    sessions = list(
        DutySession.objects.filter(user=user, started_at__lte=timezone.now()).exclude(
            ended_at__lt=earliest - timedelta(minutes=1)
        )
    )
    unit = duty_polling_unit(user)
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
        ping = LocationPing(session=session)
        ping.apply_fix(fix, unit)
        pings.append(ping)
    LocationPing.objects.bulk_create(pings)
    return len(pings)


def purge_old_pings(days: int | None = None) -> int:
    days = days or settings.LOCATION_PING_RETENTION_DAYS
    deleted, _ = LocationPing.objects.filter(
        received_at__lt=timezone.now() - timedelta(days=days)
    ).delete()
    return deleted

"""Check-ins, duty sessions, location pings, and learning polling-unit locations."""

from datetime import timedelta

from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.core.choices import Role
from apps.core.permissions import Capability
from apps.geography.gps import LocationStatus, assess
from apps.geography.models import PollingUnit
from apps.geography.scope import ScopeRef, scope_contains

from .models import AgentCheckIn, DutySession, LocationPing

FIELD_APP_ROLES = (Role.POLLING_UNIT_AGENT, Role.OBSERVER)


def _require_field_role(user):
    if user.role not in FIELD_APP_ROLES:
        raise PermissionDenied("Only field agents and observers share location from the app.")


@transaction.atomic
def check_in(*, agent, gps, client_id=None) -> AgentCheckIn:
    if agent.role != Role.POLLING_UNIT_AGENT or agent.polling_unit_id is None:
        raise PermissionDenied("Only polling-unit agents check in at a polling unit.")
    if not gps:
        raise ValidationError({"gps": "Turn on location to check in."})
    if client_id:
        existing = AgentCheckIn.objects.filter(client_id=client_id, agent=agent).first()
        if existing is not None:
            return existing
    # Re-read the unit: its location may have just been confirmed.
    unit = PollingUnit.objects.get(pk=agent.polling_unit_id)
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
        unit.location_accuracy_m = check.gps_accuracy_m
        unit.location_confirmed_by = actor
        unit.location_confirmed_at = now
        unit.mapping_status = "pinned"
        unit.save(
            update_fields=[
                "latitude",
                "longitude",
                "location_source",
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
    _require_field_role(user)
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
    _require_field_role(user)
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
    _require_field_role(user)
    if not fixes:
        return 0
    earliest = min(f["captured_at"] for f in fixes)
    sessions = list(
        DutySession.objects.filter(user=user, started_at__lte=timezone.now()).exclude(
            ended_at__lt=earliest - timedelta(minutes=1)
        )
    )
    unit = PollingUnit.objects.filter(pk=user.polling_unit_id).first()
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

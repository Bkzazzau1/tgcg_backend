"""Sending incidents to security officers, and their answers. All changes go through here.

The Situation Room (one grant holding its view and the right to assign
incidents, over the incident's place) sends an open incident to one officer
at a time, picked from those nearest to it. Distance runs from the incident's
GPS fix, else its place, to the officer's live location while they share it
for a project, else to the area of their role. Each step is on the incident's
trail, and the incident follows: escalated when sent, resolved when the
officer resolves it.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Avg, Q
from django.utils import timezone

from apps.access import engine
from apps.access.models import AssignmentStatus, RoleAssignment, RoleDefinition, RoleStatus
from apps.core.choices import IncidentStatus, MembershipState, VerificationStatus
from apps.core.permissions import Capability
from apps.field.models import Incident, IncidentEvent
from apps.geography.gps import distance_m
from apps.geography.models import PollingUnit
from apps.messaging.services import notify
from apps.tracking.models import LocationPing
from apps.tracking.services import close_finished_sessions

from .models import ACTIVE, SecurityDispatch

C = Capability
I = IncidentStatus  # noqa: E741
S = SecurityDispatch.Status
LocatedBy = SecurityDispatch.LocatedBy

# What the officer may say next, from each step.
NEXT = {
    S.SENT: (S.ACCEPTED, S.DECLINED),
    S.ACCEPTED: (S.ON_THE_WAY, S.ARRIVED, S.RESOLVED),
    S.ON_THE_WAY: (S.ARRIVED, S.RESOLVED),
    S.ARRIVED: (S.RESOLVED,),
}
NOTE_REQUIRED = {S.DECLINED: "Say why you decline.", S.RESOLVED: "Say what was done."}
STEP_TIME = {S.ACCEPTED: "accepted_at", S.ON_THE_WAY: "on_the_way_at", S.ARRIVED: "arrived_at"}
FINISHED_INCIDENT = (I.RESOLVED, I.CLOSED)
NEAREST = 10

# From a polling unit up to each kind of area.
UNIT_PATH = {
    "polling_unit": "pk",
    "ward": "ward_id",
    "lga": "ward__lga_id",
    "senatorial_district": "ward__lga__senatorial_district_id",
    "state": "ward__lga__state_id",
    "zone": "ward__lga__state__zone_id",
}
# A polling unit or ward with no located unit borrows its LGA's middle;
# anything wider would put people far from where they are.
BORROW_FROM = {"polling_unit": ("ward", "lga"), "ward": ("lga",)}


# --- who the officers are, and where ----------------------------------------------


@dataclass
class Officer:
    user: object
    member: object
    roles: list[str] = field(default_factory=list)
    areas: list = field(default_factory=list)


def officers(user=None) -> list[Officer]:
    """Everyone who answers escalations now (or just `user`), with their areas.

    An appointment counts under the access engine's own rules: active and in
    date, to an active role holding the power, of a verified member with an
    active membership and account. A National Administrator holds every
    power but is not an officer.
    """
    now = timezone.now()
    roles = [
        role.pk
        for role in RoleDefinition.objects.filter(status=RoleStatus.ACTIVE, is_protected=False)
        if C.RESPOND_TO_SECURITY in role.permissions
    ]
    assignments = (
        RoleAssignment.objects.filter(
            role__in=roles,
            status=AssignmentStatus.ACTIVE,
            starts_at__lte=now,
            member__membership_state=MembershipState.ACTIVE,
            member__verification_status=VerificationStatus.VERIFIED,
            member__user__is_active=True,
        )
        .filter(Q(ends_at__isnull=True) | Q(ends_at__gt=now))
        .select_related("role", "member__user")
        .prefetch_related("areas")
        .order_by("pk")
    )
    if user is not None:
        assignments = assignments.filter(member__user=user)
    found: dict[int, Officer] = {}
    for assignment in assignments:
        areas = list(assignment.areas.all())
        if C.RESPOND_TO_SECURITY not in assignment.permission_set() or not areas:
            continue
        member = assignment.member
        officer = found.setdefault(member.user_id, Officer(user=member.user, member=member))
        officer.roles.append(assignment.role.name)
        officer.areas.extend(areas)
    return list(found.values())


def is_officer(user) -> bool:
    """Whether `user` answers escalations, by the access engine itself."""
    return any(
        not grant.protected and C.RESPOND_TO_SECURITY in grant.permissions
        for grant in engine.active_grants(user)
    )


class Places:
    """Where areas are, each worked out once."""

    def __init__(self):
        self._points: dict[tuple, tuple | None] = {}

    def point(self, area) -> tuple[float, float] | None:
        """A polling unit's own location, else the middle of the located
        polling units in the area (see BORROW_FROM). None for the whole
        country, or where nothing is located."""
        level = area.scope_unit_field
        if level is None:
            return None
        for upper in (level, *BORROW_FROM.get(level, ())):
            unit_id = getattr(area, f"{upper}_id", None)
            if unit_id is None:
                continue
            key = (upper, unit_id)
            if key not in self._points:
                self._points[key] = self._middle(upper, unit_id)
            if self._points[key] is not None:
                return self._points[key]
        return None

    @staticmethod
    def _middle(level, unit_id):
        middle = PollingUnit.objects.filter(
            latitude__isnull=False, longitude__isnull=False, **{UNIT_PATH[level]: unit_id}
        ).aggregate(lat=Avg("latitude"), lng=Avg("longitude"))
        if middle["lat"] is None:
            return None
        return float(middle["lat"]), float(middle["lng"])


def incident_point(incident, places=None) -> tuple[float, float] | None:
    """Where the incident is: the reporter's GPS fix, else its place."""
    if incident.latitude is not None and not incident.gps_is_mocked:
        return float(incident.latitude), float(incident.longitude)
    return (places or Places()).point(incident)


def live_points(user_ids) -> dict[int, tuple[tuple[float, float], datetime]]:
    """Where each of these people is, while they share their location for a
    project: their latest real fix from the last few minutes."""
    close_finished_sessions()
    since = timezone.now() - timedelta(minutes=settings.LIVE_STALE_MINUTES)
    pings = (
        LocationPing.objects.filter(
            session__user__in=user_ids,
            session__ended_at__isnull=True,
            gps_is_mocked=False,
            gps_captured_at__gte=since,
            received_at__gte=since,
            latitude__isnull=False,
            longitude__isnull=False,
        )
        .order_by("session__user_id", "-gps_captured_at")
        .values_list("session__user_id", "latitude", "longitude", "gps_captured_at")
    )
    points = {}
    for user_id, latitude, longitude, captured_at in pings:
        points.setdefault(user_id, ((float(latitude), float(longitude)), captured_at))
    return points


@dataclass
class Candidate:
    officer: Officer
    distance_m: int | None
    located_by: str
    area: object | None
    seen_at: datetime | None
    busy: bool


def _measure(officer, target, live, places, busy) -> Candidate:
    if live is not None:
        point, seen_at = live
        if target is None:
            return Candidate(officer, None, "", None, seen_at, busy)
        return Candidate(
            officer, round(distance_m(*target, *point)), LocatedBy.LIVE, None, seen_at, busy
        )
    best = None
    for area in officer.areas:
        point = places.point(area)
        if point is None or target is None:
            continue
        distance = round(distance_m(*target, *point))
        if best is None or distance < best[0]:
            best = (distance, area)
    if best is None:
        return Candidate(officer, None, "", officer.areas[0], None, busy)
    return Candidate(officer, best[0], LocatedBy.AREA, best[1], None, busy)


def _rank(incident, *, user=None, search="", limit=None) -> list[Candidate]:
    places = Places()
    target = incident_point(incident, places)
    declined = set(
        SecurityDispatch.objects.filter(incident=incident, status=S.DECLINED).values_list(
            "officer_id", flat=True
        )
    )
    found = [officer for officer in officers(user) if officer.user.pk not in declined]
    words = search.lower().split()
    if words:
        found = [
            officer
            for officer in found
            if all(
                word in f"{officer.user.full_name} {officer.user.access_id}".lower()
                for word in words
            )
        ]
    ids = [officer.user.pk for officer in found]
    live = live_points(ids)
    busy = set(
        SecurityDispatch.objects.filter(status__in=ACTIVE, officer_id__in=ids)
        .exclude(incident=incident)
        .values_list("officer_id", flat=True)
    )
    ranked = [
        _measure(officer, target, live.get(officer.user.pk), places, officer.user.pk in busy)
        for officer in found
    ]
    # Nearest first; those whose distance is unknown last, by name.
    ranked.sort(
        key=lambda c: (c.distance_m is None, c.distance_m or 0, c.officer.user.full_name.lower())
    )
    return ranked[:limit] if limit else ranked


# --- the Situation Room -----------------------------------------------------------


def can_send(user, incident) -> bool:
    """One grant holding the Situation Room and the right to assign incidents,
    over the incident's place."""
    return any(
        C.VIEW_SITUATION_ROOM in grant.permissions
        and C.ASSIGN_INCIDENT in grant.permissions
        and grant.covers(incident)
        for grant in engine.active_grants(user)
    )


def _require_sender(user, incident):
    if not can_send(user, incident):
        raise PermissionDenied("Only the Situation Room sends incidents to security officers.")


def nearest(*, actor, incident, search="", limit=NEAREST) -> list[Candidate]:
    """The officers nearest to the incident, for the Situation Room to pick from.
    Anyone who declined it is not offered again."""
    _require_sender(actor, incident)
    return _rank(incident, search=search, limit=limit)


def place_label(incident) -> str:
    units = [getattr(incident, name) for name in ("polling_unit", "ward", "lga", "state")]
    return ", ".join(unit.name for unit in units if unit is not None) or "Nigeria"


def _trail(incident, actor, text, note="", previous=None):
    IncidentEvent.objects.create(
        incident=incident,
        action=IncidentEvent.Action.SECURITY,
        from_status=previous or "",
        to_status=incident.status if previous else "",
        note=f"{text} {note}".strip() if note else text,
        actor=actor,
    )


def _sentence(text: str) -> str:
    return text if text.endswith((".", "!", "?")) else f"{text}."


def _tell(dispatch, sender, body) -> list[str]:
    """A notice to the officer, on their phone (app and SMS) and in their inbox."""
    member = getattr(dispatch.officer, "member", None)
    if member is None:
        return []
    return notify(
        sender=sender,
        member=member,
        title=f"Security: {dispatch.incident.reference}",
        body=f"TGCG security: {body}",
        record={"security": str(dispatch.uuid)},
    )


@transaction.atomic
def send(*, actor, incident, officer, note="") -> SecurityDispatch:
    """Send an open incident to one security officer, and tell them."""
    incident = Incident.objects.select_for_update().get(pk=incident.pk)
    _require_sender(actor, incident)
    note = (note or "").strip()
    if incident.status in FINISHED_INCIDENT:
        raise ValidationError({"incident": f"The incident is {incident.status}."})
    current = (
        SecurityDispatch.objects.filter(incident=incident, status__in=ACTIVE)
        .select_related("officer")
        .first()
    )
    if current is not None:
        raise ValidationError(
            {"officer": f"{current.officer.full_name} has this incident: cancel that first."}
        )
    if SecurityDispatch.objects.filter(
        incident=incident, officer=officer, status=S.DECLINED
    ).exists():
        raise ValidationError({"officer": f"{officer.full_name} declined this incident."})
    ranked = _rank(incident, user=officer)
    if not ranked:
        raise ValidationError({"officer": "Choose a security officer from the list."})
    candidate = ranked[0]
    try:
        with transaction.atomic():
            dispatch = SecurityDispatch.objects.create(
                incident=incident,
                officer=officer,
                sent_by=actor,
                note=note,
                distance_m=candidate.distance_m,
                located_by=candidate.located_by,
            )
    except IntegrityError as exc:
        raise ValidationError({"officer": "Someone else sent this incident just now."}) from exc

    previous = incident.status
    if previous != I.ESCALATED:
        incident.status = I.ESCALATED
        incident.save(update_fields=["status", "updated_at"])
    _trail(
        incident,
        actor,
        f"Sent to security officer {officer.full_name} ({officer.access_id}).",
        note,
        previous=previous if previous != incident.status else None,
    )
    dispatch.notified = _tell(
        dispatch,
        actor,
        f"{incident.reference}, {incident.category} ({incident.severity}) at "
        f"{place_label(incident)}. Open the TGCG app to accept or decline.",
    )
    dispatch.save(update_fields=["notified", "updated_at"])
    return dispatch


def _stand_down(dispatch, actor, reason):
    dispatch.status = S.CANCELLED
    dispatch.cancelled_by = actor
    dispatch.cancel_reason = reason
    dispatch.finished_at = timezone.now()
    dispatch.save()
    _tell(
        dispatch,
        actor,
        f"{dispatch.incident.reference} is cancelled: {_sentence(reason)} "
        "You are no longer needed there.",
    )


@transaction.atomic
def cancel(*, actor, dispatch, reason) -> SecurityDispatch:
    """The Situation Room takes the incident back from the officer."""
    incident = Incident.objects.select_for_update().get(pk=dispatch.incident_id)
    dispatch = SecurityDispatch.objects.select_for_update().get(pk=dispatch.pk)
    _require_sender(actor, incident)
    reason = (reason or "").strip()
    if not reason:
        raise ValidationError({"reason": "Say why it is cancelled."})
    if dispatch.status not in ACTIVE:
        raise ValidationError({"status": f"It is already {dispatch.get_status_display().lower()}."})
    _stand_down(dispatch, actor, reason)
    _trail(incident, actor, f"Cancelled for security officer {dispatch.officer.full_name}.", reason)
    return dispatch


def incident_finished(*, actor, incident):
    """The incident was resolved or closed in the Situation Room (the caller
    holds its lock): whoever still has it is told they are no longer needed."""
    for dispatch in SecurityDispatch.objects.select_for_update().filter(
        incident=incident, status__in=ACTIVE
    ):
        reason = f"The incident was {incident.status}."
        _stand_down(dispatch, actor, reason)
        name = dispatch.officer.full_name
        _trail(incident, actor, f"Security officer {name} stood down.", reason)


# --- the officer ------------------------------------------------------------------


@transaction.atomic
def respond(*, officer, dispatch, step, note="") -> SecurityDispatch:
    """The officer's answer: accept or decline, then on the way, arrived, resolved."""
    incident = Incident.objects.select_for_update().get(pk=dispatch.incident_id)
    dispatch = SecurityDispatch.objects.select_for_update().get(pk=dispatch.pk)
    if dispatch.officer_id != officer.pk:
        raise PermissionDenied("This incident was sent to another officer.")
    if not is_officer(officer):
        raise PermissionDenied("Your role does not include answering security escalations.")
    note = (note or "").strip()
    if step not in NEXT.get(dispatch.status, ()):
        now_is = dispatch.get_status_display().lower()
        raise ValidationError({"step": f"It is {now_is}: {S(step).label.lower()} does not follow."})
    if step in NOTE_REQUIRED and not note:
        raise ValidationError({"note": NOTE_REQUIRED[step]})

    now = timezone.now()
    dispatch.status = step
    if step in STEP_TIME:
        setattr(dispatch, STEP_TIME[step], now)
    if step in (S.DECLINED, S.RESOLVED):
        dispatch.finished_at = now
        dispatch.outcome = note
    dispatch.save()

    previous = incident.status
    if step == S.RESOLVED and incident.status not in FINISHED_INCIDENT:
        incident.status = I.RESOLVED
        incident.resolved_at = now
        incident.save(update_fields=["status", "resolved_at", "updated_at"])
    _trail(
        incident,
        officer,
        f"Security officer {officer.full_name}: {S(step).label.lower()}.",
        note,
        previous=previous if previous != incident.status else None,
    )
    return dispatch


def reporter_phone(incident) -> str:
    reporter = incident.reporter
    member = getattr(reporter, "member", None)
    return reporter.phone_number or (member.phone_number if member else "")

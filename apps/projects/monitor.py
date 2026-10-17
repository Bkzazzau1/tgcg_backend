"""Who is where in a project, for the coordinators watching it."""

from collections import Counter
from datetime import timedelta

from django.conf import settings
from django.db.models import OuterRef, Subquery
from django.utils import timezone

from apps.core.permissions import Capability
from apps.field.models import FieldReport, Incident
from apps.geography.gps import GpsReadSerializer, LocationStatus
from apps.geography.scope import SCOPE_SELECT_RELATED, any_of, scope_payload, within_scope_q
from apps.results.models import ResultSubmission
from apps.tracking.models import DutySession, LocationPing

from .models import Participation, ParticipationStatus, ProjectStatus

C = Capability
PS = ParticipationStatus
# Either one lets a coordinator watch the people in their areas.
WATCHING = (C.VIEW_LIVE_OPERATIONS, C.ASSIGN_PROJECT_PARTICIPANTS)
STATE = {
    LocationStatus.ON_SITE: "atPlace",
    LocationStatus.AWAY: "away",
    LocationStatus.UNVERIFIED: "unverified",
    LocationStatus.SUSPECT: "suspect",
    LocationStatus.NO_GPS: "noGps",
}


def watched_q(viewer, prefix=""):
    return any_of(*(within_scope_q(viewer, prefix=prefix, permission=code) for code in WATCHING))


def _state(participation, status, session, ping, stale_before) -> str:
    """Where someone stands: their answer until they take part, then what
    their latest shared location says about their place."""
    if participation.status != PS.ACCEPTED:
        return participation.status
    if status == ProjectStatus.SCHEDULED:
        return "waiting"
    if session is None:
        return "notStarted"
    if ping is None or (status == ProjectStatus.ACTIVE and ping.gps_captured_at < stale_before):
        return "notSharing"
    return STATE.get(ping.location_status, "unverified")


def presence(project, viewer, now=None) -> dict:
    """The people in the viewer's areas, each with their state, and how much
    work the project has brought in there."""
    from apps.tracking.services import close_finished_sessions

    close_finished_sessions()
    now = now or timezone.now()
    status = project.status_at(now)
    stale_before = now - timedelta(minutes=settings.LIVE_STALE_MINUTES)
    latest_session = DutySession.objects.filter(participation=OuterRef("pk")).order_by(
        "-started_at"
    )
    participations = list(
        Participation.objects.filter(project=project)
        .filter(watched_q(viewer))
        .select_related("member", *SCOPE_SELECT_RELATED)
        .annotate(session_id=Subquery(latest_session.values("pk")[:1]))
        .order_by("member__full_name", "id")
    )
    latest_ping = LocationPing.objects.filter(session=OuterRef("pk")).order_by("-gps_captured_at")
    sessions = {
        session.pk: session
        for session in DutySession.objects.filter(
            pk__in=[p.session_id for p in participations if p.session_id]
        ).annotate(ping_id=Subquery(latest_ping.values("pk")[:1]))
    }
    pings = LocationPing.objects.in_bulk([s.ping_id for s in sessions.values() if s.ping_id])
    counts = Counter()
    people = []
    for participation in participations:
        session = sessions.get(participation.session_id)
        ping = pings.get(session.ping_id) if session and session.ping_id else None
        state = _state(participation, status, session, ping, stale_before)
        counts[state] += 1
        people.append(
            {
                "participation": str(participation.uuid),
                "member": participation.member.membership_number,
                "name": participation.member.full_name,
                "place": scope_payload(participation),
                "status": participation.status,
                "state": state,
                "position": GpsReadSerializer(ping).data if ping else None,
                "sharing_since": session.started_at if session else None,
            }
        )
    area = watched_q(viewer)
    return {
        "project": str(project.uuid),
        "status": status,
        "counts": dict(counts),
        "people": people,
        "work": {
            "incidents": Incident.objects.filter(area, project=project).count(),
            "reports": FieldReport.objects.filter(area, project=project).count(),
            "results": ResultSubmission.objects.filter(area, project=project).count(),
        },
    }

"""Meetings: who may call, see, join and end them, and the LiveKit passes."""

import contextlib
import io
import json
import urllib.error
from datetime import timedelta

import jwt
import pytest
from django.core.exceptions import ValidationError
from django.utils import timezone
from rest_framework.test import APIClient

from apps.access.models import AuditEvent
from apps.accounts.models import User
from apps.core.choices import GeographyLevel, Role
from apps.core.permissions import FIELD_ROLES
from apps.geography.models import Lga, PollingUnit, State, Ward
from apps.meetings import livekit, services
from apps.meetings.models import Admission, Meeting
from conftest import PASSWORD, verified_member

pytestmark = pytest.mark.django_db

URL = "/api/v1/meetings/"
SECRET = "a-test-secret-long-enough-for-hs256-signing"


@pytest.fixture(autouse=True)
def calls(settings):
    settings.LIVEKIT_URL = "wss://tgcg.livekit.cloud"
    settings.LIVEKIT_API_KEY = "APItest"
    settings.LIVEKIT_API_SECRET = SECRET


@pytest.fixture
def unit():
    ward = Ward.objects.create(
        code="18-08-01", name="Ward 01", lga=Lga.objects.get(code="KD-KADUNA-NORTH")
    )
    return PollingUnit.objects.create(code="18-08-01-001", name="Unit 1", ward=ward)


@pytest.fixture
def sent(monkeypatch):
    """Requests this server sends to LiveKit."""
    requests = []

    def urlopen(request, timeout):
        requests.append(request)
        return contextlib.nullcontext(io.BytesIO(b"{}"))  # LiveKit answers with JSON

    monkeypatch.setattr(livekit.urllib.request, "urlopen", urlopen)
    return requests


def person(access_id, role, **scope):
    user = User.objects.create_user(
        access_id, PASSWORD, full_name=f"{access_id} name", role=role, **scope
    )
    if role in FIELD_ROLES:
        verified_member(user)  # agents act only once verified
    client = APIClient()
    client.force_authenticate(user)
    return client


def kaduna_coordinator():
    return person(
        "SC-KD",
        Role.STATE_COORDINATOR,
        scope_level=GeographyLevel.STATE,
        state=State.objects.get(code="KD"),
    )


def kaduna_north_coordinator():
    return person(
        "LC-KD",
        Role.LGA_COORDINATOR,
        scope_level=GeographyLevel.LGA,
        lga=Lga.objects.get(code="KD-KADUNA-NORTH"),
    )


def agent_at(unit):
    return person(
        "AG-1", Role.POLLING_UNIT_AGENT, scope_level=GeographyLevel.POLLING_UNIT, polling_unit=unit
    )


def call(client, **body):
    return client.post(URL, {"title": "Situation call", **body}, format="json")


def later(hours=2):
    return (timezone.now() + timedelta(hours=hours)).isoformat()


def references(client):
    return [meeting["reference"] for meeting in client.get(URL).json()["results"]]


def test_coordinators_call_meetings_for_their_own_areas(unit):
    state = kaduna_coordinator()
    made = call(state)
    assert made.status_code == 201, made.content
    meeting = made.json()
    assert meeting["reference"] == "MTG-000001"
    assert meeting["scope"]["stateId"] == "KD"  # their own area by default
    assert (meeting["kind"], meeting["audience"], meeting["status"]) == ("audio", "area", "live")
    assert meeting["host"] == "SC-KD"
    assert meeting["can_join"] and meeting["can_manage"] and meeting["calls_available"]
    assert AuditEvent.objects.get(action="meeting.created").actor.access_id == "SC-KD"

    assert call(state, scope={"level": "state", "stateId": "KN"}).status_code == 403
    past = (timezone.now() - timedelta(hours=1)).isoformat()
    assert call(state, starts_at=past).status_code == 400
    # Agents join meetings; they do not call them.
    assert call(agent_at(unit)).status_code == 403


def test_area_meetings_reach_the_people_inside_the_area(unit):
    reference = call(kaduna_coordinator()).json()["reference"]
    inside = [kaduna_north_coordinator(), agent_at(unit)]
    kano = person(
        "LC-KN",
        Role.LGA_COORDINATOR,
        scope_level=GeographyLevel.LGA,
        lga=Lga.objects.get(code="KN-AJINGI"),
    )
    national = person("NCO-1", Role.NATIONAL_COLLATION_OFFICER)
    for client in inside:
        assert references(client) == [reference]
        assert client.post(f"{URL}{reference}/join/").status_code == 200
    # Not Kano's, and not every national officer's either.
    for client in (kano, national):
        assert references(client) == []
        assert client.post(f"{URL}{reference}/join/").status_code == 404


def test_invite_only_meetings_reach_only_those_invited():
    state = kaduna_coordinator()
    national = person("NCO-1", Role.NATIONAL_COLLATION_OFFICER)
    local = kaduna_north_coordinator()
    assert call(state, audience="invited").status_code == 400
    unknown = call(state, audience="invited", invitees=["NOBODY-9"])
    assert unknown.status_code == 400
    assert "NOBODY-9" in str(unknown.json())

    made = call(state, audience="invited", invitees=["nco-1"])
    assert made.status_code == 201, made.content
    assert made.json()["invitees"] == ["NCO-1"]
    reference = made.json()["reference"]
    assert references(national) == [reference]
    assert national.post(f"{URL}{reference}/join/").status_code == 200
    assert references(local) == []


def test_joining_hands_out_a_short_pass_for_the_room(unit):
    state = kaduna_coordinator()
    agent = agent_at(unit)
    reference = call(state).json()["reference"]
    joined = agent.post(f"{URL}{reference}/join/")
    assert joined.status_code == 200, joined.content
    joining = joined.json()
    meeting = Meeting.objects.get(reference=reference)
    assert joining["url"] == "wss://tgcg.livekit.cloud"
    assert joining["room"] == f"tgcg-{meeting.uuid}"
    claims = jwt.decode(joining["token"], SECRET, algorithms=["HS256"])
    assert (claims["iss"], claims["sub"], claims["name"]) == ("APItest", "AG-1", "AG-1 name")
    assert claims["exp"] - claims["nbf"] == 30 * 60
    assert claims["video"] == {
        "room": joining["room"],
        "roomJoin": True,
        "canSubscribe": True,
        "canPublish": True,
        "canPublishData": True,
        "canPublishSources": ["microphone"],  # an audio meeting
    }

    agent.post(f"{URL}{reference}/join/")
    assert Admission.objects.get(meeting=meeting).passes == 2
    assert agent.get(f"{URL}{reference}/").json()["admitted"] == 1

    video = call(state, kind="video").json()["reference"]
    token = state.post(f"{URL}{video}/join/").json()["token"]
    claims = jwt.decode(token, SECRET, algorithms=["HS256"])
    assert claims["video"]["canPublishSources"] == ["microphone", "camera"]


def test_joining_opens_shortly_before_the_start_and_needs_livekit(settings):
    state = kaduna_coordinator()
    planned = call(state, starts_at=later()).json()
    assert planned["status"] == "scheduled" and planned["can_join"]
    refused = state.post(f"{URL}{planned['reference']}/join/")
    assert refused.status_code == 400
    assert "15 minutes" in str(refused.json())
    soon = call(state, starts_at=later(hours=0.2)).json()
    assert soon["status"] == "live"

    settings.LIVEKIT_API_SECRET = ""
    assert state.get(f"{URL}{soon['reference']}/").json()["calls_available"] is False
    unavailable = state.post(f"{URL}{soon['reference']}/join/")
    assert unavailable.status_code == 503
    assert unavailable.json() == {"detail": "Calls are not set up on the server yet."}
    assert not Admission.objects.exists()


def test_ending_closes_the_room_for_everyone(sent, django_capture_on_commit_callbacks):
    state = kaduna_coordinator()
    local = kaduna_north_coordinator()
    planned = call(state, starts_at=later()).json()["reference"]
    assert state.post(f"{URL}{planned}/end/").status_code == 400  # cancel it instead

    reference = call(state).json()["reference"]
    # Neither the host nor over the whole state.
    assert local.post(f"{URL}{reference}/end/").status_code == 403
    with django_capture_on_commit_callbacks(execute=True):
        ended = state.post(f"{URL}{reference}/end/")
    assert ended.status_code == 200, ended.content
    assert ended.json()["status"] == "ended"
    meeting = Meeting.objects.get(reference=reference)
    [request] = sent
    assert request.full_url == "https://tgcg.livekit.cloud/twirp/livekit.RoomService/DeleteRoom"
    assert json.loads(request.data) == {"room": f"tgcg-{meeting.uuid}"}
    bearer = request.get_header("Authorization").removeprefix("Bearer ")
    assert jwt.decode(bearer, SECRET, algorithms=["HS256"])["video"] == {"roomCreate": True}
    assert local.post(f"{URL}{reference}/join/").status_code == 400  # it has ended
    assert AuditEvent.objects.filter(action="meeting.ended").count() == 1


def test_cancelling_needs_a_reason(sent, django_capture_on_commit_callbacks):
    state = kaduna_coordinator()
    reference = call(state, starts_at=later()).json()["reference"]
    url = f"{URL}{reference}/cancel/"
    assert state.post(url, {}, format="json").status_code == 400
    with django_capture_on_commit_callbacks(execute=True):
        cancelled = state.post(url, {"reason": "Clashes with the INEC briefing"}, format="json")
    assert cancelled.status_code == 200, cancelled.content
    assert cancelled.json()["status"] == "cancelled"
    assert sent == []  # it never opened, so there is no room to close
    assert state.post(f"{URL}{reference}/join/").status_code == 400
    assert AuditEvent.objects.get(action="meeting.cancelled").reason == (
        "Clashes with the INEC briefing"
    )


def test_a_room_livekit_cannot_close_is_logged_not_raised(monkeypatch):
    def refuse(request, timeout):
        raise urllib.error.HTTPError(request.full_url, 404, "room not found", {}, None)

    monkeypatch.setattr(livekit.urllib.request, "urlopen", refuse)
    assert livekit.close_room("tgcg-never-opened") is False


@pytest.mark.parametrize("since", ["2026-13-01T00:00:00Z", "2026-02-31T00:00:00Z", "nonsense", ""])
def test_invalid_meeting_date_filter_is_a_validation_error(since):
    state = kaduna_coordinator()
    response = state.get(URL, {"since": since})
    assert response.status_code == 400
    assert "since" in response.json()


def test_meeting_date_filter_honours_the_requested_start():
    state = kaduna_coordinator()
    current = call(state).json()["reference"]
    planned = call(state, starts_at=later()).json()["reference"]
    response = state.get(URL, {"since": later(hours=1)})
    assert response.status_code == 200
    assert [meeting["reference"] for meeting in response.json()["results"]] == [planned]
    assert current not in [meeting["reference"] for meeting in response.json()["results"]]


@pytest.mark.parametrize("closed_at", ["ended_at", "cancelled_at"])
def test_a_stale_meeting_object_cannot_issue_a_pass_after_closure(closed_at, monkeypatch):
    state = kaduna_coordinator()
    reference = call(state).json()["reference"]
    meeting = Meeting.objects.get(reference=reference)
    Meeting.objects.filter(pk=meeting.pk).update(**{closed_at: timezone.now()})
    signed = []
    monkeypatch.setattr(livekit, "join_token", lambda **kwargs: signed.append(kwargs))
    with pytest.raises(ValidationError):
        services.admit(actor=meeting.host, meeting=meeting)
    assert signed == []
    assert not Admission.objects.exists()

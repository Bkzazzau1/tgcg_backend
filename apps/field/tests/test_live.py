"""Live incident video: the reporter broadcasts, those who see the incident
watch, and LiveKit's recording is copied onto the incident."""

import base64
import contextlib
import hashlib
import io
import json
from datetime import timedelta

import jwt
import pytest
from django.core.management import call_command
from django.utils import timezone

from apps.field import recordings
from apps.field.models import Evidence, LiveStream
from apps.field.tests.test_field import INCIDENTS, report
from apps.meetings import livekit

pytestmark = pytest.mark.django_db

SECRET = "a-test-secret-that-is-long-enough-for-hs256"
MP4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64


@pytest.fixture(autouse=True)
def media(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path


@pytest.fixture
def calls(monkeypatch, settings):
    """LiveKit's server API, as this server calls it."""
    settings.LIVEKIT_URL = "wss://tgcg.livekit.cloud"
    settings.LIVEKIT_API_KEY = "APIkey"
    settings.LIVEKIT_API_SECRET = SECRET
    sent = []
    answers = {"Egress/StartRoomCompositeEgress": {"egressId": "EG_1"}}

    def twirp(method, body, grant):
        sent.append((method, body, grant))
        return answers.get(method, {})

    monkeypatch.setattr(livekit, "_twirp", twirp)
    return sent


@pytest.fixture
def bucket(settings):
    settings.LIVE_RECORDING_S3_ENDPOINT = "https://s3.tgcg.test"
    settings.LIVE_RECORDING_S3_BUCKET = "tgcg-live"
    settings.LIVE_RECORDING_S3_ACCESS_KEY = "key"
    settings.LIVE_RECORDING_S3_SECRET = "secret"


def claims(token):
    return jwt.decode(token, SECRET, algorithms=["HS256"])["video"]


def go_live(client, reference):
    return client.post(f"{INCIDENTS}{reference}/live/start/")


def test_the_reporter_goes_live_and_it_is_recorded(agent, coordinator, calls, bucket):
    reference = report(agent).json()["reference"]
    started = go_live(agent, reference)
    assert started.status_code == 200, started.content
    body = started.json()
    stream = LiveStream.objects.get()
    assert body["room"] == stream.room_name and body["url"] == "wss://tgcg.livekit.cloud"
    assert body["stream"]["is_live"] is True and body["stream"]["recording"] == "recording"
    # The reporter only broadcasts their camera and microphone.
    grant = claims(body["token"])
    assert grant["canPublish"] is True and grant["canSubscribe"] is False
    assert grant["canPublishSources"] == ["camera", "microphone"]
    assert jwt.decode(body["token"], SECRET, algorithms=["HS256"])["sub"] == "AG-1"

    # LiveKit opens the room and records it to the bucket.
    (create, *_), (record, request, record_grant) = calls
    assert create == "RoomService/CreateRoom"
    assert record == "Egress/StartRoomCompositeEgress"
    assert record_grant == {"room": stream.room_name, "roomRecord": True}
    [output] = request["file_outputs"]
    assert output["filepath"] == f"live/{reference}/{stream.uuid}.mp4"
    assert output["s3"]["bucket"] == "tgcg-live"
    assert stream.egress_id == "EG_1"

    # Whoever sees the incident watches, unseen and without broadcasting.
    incident = coordinator.get(f"{INCIDENTS}{reference}/").json()
    assert incident["live"]["is_live"] is True
    watching = coordinator.post(f"{INCIDENTS}{reference}/live/watch/")
    assert watching.status_code == 200, watching.content
    grant = claims(watching.json()["token"])
    assert grant == {
        "room": stream.room_name,
        "roomJoin": True,
        "canPublishData": False,
        "canPublish": False,
        "canSubscribe": True,
        "hidden": True,
    }
    # Going live again picks up the same stream.
    assert go_live(agent, reference).json()["room"] == stream.room_name
    assert LiveStream.objects.count() == 1


def test_only_its_reporter_goes_live_and_outsiders_do_not_watch(agent, other_agent, lagos, calls):
    reference = report(agent).json()["reference"]
    assert go_live(other_agent, reference).status_code == 404  # not theirs to see
    go_live(agent, reference)
    assert lagos.post(f"{INCIDENTS}{reference}/live/watch/").status_code == 404


def test_ending_stops_the_recording_and_closes_the_room(
    agent, calls, bucket, django_capture_on_commit_callbacks
):
    reference = report(agent).json()["reference"]
    room = go_live(agent, reference).json()["room"]
    with django_capture_on_commit_callbacks(execute=True):
        ended = agent.post(f"{INCIDENTS}{reference}/live/end/")
    assert ended.status_code == 200, ended.content
    assert ended.json()["live"]["is_live"] is False
    assert ("Egress/StopEgress", {"egress_id": "EG_1"}, {"roomRecord": True}) in calls
    assert ("RoomService/DeleteRoom", {"room": room}, {"roomCreate": True}) in calls
    assert agent.post(f"{INCIDENTS}{reference}/live/watch/").status_code == 400


def test_a_finished_recording_is_copied_onto_the_incident(
    agent, coordinator, calls, bucket, monkeypatch, django_capture_on_commit_callbacks
):
    reference = report(agent).json()["reference"]
    go_live(agent, reference)
    with django_capture_on_commit_callbacks(execute=True):
        agent.post(f"{INCIDENTS}{reference}/live/end/")
    stream = LiveStream.objects.get()
    deleted = []
    monkeypatch.setattr(
        livekit,
        "recording",
        lambda egress: {"status": "EGRESS_COMPLETE", "fileResults": [{"filename": "x"}]},
    )
    monkeypatch.setattr(recordings, "download", lambda key: contextlib.nullcontext(io.BytesIO(MP4)))
    monkeypatch.setattr(recordings, "delete", deleted.append)

    out = io.StringIO()
    call_command("collect_recordings", stdout=out)
    assert "1 saved" in out.getvalue()
    evidence = Evidence.objects.get()
    assert evidence.type == "video" and evidence.incident.reference == reference
    assert evidence.size == len(MP4)
    assert evidence.content_hash == hashlib.sha256(MP4).hexdigest()
    stream.refresh_from_db()
    assert stream.recording == "saved" and stream.evidence == evidence
    assert deleted == [stream.recording_key]
    # On the incident for whoever sees it.
    shown = coordinator.get(f"{INCIDENTS}{reference}/").json()
    assert [item["type"] for item in shown["evidence"]] == ["video"]
    assert shown["live"]["recording"] == "saved"


def test_a_failed_recording_says_so(agent, calls, bucket, monkeypatch):
    reference = report(agent).json()["reference"]
    go_live(agent, reference)
    LiveStream.objects.update(ended_at="2027-02-20T10:00:00Z")
    monkeypatch.setattr(
        livekit, "recording", lambda egress: {"status": "EGRESS_FAILED", "error": "no space"}
    )
    call_command("collect_recordings", stdout=io.StringIO())
    stream = LiveStream.objects.get()
    assert stream.recording == "failed" and stream.recording_error == "no space"


def test_without_a_bucket_live_video_is_not_recorded(agent, calls):
    reference = report(agent).json()["reference"]
    started = go_live(agent, reference)
    assert started.json()["stream"]["recording"] == "off"
    assert calls == []


def test_without_livekit_there_is_no_live_video(agent):
    reference = report(agent).json()["reference"]
    assert go_live(agent, reference).status_code == 400


def test_livekit_events_must_be_signed(agent, client, calls):
    reference = report(agent).json()["reference"]
    room = go_live(agent, reference).json()["room"]
    body = json.dumps({"event": "room_finished", "room": {"name": room}}).encode()
    digest = base64.b64encode(hashlib.sha256(body).digest()).decode()
    url = "/api/v1/livekit/webhook/"

    forged = jwt.encode(
        {"iss": "APIkey", "sha256": digest}, "a-forged-secret-that-is-long-enough-too", "HS256"
    )
    refused = client.post(
        url, body, content_type="application/webhook+json", HTTP_AUTHORIZATION=forged
    )
    assert refused.status_code == 401
    assert LiveStream.objects.get().ended_at is None

    signed = jwt.encode({"iss": "APIkey", "sha256": digest}, SECRET, "HS256")
    taken = client.post(
        url, body, content_type="application/webhook+json", HTTP_AUTHORIZATION=signed
    )
    assert taken.status_code == 204
    assert LiveStream.objects.get().ended_at is not None


def test_a_stream_left_open_past_its_time_ends(
    agent, calls, settings, django_capture_on_commit_callbacks
):
    reference = report(agent).json()["reference"]
    room = go_live(agent, reference).json()["room"]
    # Started well before the limit, so a coarse clock cannot make it look new.
    LiveStream.objects.update(started_at=timezone.now() - timedelta(minutes=5))
    settings.LIVE_STREAM_MAX_MINUTES = 1
    with django_capture_on_commit_callbacks(execute=True):
        call_command("collect_recordings", stdout=io.StringIO())
    assert LiveStream.objects.get().ended_at is not None
    assert ("RoomService/DeleteRoom", {"room": room}, {"roomCreate": True}) in calls

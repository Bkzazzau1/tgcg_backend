"""Live incident video. Its reporter broadcasts from the phone through
LiveKit; whoever can see the incident watches; LiveKit records it to a
bucket when recording is set up, and the recording is copied onto the
incident as evidence. All changes go through here."""

import hashlib
import logging
import secrets
from datetime import timedelta

from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files import File
from django.db import transaction
from django.utils import timezone

from apps.core.choices import EvidenceType, IncidentStatus, RecordOrigin
from apps.core.permissions import Capability
from apps.geography.scope import scope_contains
from apps.meetings import livekit

from . import recordings
from .models import Evidence, Incident, IncidentEvent, LiveStream
from .services import can_add_evidence

logger = logging.getLogger(__name__)
C = Capability
R = LiveStream.Recording
# A pass is used once, to connect; LiveKit keeps the connection open after.
PASS_SECONDS = 30 * 60
# A room nobody joins (or everyone left) closes after this.
EMPTY_ROOM_SECONDS = 10 * 60
# LiveKit's states for a recording that is over, with its file written.
FINISHED = ("EGRESS_COMPLETE", "EGRESS_LIMIT_REACHED")
STILL_GOING = ("EGRESS_STARTING", "EGRESS_ACTIVE", "EGRESS_ENDING")


def latest_stream(incident) -> LiveStream | None:
    """The incident's most recent stream (from a prefetch when there is one)."""
    return max(incident.live_streams.all(), key=lambda s: s.started_at, default=None)


def current_stream(incident, now=None) -> LiveStream | None:
    stream = latest_stream(incident)
    return stream if stream and stream.is_live_at(now or timezone.now()) else None


def _pass(stream, user, *, publish: bool) -> dict:
    # Watchers get their own identity each time, so one person may watch
    # from two devices without pushing the other off.
    identity = user.access_id if publish else f"{user.access_id}~{secrets.token_hex(3)}"
    return {
        "stream": stream,
        "url": settings.LIVEKIT_URL,
        "token": livekit.stream_token(
            identity=identity,
            name=user.full_name or user.access_id,
            room=stream.room_name,
            publish=publish,
            seconds=PASS_SECONDS,
        ),
        "room": stream.room_name,
        "expires_at": timezone.now() + timedelta(seconds=PASS_SECONDS),
    }


def _require_livekit():
    if not livekit.configured():
        raise ValidationError("Live video is not set up on this server.")


def _start_recording(stream) -> None:
    """Ask LiveKit to record, when a bucket is set up. The video still goes
    live if this fails; the stream then says the recording failed."""
    if not recordings.configured():
        return
    key = f"live/{stream.incident.reference}/{stream.uuid}.mp4"
    try:
        livekit.open_room(stream.room_name, empty_timeout=EMPTY_ROOM_SECONDS)
        stream.egress_id = livekit.start_recording(
            stream.room_name, filepath=key, s3=recordings.upload_target()
        )
        stream.recording = R.RECORDING
        stream.recording_key = key
    except (livekit.LiveKitError, livekit.NotConfigured, ValueError) as error:
        logger.warning("Live video %s is not being recorded: %s", stream.uuid, error)
        stream.recording = R.FAILED
        stream.recording_error = str(error)[:300]
    stream.save(
        update_fields=["egress_id", "recording", "recording_key", "recording_error", "updated_at"]
    )


@transaction.atomic
def go_live(*, actor, incident) -> dict:
    """Its reporter (or an evidence manager over it) broadcasts the incident."""
    _require_livekit()
    incident = Incident.objects.select_for_update().get(pk=incident.pk)
    if not can_add_evidence(actor, incident):
        raise PermissionDenied("Only the incident's reporter can go live from it.")
    if incident.status == IncidentStatus.CLOSED:
        raise ValidationError("A closed incident cannot go live.")
    stream = current_stream(incident)
    if stream is not None and stream.started_by_id != actor.pk:
        raise ValidationError("Someone is already live from this incident.")
    if stream is None:
        stream = LiveStream.objects.create(incident=incident, started_by=actor)
        IncidentEvent.objects.create(
            incident=incident,
            action=IncidentEvent.Action.LIVE_VIDEO,
            note="Live video started.",
            actor=actor,
        )
        _start_recording(stream)
    return _pass(stream, actor, publish=True)


def watch(*, actor, incident) -> dict:
    """A pass to watch, for whoever can see the incident (the view checks)."""
    _require_livekit()
    stream = current_stream(incident)
    if stream is None:
        raise ValidationError({"live": "This incident is not live."})
    return _pass(stream, actor, publish=False)


def _close(stream, actor=None) -> None:
    stream.ended_at = timezone.now()
    stream.ended_by = actor
    stream.save(update_fields=["ended_at", "ended_by", "updated_at"])
    egress, room = stream.egress_id, stream.room_name

    def stop():
        if egress:
            try:
                livekit.stop_recording(egress)
            except (livekit.LiveKitError, livekit.NotConfigured) as error:
                logger.warning("LiveKit did not stop recording %s: %s", egress, error)
        livekit.close_room(room)

    transaction.on_commit(stop)


@transaction.atomic
def end_live(*, actor, incident) -> LiveStream:
    """Whoever went live, or a coordinator over the incident, ends it."""
    stream = current_stream(incident)
    if stream is None:
        raise ValidationError({"live": "This incident is not live."})
    stream = LiveStream.objects.select_for_update().get(pk=stream.pk)
    if stream.started_by_id != actor.pk and not scope_contains(
        actor, incident, permission=C.ACKNOWLEDGE_INCIDENT
    ):
        raise PermissionDenied("Only whoever went live, or a coordinator over it, ends it.")
    if stream.ended_at is None:
        _close(stream, actor)
    return stream


def handle_event(event: dict) -> None:
    """An event LiveKit posted: a room that closed, or a recording that ended."""
    kind = event.get("event")
    if kind == "room_finished":
        name = (event.get("room") or {}).get("name", "")
        if not name.startswith("tgcg-live-"):
            return  # a meeting's room
        with transaction.atomic():
            stream = (
                LiveStream.objects.select_for_update()
                .filter(uuid=name.removeprefix("tgcg-live-"), ended_at__isnull=True)
                .first()
            )
            if stream is not None:
                stream.ended_at = timezone.now()
                stream.save(update_fields=["ended_at", "updated_at"])
    elif kind == "egress_ended":
        info = event.get("egressInfo") or {}
        stream = LiveStream.objects.filter(
            egress_id=info.get("egressId", ""), recording=R.RECORDING
        ).first()
        if stream is not None and info.get("status") not in (*FINISHED, *STILL_GOING):
            _fail(stream, info.get("error") or info.get("status") or "Recording failed.")


def _fail(stream, reason: str) -> None:
    stream.recording = R.FAILED
    stream.recording_error = str(reason)[:300]
    stream.save(update_fields=["recording", "recording_error", "updated_at"])


def end_overdue(now=None) -> int:
    """End streams still open past their longest time: the phone went away."""
    now = now or timezone.now()
    limit = now - timedelta(minutes=settings.LIVE_STREAM_MAX_MINUTES)
    ended = 0
    for stream in LiveStream.objects.filter(ended_at__isnull=True, started_at__lt=limit):
        with transaction.atomic():
            _close(stream)
        ended += 1
    return ended


def needs_collecting():
    """Recordings to copy: their stream is over."""
    return (
        LiveStream.objects.filter(recording=R.RECORDING, ended_at__isnull=False)
        .exclude(egress_id="")
        .select_related("incident", "started_by")
    )


def collect(stream) -> str:
    """Copy a finished recording onto its incident: saved, failed or waiting."""
    info = livekit.recording(stream.egress_id) or {}
    status = info.get("status", "")
    if status in STILL_GOING:
        return "waiting"
    if status not in FINISHED or not info.get("fileResults"):
        _fail(stream, info.get("error") or status or "LiveKit has no record of the recording.")
        return "failed"
    incident = stream.incident
    name = f"{incident.reference}-live-{stream.started_at:%Y%m%d-%H%M}.mp4"
    with recordings.download(stream.recording_key) as handle:
        digest = hashlib.sha256()
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
        size = handle.tell()
        handle.seek(0)
        with transaction.atomic():
            evidence = Evidence(
                type=EvidenceType.VIDEO,
                file=File(handle, name=name),
                file_name=name,
                mime_type="video/mp4",
                size=size,
                content_hash=digest.hexdigest(),
                caption="Recording of the live video",
                uploader=stream.started_by,
                origin=RecordOrigin.SYSTEM_DERIVED,
                incident=incident,
            )
            evidence.full_clean()
            evidence.save()
            IncidentEvent.objects.create(
                incident=incident,
                action=IncidentEvent.Action.EVIDENCE_ADDED,
                note=f"Video: {name} (recording of the live video)",
                actor=stream.started_by,
            )
            stream.evidence = evidence
            stream.recording = R.SAVED
            if stream.ended_at is None:
                stream.ended_at = timezone.now()
            stream.save(update_fields=["evidence", "recording", "ended_at", "updated_at"])
    try:
        recordings.delete(stream.recording_key)
    except Exception as error:  # noqa: BLE001 - the copy is kept; the bucket can be tidied later
        logger.warning("The bucket kept recording %s: %s", stream.recording_key, error)
    return "saved"

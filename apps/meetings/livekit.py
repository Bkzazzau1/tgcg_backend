"""LiveKit, where the calls and live incident video run. This server signs
join passes, asks LiveKit to open, record and close rooms, and checks the
events LiveKit sends back; the audio and video go between the phones and
LiveKit, never through this server.

Set LIVEKIT_URL (wss://<project>.livekit.cloud), LIVEKIT_API_KEY and
LIVEKIT_API_SECRET from the LiveKit project's settings (docs/meetings.md).
"""

import base64
import hashlib
import hmac
import json
import logging
import time
import urllib.error
import urllib.request

import jwt
from django.conf import settings

logger = logging.getLogger(__name__)
TIMEOUT_SECONDS = 10


class NotConfigured(Exception):
    """Calls are not set up on this server."""


def configured() -> bool:
    return bool(settings.LIVEKIT_URL and settings.LIVEKIT_API_KEY and settings.LIVEKIT_API_SECRET)


def _token(claims: dict, seconds: int) -> str:
    """A LiveKit access token: a JWT signed with the API secret."""
    if not configured():
        raise NotConfigured
    now = int(time.time())
    return jwt.encode(
        {"iss": settings.LIVEKIT_API_KEY, "nbf": now, "exp": now + seconds, **claims},
        settings.LIVEKIT_API_SECRET,
        algorithm="HS256",
    )


def join_token(*, identity: str, name: str, room: str, video: bool, seconds: int) -> str:
    """A pass to join `room` as `identity` for `seconds`: listen, speak, and
    show a camera only in a video meeting."""
    return _token(
        {
            "sub": identity,
            "name": name,
            "video": {
                "room": room,
                "roomJoin": True,
                "canSubscribe": True,
                "canPublish": True,
                "canPublishData": True,
                "canPublishSources": ["microphone", "camera"] if video else ["microphone"],
            },
        },
        seconds,
    )


def stream_token(*, identity: str, name: str, room: str, publish: bool, seconds: int) -> str:
    """A pass for live incident video: the reporter only broadcasts their
    camera and microphone; a watcher only receives, unseen by others."""
    grant = {"room": room, "roomJoin": True, "canPublishData": False}
    if publish:
        grant |= {
            "canPublish": True,
            "canSubscribe": False,
            "canPublishSources": ["camera", "microphone"],
        }
    else:
        grant |= {"canPublish": False, "canSubscribe": True, "hidden": True}
    return _token({"sub": identity, "name": name, "video": grant}, seconds)


def _api_url() -> str:
    url = settings.LIVEKIT_URL.rstrip("/")
    for socket, web in (("wss://", "https://"), ("ws://", "http://")):
        if url.startswith(socket):
            return web + url[len(socket) :]
    return url


class LiveKitError(Exception):
    """LiveKit refused a request or could not be reached."""


def _twirp(method: str, body: dict, grant: dict) -> dict:
    """Call LiveKit's server API (Twirp over HTTPS, JSON)."""
    if not configured():
        raise NotConfigured
    request = urllib.request.Request(  # noqa: S310 - the configured LiveKit server
        f"{_api_url()}/twirp/livekit.{method}",
        data=json.dumps(body).encode(),
        headers={
            "Authorization": f"Bearer {_token({'video': grant}, 60)}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:  # noqa: S310
            text = response.read()
    except (urllib.error.URLError, TimeoutError) as error:
        raise LiveKitError(str(error)) from error
    return json.loads(text) if text else {}


def close_room(room: str) -> bool:
    """Ask LiveKit to end `room`, which disconnects everyone in it. False when
    it could not (calls are not set up, or nobody ever opened the room)."""
    if not configured():
        return False
    try:
        _twirp("RoomService/DeleteRoom", {"room": room}, {"roomCreate": True})
        return True
    except LiveKitError as error:
        logger.warning("LiveKit did not close room %s: %s", room, error)
        return False


def open_room(room: str, *, empty_timeout: int) -> None:
    """Create `room` now, so it can be recorded before anyone joins."""
    _twirp(
        "RoomService/CreateRoom",
        {"name": room, "empty_timeout": empty_timeout},
        {"roomCreate": True},
    )


def start_recording(room: str, *, filepath: str, s3: dict) -> str:
    """Record `room` to an MP4 in an S3-compatible bucket; the egress id."""
    info = _twirp(
        "Egress/StartRoomCompositeEgress",
        {
            "room_name": room,
            "layout": "speaker",
            "file_outputs": [{"file_type": "MP4", "filepath": filepath, "s3": s3}],
        },
        {"room": room, "roomRecord": True},
    )
    return info.get("egressId") or info.get("egress_id") or ""


def stop_recording(egress_id: str) -> None:
    _twirp("Egress/StopEgress", {"egress_id": egress_id}, {"roomRecord": True})


def recording(egress_id: str) -> dict | None:
    """What LiveKit knows of a recording: status, file results and any error."""
    body = _twirp("Egress/ListEgress", {"egress_id": egress_id}, {"roomRecord": True})
    items = body.get("items") or []
    return items[0] if items else None


def read_webhook(body: bytes, authorization: str) -> dict:
    """An event LiveKit posted, after checking it was signed with our API
    secret and is the body it signed. Raises LiveKitError otherwise."""
    if not configured():
        raise NotConfigured
    token = authorization.removeprefix("Bearer ").strip()
    try:
        claims = jwt.decode(
            token,
            settings.LIVEKIT_API_SECRET,
            algorithms=["HS256"],
            options={"verify_aud": False},
        )
    except jwt.PyJWTError as error:
        raise LiveKitError("The event is not signed by LiveKit.") from error
    digest = base64.b64encode(hashlib.sha256(body).digest()).decode()
    if claims.get("iss") != settings.LIVEKIT_API_KEY or not hmac.compare_digest(
        claims.get("sha256", ""), digest
    ):
        raise LiveKitError("The event does not match its signature.")
    return json.loads(body)

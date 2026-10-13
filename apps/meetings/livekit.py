"""LiveKit, where the calls run. This server only signs join passes and asks
LiveKit to close a room; the audio and video go between the phones and
LiveKit, never through this server.

Set LIVEKIT_URL (wss://<project>.livekit.cloud), LIVEKIT_API_KEY and
LIVEKIT_API_SECRET from the LiveKit project's settings (docs/meetings.md).
"""

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


def _api_url() -> str:
    url = settings.LIVEKIT_URL.rstrip("/")
    for socket, web in (("wss://", "https://"), ("ws://", "http://")):
        if url.startswith(socket):
            return web + url[len(socket) :]
    return url


def close_room(room: str) -> bool:
    """Ask LiveKit to end `room`, which disconnects everyone in it. False when
    it could not (calls are not set up, or nobody ever opened the room)."""
    if not configured():
        return False
    request = urllib.request.Request(  # noqa: S310 - the configured LiveKit server
        f"{_api_url()}/twirp/livekit.RoomService/DeleteRoom",
        data=json.dumps({"room": room}).encode(),
        headers={
            "Authorization": f"Bearer {_token({'video': {'roomCreate': True}}, 60)}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS):  # noqa: S310
            return True
    except (urllib.error.URLError, TimeoutError) as error:
        logger.warning("LiveKit did not close room %s: %s", room, error)
        return False

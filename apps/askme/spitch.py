"""Spitch: listening and speaking in English, Hausa, Yoruba, Igbo and Pidgin,
for Ask Me's voice (decided 25 October 2026).

Set SPITCH_API_KEY. `transcribe` writes down a spoken question; `speak` reads
an answer aloud as MP3. The key travels in a header and is never logged.
"""

import json
import logging
import urllib.error
import urllib.request
import uuid

from django.conf import settings

logger = logging.getLogger(__name__)

# Spitch's language for each of Ask Me's: Pidgin is read and heard as English,
# with Pidgin voices.
SPITCH_LANGUAGE = {"en": "en", "ha": "ha", "yo": "yo", "ig": "ig", "pcm": "en"}

# Spitch's voices by the language they were made for.
VOICES = {
    "en": ["john", "lucy", "lina", "jude", "henry", "kani", "kingsley", "remi"],
    "ha": ["hasan", "amina", "zainab", "aliyu"],
    "yo": ["sade", "funmi", "segun", "femi"],
    "ig": ["obinna", "ngozi", "amara", "ebuka"],
    "pcm": ["justice", "boma", "tega", "ufoma"],
}
DEFAULT_VOICES = {"en": "jude", "ha": "aliyu", "yo": "segun", "ig": "obinna", "pcm": "justice"}


class NotConfigured(Exception):
    pass


class SpitchError(RuntimeError):
    """Spitch answered with an error, or could not be reached."""


def configured() -> bool:
    return bool(settings.SPITCH_API_KEY)


def _send(path: str, *, data: bytes, content_type: str, accept: str):
    if not configured():
        raise NotConfigured
    request = urllib.request.Request(  # noqa: S310 - https URL from settings
        f"{settings.SPITCH_BASE_URL.rstrip('/')}{path}",
        data=data,
        method="POST",
        headers={
            "Authorization": f"Bearer {settings.SPITCH_API_KEY}",
            "Content-Type": content_type,
            "Accept": accept,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=settings.ASKME_VOICE_TIMEOUT) as response:  # noqa: S310
            return response.read()
    except urllib.error.HTTPError as error:
        logger.warning("Spitch refused a request: HTTP %s", error.code)
        raise SpitchError(f"The voice service answered HTTP {error.code}.") from error
    except (urllib.error.URLError, TimeoutError) as error:
        logger.warning("Spitch could not be reached: %s", type(error).__name__)
        raise SpitchError("The voice service could not be reached.") from error


def transcribe(audio: bytes, *, filename: str, mime_type: str, language: str) -> str:
    """The words spoken in `audio` ("" when none were heard)."""
    boundary = f"tgcg-{uuid.uuid4().hex}"
    parts = [
        (
            f'--{boundary}\r\nContent-Disposition: form-data; name="language"\r\n\r\n'
            f"{SPITCH_LANGUAGE.get(language, 'en')}\r\n"
        ).encode(),
        (
            f'--{boundary}\r\nContent-Disposition: form-data; name="content"; '
            f'filename="{filename}"\r\nContent-Type: {mime_type}\r\n\r\n'
        ).encode()
        + audio
        + b"\r\n",
        f"--{boundary}--\r\n".encode(),
    ]
    raw = _send(
        "/v1/transcriptions",
        data=b"".join(parts),
        content_type=f"multipart/form-data; boundary={boundary}",
        accept="application/json",
    )
    try:
        reply = json.loads(raw)
    except ValueError as error:
        raise SpitchError("The voice service's reply could not be read.") from error
    text = reply.get("text") if isinstance(reply, dict) else None
    return " ".join(str(text or "").split())


def speak(text: str, *, language: str, voice: str) -> bytes:
    """`text` read aloud, as MP3."""
    body = {
        "text": text,
        "language": SPITCH_LANGUAGE.get(language, "en"),
        "voice": voice,
        "format": "mp3",
    }
    audio = _send(
        "/v1/speech",
        data=json.dumps(body).encode(),
        content_type="application/json",
        accept="audio/mpeg",
    )
    if not audio:
        raise SpitchError("The voice service returned no sound.")
    return audio

"""Google's Gemini API, used by Ask Me to write answers from the library.

Set GEMINI_API_KEY (a paid-tier key: Google does not use paid prompts to
improve its products) and, if needed, ASKME_GEMINI_MODEL. No tools are sent,
so Gemini cannot search the web: it sees only what Ask Me gives it. The key
travels in a header and is never logged.
"""

import json
import logging
import urllib.error
import urllib.request

from django.conf import settings

logger = logging.getLogger(__name__)


class NotConfigured(Exception):
    pass


class GeminiError(RuntimeError):
    """Gemini answered with an error, or with nothing usable."""


class Blocked(RuntimeError):
    """Gemini's own safety rules stopped the answer."""


def configured() -> bool:
    return bool(settings.GEMINI_API_KEY)


def generate_json(*, system: str, text: str, schema: dict, temperature: float = 0.3) -> dict:
    """One request; returns the JSON object Gemini wrote to match `schema`."""
    if not configured():
        raise NotConfigured
    model = settings.ASKME_GEMINI_MODEL
    url = f"{settings.GEMINI_BASE_URL.rstrip('/')}/models/{model}:generateContent"
    body = {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": text}]}],
        "generationConfig": {
            "temperature": temperature,
            "responseMimeType": "application/json",
            "responseSchema": schema,
        },
    }
    request = urllib.request.Request(  # noqa: S310 - https URL from settings
        url,
        data=json.dumps(body).encode(),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "x-goog-api-key": settings.GEMINI_API_KEY,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=settings.ASKME_GEMINI_TIMEOUT) as response:  # noqa: S310
            reply = json.loads(response.read())
    except urllib.error.HTTPError as error:
        logger.warning("Gemini refused a request: HTTP %s", error.code)
        raise GeminiError(f"Gemini answered HTTP {error.code}.") from error
    except (urllib.error.URLError, TimeoutError, ValueError) as error:
        logger.warning("Gemini could not be reached: %s", type(error).__name__)
        raise GeminiError("Gemini could not be reached.") from error

    if (reply.get("promptFeedback") or {}).get("blockReason"):
        raise Blocked
    candidates = reply.get("candidates") or []
    if not candidates:
        raise GeminiError("Gemini returned no answer.")
    candidate = candidates[0]
    if candidate.get("finishReason") in ("SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII"):
        raise Blocked
    parts = (candidate.get("content") or {}).get("parts") or []
    written = "".join(part.get("text", "") for part in parts)
    try:
        result = json.loads(written)
    except ValueError as error:
        raise GeminiError("Gemini's answer was not the expected JSON.") from error
    if not isinstance(result, dict):
        raise GeminiError("Gemini's answer was not the expected JSON.")
    return result

"""Outbound SMS. Select with settings.SMS_BACKEND ("console" or "termii")."""

import json
import logging
import urllib.error
import urllib.request

from django.conf import settings

logger = logging.getLogger(__name__)


class SmsError(RuntimeError):
    pass


def send_sms(phone: str, message: str) -> None:
    backend = settings.SMS_BACKEND
    if backend == "console":
        # Development only: the code appears in the server log.
        logger.warning("SMS to %s: %s", phone, message)
        return
    if backend == "termii":
        _send_termii(phone, message)
        return
    raise SmsError(f"Unknown SMS_BACKEND {backend!r}")


def _send_termii(phone: str, message: str) -> None:
    """Termii "send message" API. Uses the DND route so codes reach DND numbers."""
    if not settings.TERMII_API_KEY:
        raise SmsError("TERMII_API_KEY is not configured.")
    payload = {
        "api_key": settings.TERMII_API_KEY,
        "to": phone.lstrip("+"),
        "from": settings.TERMII_SENDER_ID,
        "sms": message,
        "type": "plain",
        "channel": "dnd",
    }
    request = urllib.request.Request(  # noqa: S310 - https URL from settings
        f"{settings.TERMII_BASE_URL.rstrip('/')}/api/sms/send",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:  # noqa: S310 - fixed https URL
            if response.status >= 300:
                raise SmsError(f"Termii returned HTTP {response.status}")
    except urllib.error.URLError as exc:
        logger.exception("Termii SMS failed for %s", phone)
        raise SmsError("The SMS could not be sent. Try again shortly.") from exc

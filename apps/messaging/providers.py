"""The services that carry messages, chosen in settings.

- SMS: SMS_BACKEND ("termii" sends; "console" only records).
- WhatsApp: WHATSAPP_BACKEND ("termii" or "console"). Termii must have a
  WhatsApp number connected for the sender; WhatsApp may also require an
  approved template for messages that start a conversation.
- App notifications: PUSH_BACKEND ("fcm", Firebase Cloud Messaging with the
  service account key in FIREBASE_SERVICE_ACCOUNT_FILE, or "console").

The console backends keep what they would have sent in `outbox`, for tests
and development. Phone numbers, tokens and message text are never logged.
"""

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import jwt
from django.conf import settings

logger = logging.getLogger(__name__)
TIMEOUT_SECONDS = 30
FCM_SCOPE = "https://www.googleapis.com/auth/firebase.messaging"

# What the console backends would have sent: (channel, to, text).
outbox: list[tuple[str, object, str]] = []


def ready(channel: str) -> bool:
    """Whether messages on this channel can go out: its service is set up, or
    (development and tests) the console backend records them."""
    backend, real, credential = {
        "sms": (settings.SMS_BACKEND, "termii", settings.TERMII_API_KEY),
        "whatsapp": (settings.WHATSAPP_BACKEND, "termii", settings.TERMII_API_KEY),
        "push": (settings.PUSH_BACKEND, "fcm", settings.FIREBASE_SERVICE_ACCOUNT_FILE),
    }[channel]
    return backend == "console" or (backend == real and bool(credential))


class ProviderError(RuntimeError):
    """The service refused or could not be reached; the message says which."""


class InvalidToken(ProviderError):  # noqa: N818 - reads as the reason
    """The phone no longer receives notifications for this app."""


def _post(url, body: bytes, headers: dict) -> tuple[int, dict]:
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")  # noqa: S310
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:  # noqa: S310
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as error:
        try:
            detail = json.loads(error.read() or b"{}")
        except ValueError:
            detail = {}
        return error.code, detail
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise ProviderError(f"Could not reach the service ({type(error).__name__}).") from error


# --- Termii (SMS and WhatsApp) ---------------------------------------------------


def _termii(path: str, payload: dict) -> str:
    if not settings.TERMII_API_KEY:
        raise ProviderError("TERMII_API_KEY is not configured.")
    status, body = _post(
        f"{settings.TERMII_BASE_URL.rstrip('/')}{path}",
        json.dumps(
            {"api_key": settings.TERMII_API_KEY, "from": settings.TERMII_SENDER_ID, **payload}
        ).encode(),
        {"Content-Type": "application/json"},
    )
    if status >= 300:
        reason = str(body.get("message") or body.get("error") or f"HTTP {status}")
        logger.error("Termii refused a message: %s", reason[:120])
        raise ProviderError(f"Termii: {reason[:150]}")
    return str(body.get("message_id") or "")


def termii_number(phone: str) -> str:
    return phone.lstrip("+")


def send_sms_bulk(numbers: list[str], text: str) -> str:
    """One text to many numbers in one request; returns the service's reference."""
    backend = settings.SMS_BACKEND
    if backend == "console":
        outbox.append(("sms", list(numbers), text))
        logger.info("Bulk SMS recorded, not sent (console): %d numbers", len(numbers))
        return "console"
    if backend == "termii":
        payload = {
            "to": [termii_number(number) for number in numbers],
            "sms": text,
            "type": "plain",
            "channel": settings.TERMII_BULK_CHANNEL,
        }
        return _termii("/api/sms/send/bulk", payload)
    raise ProviderError(f"Unknown SMS_BACKEND {backend!r}.")


def send_whatsapp(number: str, text: str) -> str:
    backend = settings.WHATSAPP_BACKEND
    if backend == "console":
        outbox.append(("whatsapp", number, text))
        return "console"
    if backend == "termii":
        payload = {"to": termii_number(number), "sms": text, "type": "plain", "channel": "whatsapp"}
        return _termii("/api/sms/send", payload)
    raise ProviderError(f"Unknown WHATSAPP_BACKEND {backend!r}.")


# --- Firebase Cloud Messaging (app notifications) ----------------------------------

_access = {"token": "", "expires": 0.0}


def _service_account() -> dict:
    path = settings.FIREBASE_SERVICE_ACCOUNT_FILE
    if not path:
        raise ProviderError("FIREBASE_SERVICE_ACCOUNT_FILE is not configured.")
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ProviderError("The Firebase service account file cannot be read.") from error


def _access_token(account: dict) -> str:
    """A Google access token for sending, from the service account's key."""
    if _access["token"] and time.time() < _access["expires"] - 60:
        return _access["token"]
    now = int(time.time())
    token_uri = account.get("token_uri", "https://oauth2.googleapis.com/token")
    assertion = jwt.encode(
        {
            "iss": account["client_email"],
            "scope": FCM_SCOPE,
            "aud": token_uri,
            "iat": now,
            "exp": now + 3600,
        },
        account["private_key"],
        algorithm="RS256",
    )
    form = urllib.parse.urlencode(
        {"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": assertion}
    ).encode()
    status, body = _post(token_uri, form, {"Content-Type": "application/x-www-form-urlencoded"})
    if status >= 300 or "access_token" not in body:
        raise ProviderError(f"Google refused the service account (HTTP {status}).")
    _access["token"] = body["access_token"]
    _access["expires"] = time.time() + int(body.get("expires_in", 3600))
    return _access["token"]


def send_push(token: str, title: str, body: str, data: dict) -> str:
    backend = settings.PUSH_BACKEND
    if backend == "console":
        outbox.append(("push", token, body))
        return "console"
    if backend != "fcm":
        raise ProviderError(f"Unknown PUSH_BACKEND {backend!r}.")
    account = _service_account()
    message = {
        "message": {
            "token": token,
            "notification": {"title": title or "TGCG", "body": body},
            "data": {key: str(value) for key, value in data.items()},
            "android": {"priority": "HIGH"},
        }
    }
    status, reply = _post(
        f"https://fcm.googleapis.com/v1/projects/{account['project_id']}/messages:send",
        json.dumps(message).encode(),
        {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {_access_token(account)}",
        },
    )
    if status < 300:
        return str(reply.get("name", ""))
    error = reply.get("error", {})
    codes = {
        detail.get("errorCode") for detail in error.get("details", []) if isinstance(detail, dict)
    }
    if status == 404 or "UNREGISTERED" in codes or (status == 400 and "INVALID_ARGUMENT" in codes):
        raise InvalidToken("This phone no longer receives notifications.")
    raise ProviderError(f"Firebase: {str(error.get('message') or f'HTTP {status}')[:150]}")

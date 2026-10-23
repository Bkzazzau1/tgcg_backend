"""Paystack: the bank's name for an account, and transfers to it.

Set PAYSTACK_SECRET_KEY (sk_test_... to try it, sk_live_... to pay). Paystack
posts each transfer's outcome to /api/v1/paystack/webhook/, signed with that
key: set it as the webhook URL in the Paystack dashboard. Turn off OTP for
transfers there too, or each transfer waits for a code nobody enters.

Two kinds of failure: PaystackError when Paystack answered and refused (no
transfer was made), Unreachable when there was no clear answer (a transfer
may exist; ask again later by its reference).
"""

import hashlib
import hmac
import json
import logging
import urllib.error
import urllib.parse
import urllib.request

from django.conf import settings

logger = logging.getLogger(__name__)


class NotConfigured(Exception):
    pass


class PaystackError(RuntimeError):
    """Paystack refused the request."""

    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


class Unreachable(RuntimeError):
    """No answer from Paystack, or an error on its side."""


def configured() -> bool:
    return bool(settings.PAYSTACK_SECRET_KEY)


def _refusal(error: urllib.error.HTTPError) -> str:
    try:
        return json.loads(error.read()).get("message") or f"HTTP {error.code}"
    except ValueError:
        return f"HTTP {error.code}"


def _call(method: str, path: str, *, query=None, body=None) -> dict:
    if not configured():
        raise NotConfigured
    url = f"{settings.PAYSTACK_BASE_URL.rstrip('/')}{path}"
    if query:
        url = f"{url}?{urllib.parse.urlencode(query)}"
    request = urllib.request.Request(  # noqa: S310 - https URL from settings
        url,
        data=None if body is None else json.dumps(body).encode(),
        headers={
            "Authorization": f"Bearer {settings.PAYSTACK_SECRET_KEY}",
            "Content-Type": "application/json",
        },
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
            payload = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        if 400 <= exc.code < 500:
            raise PaystackError(_refusal(exc), status=exc.code) from exc
        logger.warning("Paystack %s %s returned HTTP %s", method, path, exc.code)
        raise Unreachable(f"Paystack returned HTTP {exc.code}.") from exc
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        logger.warning("Paystack %s %s failed: %s", method, path, exc)
        raise Unreachable("Paystack could not be reached.") from exc
    if not payload.get("status"):
        raise PaystackError(payload.get("message") or "Paystack refused the request.")
    return payload.get("data") or {}


def resolve_account(account_number: str, bank_code: str) -> str:
    """The name the bank holds the account in."""
    data = _call(
        "GET",
        "/bank/resolve",
        query={"account_number": account_number, "bank_code": bank_code},
    )
    return data.get("account_name") or ""


def create_recipient(*, name: str, account_number: str, bank_code: str) -> str:
    """Paystack's code for the account, to transfer to."""
    data = _call(
        "POST",
        "/transferrecipient",
        body={
            "type": "nuban",
            "name": name,
            "account_number": account_number,
            "bank_code": bank_code,
            "currency": "NGN",
        },
    )
    return data["recipient_code"]


def transfer(*, amount_kobo: int, recipient: str, reference: str, reason: str) -> dict:
    """Send the money from TGCG's Paystack balance. Paystack's answer holds
    `status` (success, pending, otp, failed...) and `transfer_code`."""
    return _call(
        "POST",
        "/transfer",
        body={
            "source": "balance",
            "amount": amount_kobo,
            "recipient": recipient,
            "reference": reference,
            "reason": reason,
        },
    )


def verify_transfer(reference: str) -> dict:
    return _call("GET", f"/transfer/verify/{urllib.parse.quote(reference)}")


def read_webhook(body: bytes, signature: str) -> dict:
    """The event Paystack posted, once its signature (HMAC-SHA512 of the body
    with the secret key) is checked."""
    if not configured():
        raise NotConfigured
    expected = hmac.new(settings.PAYSTACK_SECRET_KEY.encode(), body, hashlib.sha512).hexdigest()
    if not hmac.compare_digest(expected, signature or ""):
        raise PaystackError("The signature does not match.")
    try:
        return json.loads(body)
    except ValueError as exc:
        raise PaystackError("The event is not JSON.") from exc

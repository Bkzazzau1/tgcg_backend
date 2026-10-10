"""One-time SMS codes that prove a person controls a phone number."""

import hashlib
import hmac
import secrets
from datetime import timedelta

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.utils import timezone

from apps.core.sms import send_sms

from .phone import normalize_ng_phone


class PhoneVerification(models.Model):
    class Purpose(models.TextChoices):
        REGISTER = "register", "Member registration"
        SET_PIN = "setPin", "Set or reset member PIN"

    phone_number = models.CharField(max_length=16, db_index=True)
    purpose = models.CharField(max_length=16, choices=Purpose.choices)
    code_hash = models.CharField(max_length=64)
    attempts = models.PositiveSmallIntegerField(default=0)
    expires_at = models.DateTimeField()
    consumed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        app_label = "accounts"
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.phone_number} {self.purpose}"


def _hash(phone: str, purpose: str, code: str) -> str:
    key = settings.SECRET_KEY.encode()
    return hmac.new(key, f"{phone}:{purpose}:{code}".encode(), hashlib.sha256).hexdigest()


def require_phone(value: str) -> str:
    phone = normalize_ng_phone(value)
    if phone is None:
        raise ValidationError({"phone_number": "Enter a valid Nigerian mobile number."})
    return phone


@transaction.atomic
def send_code(phone_number: str, purpose: str) -> int:
    """Send a 6-digit code; returns its lifetime in seconds."""
    phone = require_phone(phone_number)
    now = timezone.now()
    recent = PhoneVerification.objects.filter(phone_number=phone, purpose=purpose)
    last = recent.first()
    if last and (now - last.created_at).total_seconds() < settings.OTP_RESEND_SECONDS:
        raise ValidationError(
            {"phone_number": "A code was just sent. Wait a minute before asking again."}
        )
    if recent.filter(created_at__gte=now - timedelta(hours=1)).count() >= settings.OTP_MAX_PER_HOUR:
        raise ValidationError({"phone_number": "Too many codes requested. Try again later."})

    code = f"{secrets.randbelow(1_000_000):06d}"
    ttl = timedelta(minutes=settings.OTP_TTL_MINUTES)
    PhoneVerification.objects.create(
        phone_number=phone,
        purpose=purpose,
        code_hash=_hash(phone, purpose, code),
        expires_at=now + ttl,
    )
    send_sms(
        phone,
        f"Your TGCG verification code is {code}. It expires in "
        f"{settings.OTP_TTL_MINUTES} minutes. Do not share it.",
    )
    return int(ttl.total_seconds())


def consume_code(phone_number: str, purpose: str, code: str) -> str:
    """Check and burn a code. Returns the normalised phone. Raises ValidationError.

    The attempt counter must survive a failed request, so call this from views
    marked `transaction.non_atomic_requests` and validate everything else first.
    """
    phone = require_phone(phone_number)
    error = ValidationError({"code": "The code is wrong or has expired. Request a new one."})
    with transaction.atomic():
        verification = (
            PhoneVerification.objects.select_for_update()
            .filter(
                phone_number=phone,
                purpose=purpose,
                consumed_at__isnull=True,
                expires_at__gt=timezone.now(),
            )
            .first()
        )
        if verification is None or verification.attempts >= settings.OTP_MAX_ATTEMPTS:
            raise error
        verification.attempts += 1
        valid = hmac.compare_digest(
            verification.code_hash, _hash(phone, purpose, (code or "").strip())
        )
        if valid:
            verification.consumed_at = timezone.now()
        verification.save(update_fields=["attempts", "consumed_at"])
    if not valid:
        raise error
    return phone

"""Agent check-ins and on-duty location sharing.

Pings are recorded only while an agent has switched "on duty", and are
deleted after LOCATION_PING_RETENTION_DAYS (see purge_location_pings).
"""

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils import timezone

from apps.core.models import TimeStampedModel
from apps.geography.gps import GpsCheckFields


def _lat():
    return models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)


class AgentCheckIn(TimeStampedModel, GpsCheckFields):
    """An agent declaring they are at their polling unit, with a GPS fix."""

    agent = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="check_ins"
    )
    polling_unit = models.ForeignKey(
        "geography.PollingUnit", on_delete=models.PROTECT, related_name="check_ins"
    )
    latitude = _lat()
    longitude = _lat()
    client_id = models.CharField(  # noqa: DJ001 - NULL keeps the unique index sparse
        max_length=64, unique=True, null=True, blank=True
    )
    confirmed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
        help_text="Coordinator who confirmed this check-in was at the polling unit.",
    )
    confirmed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["polling_unit", "created_at"])]

    def __str__(self):
        return f"{self.agent_id} @ {self.polling_unit_id} ({self.location_status})"


class DutySession(models.Model):
    """The period an agent has chosen to share live location."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="duty_sessions"
    )
    started_at = models.DateTimeField(
        default=timezone.now, help_text="Phone time the agent went on duty."
    )
    ended_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-started_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["user"],
                condition=Q(ended_at__isnull=True),
                name="one_open_duty_session_per_user",
            ),
        ]

    def __str__(self):
        return f"{self.user_id} {self.started_at:%Y-%m-%d %H:%M}"


class LocationPing(GpsCheckFields):
    session = models.ForeignKey(DutySession, on_delete=models.CASCADE, related_name="pings")
    latitude = _lat()
    longitude = _lat()
    received_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-gps_captured_at"]
        indexes = [models.Index(fields=["session", "gps_captured_at"])]


def pin_photo_path(instance, filename):
    import uuid
    from pathlib import Path

    suffix = Path(filename).suffix.lower()[:10] or ".jpg"
    return f"mapping/{timezone.now():%Y/%m}/{uuid.uuid4().hex}{suffix}"


class PollingUnitPin(TimeStampedModel, GpsCheckFields):
    """A coordinator's on-site record of where a polling unit is (or a problem)."""

    class Status(models.TextChoices):
        PINNED = "pinned", "Set the unit's location"
        CONFIRMED = "confirmed", "Agrees with the unit's location"
        CONFLICT = "conflict", "Disagrees with the unit's location"
        ACCEPTED = "accepted", "Accepted to resolve a conflict"
        SUPERSEDED = "superseded", "Replaced by an accepted pin"
        PROBLEM = "problem", "Unit could not be pinned"

    class Problem(models.TextChoices):
        NOT_FOUND = "notFound", "Could not find the unit"
        MOVED = "moved", "Unit has moved"
        NAME_MISMATCH = "nameMismatch", "Signboard shows a different name"
        OTHER = "other", "Other"

    polling_unit = models.ForeignKey(
        "geography.PollingUnit", on_delete=models.PROTECT, related_name="pins"
    )
    pinned_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    status = models.CharField(max_length=12, choices=Status.choices)
    problem = models.CharField(max_length=16, choices=Problem.choices, blank=True)
    note = models.TextField(blank=True, max_length=1000)
    latitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    longitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    photo = models.ImageField(upload_to=pin_photo_path, blank=True)
    at_entrance = models.BooleanField(default=False)
    name_matches = models.BooleanField(default=False)
    resolved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
    )
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["polling_unit", "status"])]

    def __str__(self):
        return f"{self.polling_unit_id} {self.status}"

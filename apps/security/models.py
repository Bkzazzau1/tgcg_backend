"""Incidents the Situation Room sends to security officers.

A security officer is a member whose role holds `respondToSecurity`, which a
National Administrator gives through roles; they sign in like everyone else.
The Situation Room sends an open incident to one officer at a time, picked
from those nearest to it, and the officer answers each step: accepted or
declined, on the way, arrived, resolved.
"""

from django.conf import settings
from django.db import models
from django.db.models import Q

from apps.core.models import TimeStampedModel


class SecurityDispatch(TimeStampedModel):
    """One incident sent to one security officer, and how they answered."""

    class Status(models.TextChoices):
        SENT = "sent", "Sent"
        ACCEPTED = "accepted", "Accepted"
        DECLINED = "declined", "Declined"
        ON_THE_WAY = "onTheWay", "On the way"
        ARRIVED = "arrived", "Arrived"
        RESOLVED = "resolved", "Resolved"
        CANCELLED = "cancelled", "Cancelled"

    class LocatedBy(models.TextChoices):
        LIVE = "live", "Live location"
        AREA = "area", "The area of their role"

    incident = models.ForeignKey(
        "field.Incident", on_delete=models.PROTECT, related_name="security_dispatches"
    )
    officer = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="security_dispatches"
    )
    sent_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    status = models.CharField(
        max_length=10, choices=Status.choices, default=Status.SENT, db_index=True
    )
    note = models.TextField(
        blank=True, max_length=1000, help_text="What the Situation Room told the officer."
    )
    distance_m = models.PositiveIntegerField(
        null=True, blank=True, help_text="How far the officer was from the incident when sent."
    )
    located_by = models.CharField(
        max_length=8,
        choices=LocatedBy.choices,
        blank=True,
        help_text="How that distance was measured; blank when it could not be.",
    )
    notified = models.JSONField(
        default=list, help_text="How the officer was told: app notification, SMS."
    )
    outcome = models.TextField(
        blank=True, max_length=1000, help_text="Why the officer declined, or what they did."
    )
    accepted_at = models.DateTimeField(null=True, blank=True)
    on_the_way_at = models.DateTimeField(null=True, blank=True)
    arrived_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(
        null=True, blank=True, help_text="When it was declined, resolved or cancelled."
    )
    cancelled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    cancel_reason = models.TextField(blank=True, max_length=1000)

    class Meta:
        ordering = ["-created_at", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["incident"],
                condition=Q(status__in=["sent", "accepted", "onTheWay", "arrived"]),
                name="one_active_security_dispatch_per_incident",
            )
        ]

    def __str__(self):
        return f"{self.incident_id} → {self.officer_id} ({self.status})"

    @property
    def is_active(self) -> bool:
        return self.status in ACTIVE


S = SecurityDispatch.Status
# Still with the officer: the incident cannot go to anyone else meanwhile.
ACTIVE = (S.SENT, S.ACCEPTED, S.ON_THE_WAY, S.ARRIVED)

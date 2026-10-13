"""Meetings: calls for the people of an area, or for those invited.

The audio and video run on LiveKit (see livekit.py). This server decides who
may see and join each meeting, hands out short-lived join passes, and keeps
the record: who called it, for whom, and who was let in.
"""

from datetime import timedelta

from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils import timezone

from apps.core.models import TimeStampedModel
from apps.geography.scope import ScopedModel

# Joining opens this long before the start...
EARLY = timedelta(minutes=15)
# ...and closes this long after the planned end, unless the meeting is ended first.
OVERRUN = timedelta(hours=2)


class Meeting(TimeStampedModel, ScopedModel):
    """A call. The scope is the area it is for."""

    class Kind(models.TextChoices):
        AUDIO = "audio", "Audio"
        VIDEO = "video", "Video"

    class Audience(models.TextChoices):
        AREA = "area", "Everyone in the area"
        INVITED = "invited", "Only those invited"

    class Status(models.TextChoices):
        SCHEDULED = "scheduled", "Scheduled"
        LIVE = "live", "Open to join"
        ENDED = "ended", "Ended"
        CANCELLED = "cancelled", "Cancelled"

    reference = models.CharField(max_length=16, unique=True, editable=False)
    title = models.CharField(max_length=160)
    agenda = models.TextField(blank=True, max_length=4000)
    kind = models.CharField(max_length=8, choices=Kind.choices, default=Kind.AUDIO)
    audience = models.CharField(max_length=8, choices=Audience.choices, default=Audience.AREA)
    starts_at = models.DateTimeField(db_index=True)
    duration_minutes = models.PositiveSmallIntegerField(
        default=60, validators=[MinValueValidator(5), MaxValueValidator(480)]
    )
    host = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    invitees = models.ManyToManyField(
        settings.AUTH_USER_MODEL, blank=True, related_name="meetings_invited_to"
    )
    ended_at = models.DateTimeField(null=True, blank=True)
    ended_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
    )
    cancelled_at = models.DateTimeField(null=True, blank=True)
    cancel_reason = models.TextField(blank=True)

    class Meta:
        ordering = ["starts_at"]

    def __str__(self):
        return f"{self.reference} {self.title}"

    @property
    def ends_at(self):
        return self.starts_at + timedelta(minutes=self.duration_minutes)

    @property
    def room_name(self) -> str:
        """The LiveKit room: not guessable from the reference."""
        return f"tgcg-{self.uuid}"

    def status_at(self, now) -> str:
        if self.cancelled_at:
            return self.Status.CANCELLED
        if self.ended_at or now > self.ends_at + OVERRUN:
            return self.Status.ENDED
        if now >= self.starts_at - EARLY:
            return self.Status.LIVE
        return self.Status.SCHEDULED

    @property
    def status(self) -> str:
        return self.status_at(timezone.now())

    def clean(self):
        super().clean()
        self.title = " ".join(self.title.split())
        self.agenda = self.agenda.strip()
        self.normalize_scope()


class Admission(models.Model):
    """Someone this server let into a meeting (gave a join pass). LiveKit
    knows whether they then stayed; this records that they were let in."""

    meeting = models.ForeignKey(Meeting, on_delete=models.CASCADE, related_name="admissions")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    first_admitted_at = models.DateTimeField(auto_now_add=True)
    last_admitted_at = models.DateTimeField(auto_now=True)
    passes = models.PositiveIntegerField(default=1)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["meeting", "user"], name="one_admission_per_person"),
        ]

    def __str__(self):
        return f"{self.meeting_id}: {self.user_id}"

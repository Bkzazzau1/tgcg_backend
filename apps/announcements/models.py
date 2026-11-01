import uuid
from pathlib import Path

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils import timezone

from apps.core.models import TimeStampedModel
from apps.geography.scope import ScopedModel


class Announcement(TimeStampedModel, ScopedModel):
    """News for members. The scope is the area it is addressed to."""

    title = models.CharField(max_length=160)
    body = models.TextField(max_length=5000)
    published_at = models.DateTimeField(default=timezone.now, db_index=True)
    is_published = models.BooleanField(default=True)
    author = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")

    class Meta:
        ordering = ["-published_at"]

    def __str__(self):
        return self.title

    def clean(self):
        super().clean()
        self.title = " ".join(self.title.split())
        self.body = self.body.strip()
        self.normalize_scope()


def popup_upload_path(instance, filename):
    # Random stored name: never trust or expose the uploaded file name on disk.
    suffix = Path(filename).suffix.lower()[:10]
    return f"popups/{timezone.now():%Y/%m}/{uuid.uuid4().hex}{suffix}"


class PopupMessage(TimeStampedModel):
    """A message that pops up for everyone on signing in or opening the app.

    The poster chooses how often each person sees it; while none is due, the
    app shows its daily reminder. A message is kept once posted: it can be
    stopped, not changed or removed.
    """

    class Frequency(models.TextChoices):
        ONCE = "once", "Once only"
        EVERY_OPEN = "everyOpen", "Every sign-in"
        DAILY = "daily", "Once a day"
        WEEKLY = "weekly", "Once a week"

    title = models.CharField(max_length=160)
    body = models.TextField(max_length=2000)
    picture = models.FileField(upload_to=popup_upload_path, blank=True)
    picture_type = models.CharField(max_length=32, blank=True)
    link_url = models.URLField(blank=True)
    link_label = models.CharField(max_length=40, blank=True)
    frequency = models.CharField(max_length=10, choices=Frequency.choices, default=Frequency.DAILY)
    starts_at = models.DateTimeField(default=timezone.now, db_index=True)
    ends_at = models.DateTimeField(null=True, blank=True)
    author = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    stopped_at = models.DateTimeField(null=True, blank=True)
    stopped_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
    )

    class Meta:
        ordering = ["-starts_at", "-id"]

    def __str__(self):
        return self.title

    def state(self, now=None) -> str:
        now = now or timezone.now()
        if self.stopped_at:
            return "stopped"
        if self.starts_at > now:
            return "scheduled"
        if self.ends_at and self.ends_at <= now:
            return "ended"
        return "showing"


def popups_posted(now=None):
    """Those that have started and were not stopped, also once they ended."""
    now = now or timezone.now()
    return PopupMessage.objects.filter(stopped_at__isnull=True, starts_at__lte=now)


def popups_showing(now=None):
    now = now or timezone.now()
    return popups_posted(now).filter(Q(ends_at__isnull=True) | Q(ends_at__gt=now))

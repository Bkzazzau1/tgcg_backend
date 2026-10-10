from django.conf import settings
from django.db import models
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

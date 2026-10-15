"""The discussion forum: for everyone with a role, never ordinary members.

A post carries text, pictures and videos, and may be tagged with an
assignment: a job someone was given, shown to the whole platform. While
approval is on, a post made at a polling unit waits for the ward
coordinator; other posts and every reply appear at once. Coordinators hide
what breaks the rules, and authors delete their own.
"""

import uuid
from pathlib import Path

from django.conf import settings
from django.db import models
from django.utils import timezone

from apps.core.models import TimeStampedModel
from apps.geography.scope import ScopedModel


class ForumSettings(models.Model):
    """One row: whether posts from polling units wait for approval."""

    approval_required = models.BooleanField(default=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name_plural = "forum settings"

    def __str__(self):
        return f"Approval {'on' if self.approval_required else 'off'}"

    @classmethod
    def current(cls) -> "ForumSettings":
        return cls.objects.get_or_create(pk=1)[0]


class Post(TimeStampedModel, ScopedModel):
    """A post. The scope is where it was made: its author's area."""

    class Status(models.TextChoices):
        PENDING = "pending", "Waiting for approval"
        PUBLISHED = "published", "Published"
        REJECTED = "rejected", "Not approved"
        HIDDEN = "hidden", "Hidden by a coordinator"
        DELETED = "deleted", "Deleted by its author"

    author = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    # As shown when posted, so later role changes do not rewrite the record.
    author_name = models.CharField(max_length=150)
    author_role = models.CharField(max_length=200, blank=True)
    body = models.TextField(max_length=4000, blank=True)
    assignment = models.CharField(
        max_length=120, blank=True, db_index=True, help_text="The assignment the post shows."
    )
    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.PUBLISHED, db_index=True
    )
    published_at = models.DateTimeField(null=True, blank=True, db_index=True)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    review_reason = models.TextField(blank=True)
    client_id = models.CharField(  # noqa: DJ001 - NULL keeps the unique index sparse
        max_length=64, unique=True, null=True, blank=True
    )

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.author_name}: {self.body[:40]}"

    def clean(self):
        super().clean()
        self.body = self.body.strip()
        self.assignment = " ".join(self.assignment.split())
        self.normalize_scope()


def forum_upload_path(instance, filename):
    # Random stored name: never trust or expose the uploaded file name on disk.
    suffix = Path(filename).suffix.lower()[:10]
    return f"forum/{timezone.now():%Y/%m}/{uuid.uuid4().hex}{suffix}"


class Attachment(TimeStampedModel):
    """A picture or video in a post. Served only through the forum, never
    from MEDIA_URL."""

    class Kind(models.TextChoices):
        PHOTO = "photo", "Picture"
        VIDEO = "video", "Video"

    post = models.ForeignKey(Post, on_delete=models.CASCADE, related_name="attachments")
    kind = models.CharField(max_length=8, choices=Kind.choices)
    file = models.FileField(upload_to=forum_upload_path)
    mime_type = models.CharField(max_length=100)
    size = models.PositiveBigIntegerField()
    content_hash = models.CharField(max_length=64, help_text="SHA-256 computed by the server.")

    class Meta:
        ordering = ["created_at", "id"]

    def __str__(self):
        return f"{self.get_kind_display()} in {self.post_id}"


class Reply(TimeStampedModel):
    class Status(models.TextChoices):
        VISIBLE = "visible", "Visible"
        HIDDEN = "hidden", "Hidden by a coordinator"
        DELETED = "deleted", "Deleted by its author"

    post = models.ForeignKey(Post, on_delete=models.CASCADE, related_name="replies")
    author = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    author_name = models.CharField(max_length=150)
    author_role = models.CharField(max_length=200, blank=True)
    body = models.TextField(max_length=2000)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.VISIBLE)
    hidden_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
    )
    hidden_at = models.DateTimeField(null=True, blank=True)
    hide_reason = models.TextField(blank=True)

    class Meta:
        ordering = ["created_at", "id"]
        verbose_name_plural = "replies"

    def __str__(self):
        return f"{self.author_name}: {self.body[:40]}"

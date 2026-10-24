"""The Social Forum: every member posts, comments and likes (agreed with the
user on 24 October 2026; before, the discussion forum was for role holders).

A post carries text, pictures and videos, and may be tagged with an
assignment: a job someone was given, shown to the whole platform. Posts and
comments appear at once. Members like posts and comments, answer a comment
(one level), edit or delete their own, and report what breaks the rules.
Editors (everyone holding a role or position) hide posts and comments with a
reason, anywhere, and restore them; nobody changes another person's words.
"""

import uuid
from pathlib import Path

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils import timezone

from apps.core.models import TimeStampedModel
from apps.geography.scope import ScopedModel


class Post(TimeStampedModel, ScopedModel):
    """A post. The scope is where it was made: its author's area."""

    class Status(models.TextChoices):
        # Pending and rejected come from the approval of the first forum
        # (15-24 October 2026); new posts are published at once.
        PENDING = "pending", "Waiting for approval"
        PUBLISHED = "published", "Published"
        REJECTED = "rejected", "Not approved"
        HIDDEN = "hidden", "Hidden by an editor"
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
    edited_at = models.DateTimeField(null=True, blank=True, help_text="Last edited by its author.")
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
    """A comment on a post, or an answer to a comment (one level: an answer
    to an answer joins the same comment)."""

    class Status(models.TextChoices):
        VISIBLE = "visible", "Visible"
        HIDDEN = "hidden", "Hidden by an editor"
        DELETED = "deleted", "Deleted by its author"

    post = models.ForeignKey(Post, on_delete=models.CASCADE, related_name="replies")
    parent = models.ForeignKey(
        "self",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="answers",
        help_text="The comment this answers; empty for a comment on the post.",
    )
    author = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    author_name = models.CharField(max_length=150)
    author_role = models.CharField(max_length=200, blank=True)
    body = models.TextField(max_length=2000)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.VISIBLE)
    edited_at = models.DateTimeField(null=True, blank=True, help_text="Last edited by its author.")
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


class Like(models.Model):
    """One person likes one post or one comment."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+")
    post = models.ForeignKey(
        Post, on_delete=models.CASCADE, null=True, blank=True, related_name="likes"
    )
    reply = models.ForeignKey(
        Reply, on_delete=models.CASCADE, null=True, blank=True, related_name="likes"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(post__isnull=False, reply__isnull=True)
                | Q(post__isnull=True, reply__isnull=False),
                name="forum_like_one_target",
            ),
            models.UniqueConstraint(
                fields=["user", "post"],
                condition=Q(post__isnull=False),
                name="forum_like_once_per_post",
            ),
            models.UniqueConstraint(
                fields=["user", "reply"],
                condition=Q(reply__isnull=False),
                name="forum_like_once_per_reply",
            ),
        ]

    def __str__(self):
        return f"{self.user_id} likes {self.post_id or self.reply_id}"


class Report(TimeStampedModel):
    """Someone reports a post or a comment to the editors."""

    class Status(models.TextChoices):
        OPEN = "open", "Open"
        CLOSED = "closed", "Closed"

    class Outcome(models.TextChoices):
        HIDDEN = "hidden", "Hidden"
        DISMISSED = "dismissed", "Dismissed"

    reporter = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    post = models.ForeignKey(Post, on_delete=models.CASCADE, related_name="reports")
    reply = models.ForeignKey(
        Reply,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="reports",
        help_text="The comment reported; empty when the post itself is.",
    )
    reason = models.TextField(max_length=1000)
    status = models.CharField(
        max_length=8, choices=Status.choices, default=Status.OPEN, db_index=True
    )
    outcome = models.CharField(max_length=10, choices=Outcome.choices, blank=True)
    closed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
    )
    closed_at = models.DateTimeField(null=True, blank=True)
    close_reason = models.TextField(blank=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            # One open report per person per post or comment.
            models.UniqueConstraint(
                fields=["reporter", "post"],
                condition=Q(status="open", reply__isnull=True),
                name="forum_report_open_once_per_post",
            ),
            models.UniqueConstraint(
                fields=["reporter", "reply"],
                condition=Q(status="open", reply__isnull=False),
                name="forum_report_open_once_per_reply",
            ),
        ]

    def __str__(self):
        return f"Report on {self.reply_id or self.post_id}: {self.reason[:40]}"

from django.urls import reverse
from rest_framework import serializers

from apps.core.choices import GeographyLevel
from apps.geography.scope import ScopeField

from . import services
from .models import Attachment, Post, Reply, Report


class AttachmentSerializer(serializers.ModelSerializer):
    id = serializers.UUIDField(source="uuid", read_only=True)
    url = serializers.SerializerMethodField(help_text="Download with the session's token.")
    poster_url = serializers.SerializerMethodField(
        help_text="A video's picture (JPEG), when the posting app made one; download with "
        "the session's token."
    )

    class Meta:
        model = Attachment
        fields = [
            "id",
            "kind",
            "mime_type",
            "size",
            "url",
            "poster_url",
            "width",
            "height",
            "duration_ms",
        ]
        read_only_fields = fields

    def get_url(self, attachment) -> str:
        return self.context["request"].build_absolute_uri(
            reverse("forum-attachment", args=[attachment.uuid])
        )

    def get_poster_url(self, attachment) -> str | None:
        if not attachment.poster:
            return None
        return self.context["request"].build_absolute_uri(
            reverse("forum-attachment-poster", args=[attachment.uuid])
        )


class _ForumItem(serializers.ModelSerializer):
    """What posts and comments share: likes, and what this account may do."""

    is_mine = serializers.SerializerMethodField()
    likes = serializers.SerializerMethodField()
    liked = serializers.SerializerMethodField(help_text="This account likes it.")
    can_edit = serializers.SerializerMethodField(help_text="Its author may edit or delete it.")
    can_hide = serializers.SerializerMethodField(help_text="An editor may hide it.")
    can_restore = serializers.SerializerMethodField(help_text="An editor may restore it.")
    can_report = serializers.SerializerMethodField(help_text="This account may report it.")

    shown = None  # the status in which everyone sees it
    hidden = None

    def _user(self):
        return self.context["request"].user

    def _editor(self) -> bool:
        if "editor" not in self.context:
            self.context["editor"] = services.is_editor(self._user())
        return self.context["editor"]

    def get_is_mine(self, item) -> bool:
        return item.author_id == self._user().pk

    def get_likes(self, item) -> int:
        count = getattr(item, "like_count", None)
        return count if count is not None else item.likes.count()

    def get_liked(self, item) -> bool:
        liked = getattr(item, "liked", None)
        return bool(liked) if liked is not None else item.likes.filter(user=self._user()).exists()

    def get_can_edit(self, item) -> bool:
        return self.get_is_mine(item) and item.status != "deleted"

    def get_can_hide(self, item) -> bool:
        return self._editor() and item.status == self.shown

    def get_can_restore(self, item) -> bool:
        return self._editor() and item.status == self.hidden

    def get_can_report(self, item) -> bool:
        return not self.get_is_mine(item) and item.status == self.shown


class PostSerializer(_ForumItem):
    id = serializers.UUIDField(source="uuid", read_only=True)
    scope = ScopeField(read_only=True)
    attachments = AttachmentSerializer(many=True, read_only=True)
    replies = serializers.SerializerMethodField(help_text="Comments everyone sees.")
    review_reason = serializers.SerializerMethodField(
        help_text="Why it was hidden (or, before 24 October 2026, not approved): for its "
        "author and editors."
    )
    can_moderate = serializers.SerializerMethodField(help_text="Same as can_hide (older apps).")

    shown = Post.Status.PUBLISHED
    hidden = Post.Status.HIDDEN

    class Meta:
        model = Post
        fields = [
            "id",
            "author_name",
            "author_role",
            "scope",
            "body",
            "assignment",
            "status",
            "created_at",
            "published_at",
            "edited_at",
            "review_reason",
            "attachments",
            "replies",
            "likes",
            "liked",
            "is_mine",
            "can_edit",
            "can_hide",
            "can_restore",
            "can_report",
            "can_moderate",
        ]
        read_only_fields = fields

    def get_replies(self, post) -> int:
        count = getattr(post, "reply_count", None)
        return count if count is not None else post.replies.filter(status="visible").count()

    def get_review_reason(self, post) -> str:
        if post.author_id == self._user().pk or self._editor():
            return post.review_reason
        return ""

    def get_can_moderate(self, post) -> bool:
        return self.get_can_hide(post)


class PostCreateSerializer(serializers.Serializer):
    body = serializers.CharField(max_length=4000, required=False, allow_blank=True, default="")
    assignment = serializers.CharField(max_length=120, required=False, allow_blank=True, default="")
    level = serializers.ChoiceField(
        choices=GeographyLevel.choices,
        required=False,
        help_text="Ignored: a post is from its author's own area (kept for older apps).",
    )
    code = serializers.CharField(max_length=32, required=False)
    client_id = serializers.CharField(max_length=64, required=False, allow_blank=True)
    files = serializers.ListField(
        child=serializers.FileField(),
        required=False,
        help_text="Pictures and videos (multipart, repeat the field). For the n-th file (from "
        "0), a video, `poster_<n>` may add a frame from it (JPEG, PNG or WebP, up to 2 MB) "
        "and `duration_<n>` its length in milliseconds.",
    )


class PostUpdateSerializer(serializers.Serializer):
    body = serializers.CharField(max_length=4000, required=False, allow_blank=True)
    assignment = serializers.CharField(max_length=120, required=False, allow_blank=True)


class ReplySerializer(_ForumItem):
    id = serializers.UUIDField(source="uuid", read_only=True)
    parent = serializers.UUIDField(
        source="parent.uuid",
        default=None,
        read_only=True,
        help_text="The comment this answers; null for a comment on the post.",
    )
    hide_reason = serializers.SerializerMethodField()

    shown = Reply.Status.VISIBLE
    hidden = Reply.Status.HIDDEN

    class Meta:
        model = Reply
        fields = [
            "id",
            "parent",
            "author_name",
            "author_role",
            "body",
            "status",
            "hide_reason",
            "created_at",
            "edited_at",
            "likes",
            "liked",
            "is_mine",
            "can_edit",
            "can_hide",
            "can_restore",
            "can_report",
        ]
        read_only_fields = fields

    def get_hide_reason(self, reply) -> str:
        if reply.author_id == self._user().pk or self._editor():
            return reply.hide_reason
        return ""


class ReplyCreateSerializer(serializers.Serializer):
    body = serializers.CharField(max_length=2000)
    parent = serializers.UUIDField(
        required=False, allow_null=True, help_text="The comment to answer."
    )


class ReplyUpdateSerializer(serializers.Serializer):
    body = serializers.CharField(max_length=2000)


class ForumReasonSerializer(serializers.Serializer):
    reason = serializers.CharField(max_length=1000)


class OptionalReasonSerializer(serializers.Serializer):
    reason = serializers.CharField(max_length=1000, required=False, allow_blank=True, default="")


class LikesSerializer(serializers.Serializer):
    liked = serializers.BooleanField()
    likes = serializers.IntegerField()


class ReportSerializer(serializers.ModelSerializer):
    """A report, for editors: what was reported, by whom and why."""

    id = serializers.UUIDField(source="uuid", read_only=True)
    target = serializers.SerializerMethodField(help_text='"post" or "reply".')
    post = serializers.UUIDField(source="post.uuid", read_only=True)
    reply = serializers.UUIDField(source="reply.uuid", default=None, read_only=True)
    author_name = serializers.SerializerMethodField(help_text="Who wrote what was reported.")
    excerpt = serializers.SerializerMethodField(help_text="What was reported, shortened.")
    target_status = serializers.SerializerMethodField()
    reporter = serializers.SerializerMethodField()

    class Meta:
        model = Report
        fields = [
            "id",
            "target",
            "post",
            "reply",
            "author_name",
            "excerpt",
            "target_status",
            "reason",
            "reporter",
            "created_at",
            "status",
            "outcome",
        ]
        read_only_fields = fields

    def _target(self, report):
        return report.reply or report.post

    def get_target(self, report) -> str:
        return "reply" if report.reply_id else "post"

    def get_author_name(self, report) -> str:
        return self._target(report).author_name

    def get_excerpt(self, report) -> str:
        return self._target(report).body[:280]

    def get_target_status(self, report) -> str:
        return self._target(report).status

    def get_reporter(self, report) -> str:
        user = report.reporter
        return user.full_name or user.access_id


class ForumMeSerializer(serializers.Serializer):
    takes_part = serializers.BooleanField(help_text="May post, comment, like and report.")
    editor = serializers.BooleanField(help_text="Holds a role or position: hides and restores.")


class ForumSettingsSerializer(serializers.Serializer):
    approval_required = serializers.BooleanField(
        help_text="Always false since 24 October 2026: posts appear at once."
    )
    can_change = serializers.BooleanField(read_only=True)
    updated_at = serializers.DateTimeField(read_only=True, allow_null=True)


class AssignmentTagSerializer(serializers.Serializer):
    tag = serializers.CharField()
    posts = serializers.IntegerField()

from django.urls import reverse
from rest_framework import serializers

from apps.core.choices import GeographyLevel
from apps.geography.scope import ScopeField

from . import services
from .models import Attachment, Post, Reply


class AttachmentSerializer(serializers.ModelSerializer):
    id = serializers.UUIDField(source="uuid", read_only=True)
    url = serializers.SerializerMethodField(help_text="Download with the session's token.")

    class Meta:
        model = Attachment
        fields = ["id", "kind", "mime_type", "size", "url"]
        read_only_fields = fields

    def get_url(self, attachment) -> str:
        return self.context["request"].build_absolute_uri(
            reverse("forum-attachment", args=[attachment.uuid])
        )


class PostSerializer(serializers.ModelSerializer):
    id = serializers.UUIDField(source="uuid", read_only=True)
    scope = ScopeField(read_only=True)
    attachments = AttachmentSerializer(many=True, read_only=True)
    replies = serializers.SerializerMethodField(help_text="Replies everyone sees.")
    review_reason = serializers.SerializerMethodField(
        help_text="Why it was not approved or was hidden: for its author and moderators."
    )
    is_mine = serializers.SerializerMethodField()
    can_moderate = serializers.SerializerMethodField(help_text="May approve, refuse or hide it.")

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
            "review_reason",
            "attachments",
            "replies",
            "is_mine",
            "can_moderate",
        ]
        read_only_fields = fields

    def _user(self):
        return self.context["request"].user

    def get_replies(self, post) -> int:
        count = getattr(post, "reply_count", None)
        return count if count is not None else post.replies.filter(status="visible").count()

    def get_review_reason(self, post) -> str:
        if post.author_id == self._user().pk or self.get_can_moderate(post):
            return post.review_reason
        return ""

    def get_is_mine(self, post) -> bool:
        return post.author_id == self._user().pk

    def get_can_moderate(self, post) -> bool:
        return services.moderates(self._user(), post)


class PostCreateSerializer(serializers.Serializer):
    body = serializers.CharField(max_length=4000, required=False, allow_blank=True, default="")
    assignment = serializers.CharField(max_length=120, required=False, allow_blank=True, default="")
    level = serializers.ChoiceField(
        choices=GeographyLevel.choices,
        required=False,
        help_text="The area the post is for, when the author has several (with code).",
    )
    code = serializers.CharField(max_length=32, required=False)
    client_id = serializers.CharField(max_length=64, required=False, allow_blank=True)
    files = serializers.ListField(
        child=serializers.FileField(),
        required=False,
        help_text="Pictures and videos (multipart, repeat the field).",
    )


class ReplySerializer(serializers.ModelSerializer):
    id = serializers.UUIDField(source="uuid", read_only=True)
    hide_reason = serializers.SerializerMethodField()
    is_mine = serializers.SerializerMethodField()

    class Meta:
        model = Reply
        fields = [
            "id",
            "author_name",
            "author_role",
            "body",
            "status",
            "hide_reason",
            "created_at",
            "is_mine",
        ]
        read_only_fields = fields

    def get_hide_reason(self, reply) -> str:
        user = self.context["request"].user
        if reply.author_id == user.pk or services.moderates(user, reply.post):
            return reply.hide_reason
        return ""

    def get_is_mine(self, reply) -> bool:
        return reply.author_id == self.context["request"].user.pk


class ReplyCreateSerializer(serializers.Serializer):
    body = serializers.CharField(max_length=2000)


class ForumReasonSerializer(serializers.Serializer):
    reason = serializers.CharField(max_length=1000)


class ForumSettingsSerializer(serializers.Serializer):
    approval_required = serializers.BooleanField(
        help_text="Posts from polling units wait for the ward coordinator."
    )
    can_change = serializers.BooleanField(read_only=True)
    updated_at = serializers.DateTimeField(read_only=True)


class AssignmentTagSerializer(serializers.Serializer):
    tag = serializers.CharField()
    posts = serializers.IntegerField()

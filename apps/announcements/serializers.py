from django.utils import timezone
from rest_framework import serializers

from apps.geography.scope import ScopeField

from .models import Announcement, PopupMessage


class AnnouncementSerializer(serializers.ModelSerializer):
    id = serializers.UUIDField(source="uuid", read_only=True)
    scope = ScopeField(required=False, help_text="Audience area. Defaults to your own scope.")
    author = serializers.CharField(source="author.full_name", read_only=True)

    class Meta:
        model = Announcement
        fields = ["id", "title", "body", "scope", "published_at", "is_published", "author"]


class PopupSerializer(serializers.ModelSerializer):
    """A pop-up as the people who see it receive it."""

    id = serializers.UUIDField(source="uuid", read_only=True)
    has_picture = serializers.SerializerMethodField()

    class Meta:
        model = PopupMessage
        fields = ["id", "title", "body", "has_picture", "link_url", "link_label", "frequency"]
        read_only_fields = fields

    def get_has_picture(self, popup) -> bool:
        return bool(popup.picture)


class PostedPopupSerializer(PopupSerializer):
    """A pop-up as those who post them see it."""

    state = serializers.SerializerMethodField()
    author = serializers.CharField(source="author.full_name", read_only=True)

    class Meta(PopupSerializer.Meta):
        fields = [
            *PopupSerializer.Meta.fields,
            "starts_at",
            "ends_at",
            "state",
            "author",
            "created_at",
        ]
        read_only_fields = fields

    def get_state(self, popup) -> str:
        return popup.state()


class NewPopupSerializer(serializers.Serializer):
    """A multipart form, so that a picture can come with it."""

    title = serializers.CharField(max_length=160)
    body = serializers.CharField(max_length=2000)
    frequency = serializers.ChoiceField(choices=PopupMessage.Frequency.choices)
    picture = serializers.FileField(required=False)
    link_url = serializers.URLField(required=False, allow_blank=True)
    link_label = serializers.CharField(max_length=40, required=False, allow_blank=True)
    starts_at = serializers.DateTimeField(required=False)
    ends_at = serializers.DateTimeField(required=False)

    def validate_link_url(self, value):
        if value and not value.lower().startswith("https://"):
            raise serializers.ValidationError("Use a link that starts with https://.")
        return value

    def validate(self, attrs):
        url, label = attrs.get("link_url"), attrs.get("link_label", "").strip()
        if bool(url) != bool(label):
            field = "link_label" if url else "link_url"
            raise serializers.ValidationError({field: "A button needs both its words and a link."})
        start = attrs.get("starts_at") or timezone.now()
        end = attrs.get("ends_at")
        if end and end <= start:
            raise serializers.ValidationError({"ends_at": "The end must come after the start."})
        if end and end <= timezone.now():
            raise serializers.ValidationError({"ends_at": "The end has already passed."})
        return {**attrs, "link_label": label, "starts_at": start}

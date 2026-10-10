from rest_framework import serializers

from apps.geography.scope import ScopeField

from .models import Announcement


class AnnouncementSerializer(serializers.ModelSerializer):
    id = serializers.UUIDField(source="uuid", read_only=True)
    scope = ScopeField(required=False, help_text="Audience area. Defaults to your own scope.")
    author = serializers.CharField(source="author.full_name", read_only=True)

    class Meta:
        model = Announcement
        fields = ["id", "title", "body", "scope", "published_at", "is_published", "author"]

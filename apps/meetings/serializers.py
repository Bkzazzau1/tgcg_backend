from rest_framework import serializers

from apps.geography.scope import ScopeField

from . import livekit, services
from .models import Meeting


class MeetingSerializer(serializers.ModelSerializer):
    id = serializers.UUIDField(source="uuid", read_only=True)
    scope = ScopeField(read_only=True)
    status = serializers.ChoiceField(choices=Meeting.Status.choices, read_only=True)
    ends_at = serializers.DateTimeField(read_only=True)
    host = serializers.SlugRelatedField(slug_field="access_id", read_only=True)
    host_name = serializers.CharField(source="host.full_name", read_only=True)
    invitees = serializers.SlugRelatedField(slug_field="access_id", many=True, read_only=True)
    admitted = serializers.SerializerMethodField(help_text="People given a join pass so far.")
    can_join = serializers.SerializerMethodField()
    can_manage = serializers.SerializerMethodField(help_text="May end or cancel it.")
    calls_available = serializers.SerializerMethodField(
        help_text="False until LiveKit is set up on the server."
    )

    class Meta:
        model = Meeting
        fields = [
            "id",
            "reference",
            "title",
            "agenda",
            "kind",
            "audience",
            "scope",
            "starts_at",
            "duration_minutes",
            "ends_at",
            "status",
            "host",
            "host_name",
            "invitees",
            "admitted",
            "ended_at",
            "cancelled_at",
            "cancel_reason",
            "can_join",
            "can_manage",
            "calls_available",
            "created_at",
        ]
        read_only_fields = fields

    def _user(self):
        return self.context["request"].user

    def get_admitted(self, meeting) -> int:
        return getattr(meeting, "admitted_count", None) or meeting.admissions.count()

    def get_can_join(self, meeting) -> bool:
        return services.can_join(self._user(), meeting)

    def get_can_manage(self, meeting) -> bool:
        return services.can_manage(self._user(), meeting)

    def get_calls_available(self, meeting) -> bool:
        return livekit.configured()


class MeetingListQuerySerializer(serializers.Serializer):
    since = serializers.DateTimeField(required=False)


class MeetingCreateSerializer(serializers.Serializer):
    title = serializers.CharField(max_length=160)
    agenda = serializers.CharField(required=False, allow_blank=True, max_length=4000)
    kind = serializers.ChoiceField(choices=Meeting.Kind.choices, default=Meeting.Kind.AUDIO)
    audience = serializers.ChoiceField(
        choices=Meeting.Audience.choices, default=Meeting.Audience.AREA
    )
    starts_at = serializers.DateTimeField(required=False, help_text="Defaults to now.")
    duration_minutes = serializers.IntegerField(min_value=5, max_value=480, default=60)
    scope = ScopeField(required=False, help_text="The area it is for. Defaults to yours.")
    invitees = serializers.ListField(
        child=serializers.CharField(max_length=40),
        required=False,
        max_length=services.MAX_INVITEES,
        help_text="Access IDs of people to invite, inside or outside the area.",
    )


class CancelSerializer(serializers.Serializer):
    reason = serializers.CharField(max_length=2000)


class JoinPassSerializer(serializers.Serializer):
    url = serializers.CharField(help_text="The LiveKit server to connect to.")
    token = serializers.CharField(help_text="The join pass; connect within 30 minutes.")
    room = serializers.CharField()
    expires_at = serializers.DateTimeField()

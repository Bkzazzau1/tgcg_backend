from django.db.models import Count
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from apps.core.choices import Gender
from apps.geography.scope import ScopeField

from .models import Broadcast, Channel, MessageList

PEOPLE = ["everyone", "withRole", "noRole", "roles"]


class GroupSerializer(serializers.Serializer):
    """One group of people. `filter`: members in `areas` (all of Nigeria when
    empty) narrowed by `people`, `roles`, `gender` and `verified_only`;
    `project`: those taking part in a project; `list`: a saved list."""

    type = serializers.ChoiceField(choices=["filter", "project", "list"])
    areas = serializers.ListField(child=ScopeField(), required=False)
    people = serializers.ChoiceField(choices=PEOPLE, required=False, default="everyone")
    roles = serializers.ListField(
        child=serializers.CharField(max_length=64), required=False, help_text="Role codes."
    )
    gender = serializers.ChoiceField(
        choices=Gender.choices, required=False, allow_blank=True, default=""
    )
    verified_only = serializers.BooleanField(required=False, default=False)
    project = serializers.UUIDField(required=False)
    list = serializers.UUIDField(required=False)

    def validate(self, attrs):
        kind = attrs["type"]
        if kind in ("project", "list") and not attrs.get(kind):
            raise serializers.ValidationError({kind: "Choose one."})
        if kind == "filter" and attrs.get("people") == "roles" and not attrs.get("roles"):
            raise serializers.ValidationError({"roles": "Choose at least one role."})
        return attrs


class MessageSerializer(serializers.Serializer):
    channels = serializers.ListField(
        child=serializers.ChoiceField(choices=Channel.choices), allow_empty=False
    )
    title = serializers.CharField(max_length=120, required=False, allow_blank=True, default="")
    body = serializers.CharField(max_length=1000)
    groups = GroupSerializer(many=True, allow_empty=False)


class PreviewSerializer(MessageSerializer):
    body = serializers.CharField(max_length=1000, required=False, allow_blank=True, default="")


class BroadcastSerializer(serializers.ModelSerializer):
    id = serializers.UUIDField(source="uuid", read_only=True)
    sender = serializers.CharField(source="sender.access_id", read_only=True)
    sender_name = serializers.CharField(source="sender.full_name", read_only=True)
    delivered = serializers.SerializerMethodField()

    class Meta:
        model = Broadcast
        fields = [
            "id",
            "sender",
            "sender_name",
            "title",
            "body",
            "channels",
            "audience",
            "people",
            "unreachable",
            "status",
            "created_at",
            "finished_at",
            "delivered",
        ]
        read_only_fields = fields

    @extend_schema_field(serializers.DictField(child=serializers.DictField()))
    def get_delivered(self, broadcast):
        """Per channel, how many are sent, not sent, or waiting."""
        counts = {}
        rows = broadcast.deliveries.values("channel", "status").annotate(n=Count("id"))
        for row in rows:
            counts.setdefault(row["channel"], {})[row["status"]] = row["n"]
        return counts


class InboxSerializer(serializers.ModelSerializer):
    id = serializers.UUIDField(source="uuid", read_only=True)

    class Meta:
        model = Broadcast
        fields = ["id", "title", "body", "created_at"]
        read_only_fields = fields


class ListMemberSerializer(serializers.Serializer):
    membership_number = serializers.CharField()
    full_name = serializers.CharField()


class MessageListSerializer(serializers.ModelSerializer):
    id = serializers.UUIDField(source="uuid", read_only=True)
    people = serializers.SerializerMethodField()

    class Meta:
        model = MessageList
        fields = ["id", "name", "people", "created_at", "updated_at"]
        read_only_fields = fields

    def get_people(self, message_list) -> int:
        return message_list.members.count()


class MessageListDetailSerializer(MessageListSerializer):
    members = ListMemberSerializer(many=True, read_only=True)

    class Meta(MessageListSerializer.Meta):
        fields = [*MessageListSerializer.Meta.fields, "members"]
        read_only_fields = fields


class MessageListWriteSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=120)
    members = serializers.ListField(
        child=serializers.CharField(max_length=16), required=False, help_text="Membership numbers."
    )
    groups = GroupSerializer(many=True, required=False, help_text="Add the people these reach now.")


class MessageListUpdateSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=120, required=False)
    add = serializers.ListField(child=serializers.CharField(max_length=16), required=False)
    remove = serializers.ListField(child=serializers.CharField(max_length=16), required=False)


class DeviceSerializer(serializers.Serializer):
    token = serializers.CharField(max_length=255)
    platform = serializers.CharField(max_length=16, required=False, allow_blank=True, default="")

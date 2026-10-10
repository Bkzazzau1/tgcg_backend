from rest_framework import serializers

from apps.core.choices import AccreditationStatus
from apps.geography.scope import ScopeField, within_scope_q

from .models import ACCREDITABLE_ROLES, AccreditationEvent, AccreditedAgent, Member


def flatten_scope(validated_data: dict) -> dict:
    """Turn the ScopeField's nested attrs into model field kwargs."""
    data = dict(validated_data)
    data.update(data.pop("scope", {}))
    return data


class ActiveAccreditationSerializer(serializers.ModelSerializer):
    class Meta:
        model = AccreditedAgent
        fields = ["agent_id", "role", "status"]


class MemberSerializer(serializers.ModelSerializer):
    id = serializers.UUIDField(source="uuid", read_only=True)
    scope = ScopeField(required=False)
    active_accreditation = serializers.SerializerMethodField()

    class Meta:
        model = Member
        fields = [
            "id",
            "membership_number",
            "full_name",
            "phone_number",
            "email",
            "status",
            "origin",
            "client_id",
            "scope",
            "active_accreditation",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["membership_number", "created_at", "updated_at"]
        # Uniqueness is enforced by the model/service so offline retries
        # with the same client_id are idempotent instead of rejected.
        extra_kwargs = {
            "phone_number": {"validators": []},
            "client_id": {"validators": []},
        }
        validators = []

    def get_active_accreditation(self, member) -> dict | None:
        for agent in member.accreditations.all():
            if agent.status in (AccreditationStatus.PENDING, AccreditationStatus.APPROVED):
                return ActiveAccreditationSerializer(agent).data
        return None


class MemberCreateSerializer(MemberSerializer):
    class Meta(MemberSerializer.Meta):
        read_only_fields = [*MemberSerializer.Meta.read_only_fields, "status"]


class ScopedMemberField(serializers.SlugRelatedField):
    """Member by membership number, limited to the requesting user's scope."""

    def get_queryset(self):
        user = self.context["request"].user
        return Member.objects.filter(within_scope_q(user))


class AgentSerializer(serializers.ModelSerializer):
    id = serializers.UUIDField(source="uuid", read_only=True)
    member = ScopedMemberField(slug_field="membership_number")
    member_name = serializers.CharField(source="member.full_name", read_only=True)
    role = serializers.ChoiceField(choices=[(r.value, r.label) for r in ACCREDITABLE_ROLES])
    scope = ScopeField()
    is_ready = serializers.BooleanField(read_only=True)

    class Meta:
        model = AccreditedAgent
        fields = [
            "id",
            "agent_id",
            "member",
            "member_name",
            "role",
            "status",
            "scope",
            "registered_phone_number",
            "device_id",
            "sim_fingerprint",
            "biometric_enrolled",
            "training_completed",
            "is_ready",
            "origin",
            "client_id",
            "status_changed_at",
            "created_at",
        ]
        read_only_fields = ["agent_id", "status", "status_changed_at", "created_at"]
        extra_kwargs = {"client_id": {"validators": []}}
        # Accreditation constraints are enforced by the model and service.
        validators = []


class ReadinessSerializer(serializers.ModelSerializer):
    class Meta:
        model = AccreditedAgent
        fields = ["device_id", "sim_fingerprint", "biometric_enrolled", "training_completed"]


class StatusChangeSerializer(serializers.Serializer):
    status = serializers.ChoiceField(choices=AccreditationStatus.choices)
    reason = serializers.CharField(required=False, allow_blank=True, max_length=1000)


class AccreditationEventSerializer(serializers.ModelSerializer):
    actor = serializers.SlugRelatedField(slug_field="access_id", read_only=True)

    class Meta:
        model = AccreditationEvent
        fields = ["from_status", "to_status", "reason", "actor", "created_at"]

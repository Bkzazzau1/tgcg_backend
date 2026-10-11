from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from apps.core.choices import GeographyLevel
from apps.core.permissions import Capability
from apps.geography.scope import ScopeField, scope_payload

from .models import AuditEvent, RoleAssignment, RoleCategory, RoleDefinition

# The Capability choices themselves, so the schema names them once (PermissionCode).
PERMISSION_CHOICES = Capability.choices


class PermissionInfoSerializer(serializers.Serializer):
    code = serializers.CharField()
    module = serializers.CharField()
    action = serializers.CharField()
    label = serializers.CharField()
    protected = serializers.BooleanField()
    sensitive = serializers.BooleanField()


class RoleDefinitionSerializer(serializers.ModelSerializer):
    permitted_levels = serializers.ListField(
        child=serializers.ChoiceField(choices=GeographyLevel.choices), allow_empty=False
    )
    permissions = serializers.ListField(
        child=serializers.ChoiceField(choices=PERMISSION_CHOICES), allow_empty=False
    )
    category = serializers.ChoiceField(choices=RoleCategory.choices, required=False)
    supervising_role = serializers.SlugRelatedField(
        slug_field="code",
        queryset=RoleDefinition.objects.all(),
        required=False,
        allow_null=True,
    )
    created_by = serializers.CharField(source="created_by.access_id", read_only=True, default=None)
    active_assignments = serializers.IntegerField(read_only=True, default=0)

    class Meta:
        model = RoleDefinition
        fields = [
            "code",
            "name",
            "description",
            "category",
            "permitted_levels",
            "permissions",
            "supervising_role",
            "is_system",
            "is_protected",
            "status",
            "archived_at",
            "archive_reason",
            "created_by",
            "active_assignments",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "code",
            "is_system",
            "is_protected",
            "status",
            "archived_at",
            "archive_reason",
            "created_at",
            "updated_at",
        ]


class AssignmentSerializer(serializers.ModelSerializer):
    id = serializers.UUIDField(source="uuid", read_only=True)
    member = serializers.CharField(source="member.membership_number", read_only=True)
    member_name = serializers.CharField(source="member.full_name", read_only=True)
    role = serializers.CharField(source="role.code", read_only=True)
    role_name = serializers.CharField(source="role.name", read_only=True)
    supervising_role = serializers.CharField(
        source="role.supervising_role.code", read_only=True, default=None
    )
    areas = serializers.SerializerMethodField()
    permissions = serializers.SerializerMethodField()
    state = serializers.SerializerMethodField(
        help_text="active, scheduled, expired, suspended, declined, resigned or revoked."
    )
    granted_by = serializers.CharField(source="granted_by.access_id", read_only=True, default=None)

    class Meta:
        model = RoleAssignment
        fields = [
            "id",
            "member",
            "member_name",
            "role",
            "role_name",
            "supervising_role",
            "areas",
            "restricted_permissions",
            "permissions",
            "starts_at",
            "ends_at",
            "state",
            "status",
            "status_reason",
            "status_changed_at",
            "accepted_at",
            "granted_by",
            "authority",
            "created_at",
        ]
        read_only_fields = fields

    @extend_schema_field(OpenApiTypes.OBJECT)
    def get_areas(self, assignment) -> list:
        return [scope_payload(area) for area in assignment.areas.all()]

    def get_permissions(self, assignment) -> list[str]:
        return sorted(assignment.permission_set())

    def get_state(self, assignment) -> str:
        return assignment.effective_status()


class AppointSerializer(serializers.Serializer):
    member = serializers.CharField(help_text="Membership number, e.g. TGCG-000123")
    role = serializers.CharField(help_text="Role code")
    areas = serializers.ListField(child=ScopeField(), allow_empty=False)
    restricted_permissions = serializers.ListField(
        child=serializers.ChoiceField(choices=PERMISSION_CHOICES),
        required=False,
        allow_null=True,
    )
    starts_at = serializers.DateTimeField(required=False, allow_null=True)
    ends_at = serializers.DateTimeField(required=False, allow_null=True)
    reason = serializers.CharField(required=False, allow_blank=True, max_length=1000)


class AccessReasonSerializer(serializers.Serializer):
    reason = serializers.CharField(required=False, allow_blank=True, max_length=1000)


class RestrictSerializer(serializers.Serializer):
    permissions = serializers.ListField(
        child=serializers.ChoiceField(choices=PERMISSION_CHOICES),
        allow_null=True,
        help_text="The permissions to keep; null lifts the restriction.",
    )


class AuditEventSerializer(serializers.ModelSerializer):
    actor = serializers.CharField(source="actor_label")

    class Meta:
        model = AuditEvent
        fields = [
            "id",
            "created_at",
            "actor",
            "action",
            "target_type",
            "target_id",
            "target_label",
            "scope",
            "changes",
            "reason",
            "provenance",
            "hash",
        ]
        read_only_fields = fields


class GrantSerializer(serializers.Serializer):
    """One source of the signed-in user's access (an appointment or a legacy role)."""

    source = serializers.CharField()
    assignment = serializers.CharField(source="key")
    role = serializers.CharField(source="role_code")
    role_name = serializers.CharField()
    protected = serializers.BooleanField()
    areas = serializers.SerializerMethodField()
    permissions = serializers.SerializerMethodField()

    @extend_schema_field(OpenApiTypes.OBJECT)
    def get_areas(self, grant) -> list:
        return [scope_payload(area) for area in grant.areas]

    def get_permissions(self, grant) -> list[str]:
        return sorted(grant.permissions)

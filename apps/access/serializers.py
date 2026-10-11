from django.utils import timezone
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from apps.core.choices import GeographyLevel, Role
from apps.core.permissions import Capability
from apps.geography.scope import ScopeField, scope_payload

from .models import (
    AdminActionApproval,
    AdminActionKind,
    AdminActionRequest,
    AdminActionStatus,
    AssignmentStatus,
    AuditEvent,
    DelegationGrant,
    GrantStatus,
    RoleAssignment,
    RoleCategory,
    RoleDefinition,
)

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


class DelegationSerializer(serializers.ModelSerializer):
    id = serializers.UUIDField(source="uuid", read_only=True)
    grantor = serializers.CharField(source="grantor.access_id", read_only=True)
    grantor_name = serializers.CharField(source="grantor.full_name", read_only=True)
    recipient = serializers.CharField(source="recipient.membership_number", read_only=True)
    recipient_name = serializers.CharField(source="recipient.full_name", read_only=True)
    areas = serializers.SerializerMethodField()
    state = serializers.SerializerMethodField(help_text="active, scheduled, expired or revoked.")
    revoked_by = serializers.CharField(source="revoked_by.access_id", read_only=True, default=None)
    source_grant = serializers.UUIDField(source="source_grant.uuid", read_only=True, default=None)
    source_assignment = serializers.UUIDField(
        source="source_assignment.uuid", read_only=True, default=None
    )

    class Meta:
        model = DelegationGrant
        fields = [
            "id",
            "grantor",
            "grantor_name",
            "recipient",
            "recipient_name",
            "permissions",
            "categories",
            "levels",
            "areas",
            "can_redelegate",
            "depth",
            "starts_at",
            "ends_at",
            "state",
            "status",
            "revoked_at",
            "revoked_by",
            "revoke_reason",
            "source",
            "source_grant",
            "source_assignment",
            "reason",
            "created_at",
        ]
        read_only_fields = fields

    @extend_schema_field(OpenApiTypes.OBJECT)
    def get_areas(self, grant) -> list:
        return [scope_payload(area) for area in grant.areas.all()]

    def get_state(self, grant) -> str:
        if grant.status != GrantStatus.ACTIVE:
            return grant.status
        now = timezone.now()
        if grant.ends_at and now >= grant.ends_at:
            return "expired"
        return "scheduled" if now < grant.starts_at else "active"


class DelegateSerializer(serializers.Serializer):
    recipient = serializers.CharField(help_text="Membership number, e.g. TGCG-000123")
    permissions = serializers.ListField(
        child=serializers.ChoiceField(choices=PERMISSION_CHOICES),
        allow_empty=False,
        help_text="Powers to pass on (assignRoles, manageRoles, manageAssignments, "
        "delegateAuthority) and the ceiling for the roles they cover.",
    )
    categories = serializers.ListField(
        child=serializers.ChoiceField(choices=RoleCategory.choices), allow_empty=False
    )
    levels = serializers.ListField(
        child=serializers.ChoiceField(choices=GeographyLevel.choices), allow_empty=False
    )
    areas = serializers.ListField(child=ScopeField(), allow_empty=False)
    can_redelegate = serializers.BooleanField(default=False)
    depth = serializers.IntegerField(
        min_value=0, max_value=10, default=0, help_text="Further levels of delegation allowed."
    )
    starts_at = serializers.DateTimeField(required=False, allow_null=True)
    ends_at = serializers.DateTimeField(required=False, allow_null=True)
    reason = serializers.CharField(required=False, allow_blank=True, max_length=1000)


class AdminApprovalSerializer(serializers.ModelSerializer):
    approver = serializers.CharField(source="approver.access_id", read_only=True)
    approver_name = serializers.CharField(source="approver.full_name", read_only=True)

    class Meta:
        model = AdminActionApproval
        fields = ["approver", "approver_name", "created_at"]
        read_only_fields = fields


class AdminActionSerializer(serializers.ModelSerializer):
    id = serializers.UUIDField(source="uuid", read_only=True)
    target = serializers.CharField(source="target.access_id", read_only=True)
    target_name = serializers.CharField(source="target.full_name", read_only=True)
    requested_by = serializers.CharField(source="requested_by.access_id", read_only=True)
    cancelled_by = serializers.CharField(
        source="cancelled_by.access_id", read_only=True, default=None
    )
    approvals = AdminApprovalSerializer(many=True, read_only=True)

    class Meta:
        model = AdminActionRequest
        fields = [
            "id",
            "target",
            "target_name",
            "kind",
            "reason",
            "requested_by",
            "status",
            "approvals",
            "executed_at",
            "cancelled_at",
            "cancelled_by",
            "cancel_reason",
            "created_at",
        ]
        read_only_fields = fields


class AdminActionCreateSerializer(serializers.Serializer):
    target = serializers.CharField(help_text="Access ID of the National Administrator concerned")
    kind = serializers.ChoiceField(choices=AdminActionKind.choices)
    reason = serializers.CharField(max_length=1000)


class AdministratorSerializer(serializers.Serializer):
    """A National Administrator account and where its standing comes from."""

    access_id = serializers.CharField()
    full_name = serializers.CharField()
    state = serializers.SerializerMethodField(help_text="active, suspended or inactive.")
    legacy = serializers.SerializerMethodField(help_text="Holds the former fixed role.")
    appointments = serializers.SerializerMethodField()
    pending_request = serializers.SerializerMethodField()

    def get_state(self, user) -> str:
        from .services import administrator_state

        return administrator_state(user, self.context.get("active"))

    def get_legacy(self, user) -> bool:
        return user.role == Role.NATIONAL_ADMINISTRATOR

    def get_appointments(self, user) -> list[str]:
        return [
            str(uuid)
            for uuid in RoleAssignment.objects.filter(
                member__user=user,
                role__is_protected=True,
                status__in=[AssignmentStatus.ACTIVE, AssignmentStatus.SUSPENDED],
            ).values_list("uuid", flat=True)
        ]

    def get_pending_request(self, user) -> str | None:
        pending = (
            AdminActionRequest.objects.filter(target=user, status=AdminActionStatus.PENDING)
            .values_list("uuid", flat=True)
            .first()
        )
        return str(pending) if pending else None

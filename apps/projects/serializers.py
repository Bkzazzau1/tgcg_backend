from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from apps.access import engine
from apps.core.permissions import Capability
from apps.geography.scope import SCOPE_SELECT_RELATED, ScopeField, scope_payload
from apps.results.models import Election

from . import services
from .models import Participation, Project

C = Capability


class ParticipationSerializer(serializers.ModelSerializer):
    id = serializers.UUIDField(source="uuid", read_only=True)
    project = serializers.UUIDField(source="project.uuid", read_only=True)
    project_name = serializers.CharField(source="project.name", read_only=True)
    member = serializers.CharField(source="member.membership_number", read_only=True)
    member_name = serializers.CharField(source="member.full_name", read_only=True)
    place = ScopeField(read_only=True)
    assigned_by = serializers.CharField(source="assigned_by.access_id", read_only=True)

    class Meta:
        model = Participation
        fields = [
            "id",
            "project",
            "project_name",
            "member",
            "member_name",
            "place",
            "status",
            "assigned_by",
            "created_at",
            "responded_at",
            "ended_at",
            "reason",
        ]
        read_only_fields = fields


class ElectionRefSerializer(serializers.ModelSerializer):
    class Meta:
        model = Election
        fields = ["code", "name"]


class ProjectSerializer(serializers.ModelSerializer):
    """A project as the requesting account sees it: `my_participation` is
    their own place in it, if any; `can_manage` and `can_assign` say what
    they may do."""

    id = serializers.UUIDField(source="uuid", read_only=True)
    status = serializers.SerializerMethodField()
    areas = serializers.SerializerMethodField()
    election = ElectionRefSerializer(read_only=True)
    my_participation = serializers.SerializerMethodField()
    can_manage = serializers.SerializerMethodField()
    can_assign = serializers.SerializerMethodField()

    class Meta:
        model = Project
        fields = [
            "id",
            "name",
            "description",
            "starts_at",
            "ends_at",
            "status",
            "areas",
            "election",
            "check_place",
            "cancelled_at",
            "cancel_reason",
            "my_participation",
            "can_manage",
            "can_assign",
        ]
        read_only_fields = fields

    def _user(self):
        request = self.context.get("request")
        return request.user if request else None

    def get_status(self, project) -> str:
        return project.status_at()

    @extend_schema_field(serializers.ListField(child=serializers.DictField()))
    def get_areas(self, project):
        return [scope_payload(area) for area in project.areas.all()]

    @extend_schema_field(ParticipationSerializer(allow_null=True))
    def get_my_participation(self, project):
        user = self._user()
        member = getattr(user, "member", None) if user else None
        if member is None:
            return None
        mine = (
            Participation.objects.filter(project=project, member=member)
            .select_related("project", "member", "assigned_by", *SCOPE_SELECT_RELATED)
            .order_by("-created_at")
            .first()
        )
        return ParticipationSerializer(mine).data if mine else None

    def get_can_manage(self, project) -> bool:
        user = self._user()
        return bool(user and services.can_manage(user))

    def get_can_assign(self, project) -> bool:
        user = self._user()
        if user is None:
            return False
        areas = engine.areas_for(user, C.ASSIGN_PROJECT_PARTICIPANTS)
        return any(services.overlaps(area, mine) for area in project.areas.all() for mine in areas)


class ProjectWriteSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=120)
    description = serializers.CharField(max_length=2000, required=False, allow_blank=True)
    starts_at = serializers.DateTimeField()
    ends_at = serializers.DateTimeField()
    areas = serializers.ListField(child=ScopeField(), allow_empty=False)
    election = serializers.SlugRelatedField(
        slug_field="code", queryset=Election.objects.all(), required=False, allow_null=True
    )
    check_place = serializers.BooleanField(required=False, default=True)


class ProjectUpdateSerializer(ProjectWriteSerializer):
    def __init__(self, *args, **kwargs):
        kwargs["partial"] = True
        super().__init__(*args, **kwargs)
        self.fields["check_place"].default = serializers.empty


class AssignSerializer(serializers.Serializer):
    member = serializers.CharField(max_length=16, help_text="Membership number.")
    place = ScopeField(
        required=False, help_text="Where they work; by default their own polling unit."
    )


class ProjectReasonSerializer(serializers.Serializer):
    reason = serializers.CharField(required=False, allow_blank=True, max_length=1000)

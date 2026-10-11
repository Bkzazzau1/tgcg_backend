"""Roles, appointments, the signed-in member's own appointments, and the audit log.

Views only check that the caller may use the endpoint at all; every rule
about who may do what, where, lives in services.py and engine.py.
"""

from django.db.models import Count, Q
from django.shortcuts import get_object_or_404
from drf_spectacular.utils import extend_schema
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core.permissions import Capability, requires
from apps.geography.scope import within_scope_any_q
from apps.membership.models import Member

from . import audit, engine, services
from .models import AssignmentStatus, AuditEvent, RoleAssignment, RoleAssignmentArea, RoleDefinition
from .registry import PERMISSIONS
from .serializers import (
    AccessReasonSerializer,
    AppointSerializer,
    AssignmentSerializer,
    AuditEventSerializer,
    GrantSerializer,
    PermissionInfoSerializer,
    RestrictSerializer,
    RoleDefinitionSerializer,
)

C = Capability
ROLE_READERS = (C.VIEW_ROLES, C.MANAGE_ROLES, C.ASSIGN_ROLES, C.MANAGE_ASSIGNMENTS)


class PermissionRegistryView(APIView):
    """Every permission code a role can hold, by module and action."""

    permission_classes = [requires(*ROLE_READERS)]

    @extend_schema(responses=PermissionInfoSerializer(many=True))
    def get(self, request):
        return Response(PermissionInfoSerializer(PERMISSIONS.values(), many=True).data)


class RoleViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    mixins.UpdateModelMixin,
    viewsets.GenericViewSet,
):
    serializer_class = RoleDefinitionSerializer
    lookup_field = "code"
    http_method_names = ["get", "post", "patch", "head", "options"]
    filterset_fields = {"status": ["exact"], "category": ["exact"], "is_system": ["exact"]}
    search_fields = ["name", "description", "code"]
    ordering_fields = ["name", "created_at"]

    def get_permissions(self):
        if self.action in ("create", "partial_update", "archive", "restore"):
            return [requires(C.MANAGE_ROLES)()]
        return [requires(*ROLE_READERS)()]

    def get_queryset(self):
        now_active = Q(
            assignments__status=AssignmentStatus.ACTIVE,
        )
        return RoleDefinition.objects.select_related("supervising_role", "created_by").annotate(
            active_assignments=Count("assignments", filter=now_active, distinct=True)
        )

    def perform_create(self, serializer):
        serializer.instance = services.create_role(
            actor=self.request.user, **serializer.validated_data
        )

    def perform_update(self, serializer):
        serializer.instance = services.update_role(
            actor=self.request.user, role=serializer.instance, **serializer.validated_data
        )

    @extend_schema(request=AccessReasonSerializer, responses=RoleDefinitionSerializer)
    @action(detail=True, methods=["post"])
    def archive(self, request, code=None):
        reason = AccessReasonSerializer(data=request.data)
        reason.is_valid(raise_exception=True)
        role = services.archive_role(
            actor=request.user,
            role=self.get_object(),
            reason=reason.validated_data.get("reason", ""),
        )
        return Response(RoleDefinitionSerializer(role).data)

    @extend_schema(request=AccessReasonSerializer, responses=RoleDefinitionSerializer)
    @action(detail=True, methods=["post"])
    def restore(self, request, code=None):
        reason = AccessReasonSerializer(data=request.data)
        reason.is_valid(raise_exception=True)
        role = services.restore_role(
            actor=request.user,
            role=self.get_object(),
            reason=reason.validated_data.get("reason", ""),
        )
        return Response(RoleDefinitionSerializer(role).data)


def _assignments():
    return RoleAssignment.objects.select_related(
        "member", "role__supervising_role", "granted_by"
    ).prefetch_related("areas")


class AssignmentViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    viewsets.GenericViewSet,
):
    """Appointments in the caller's areas. POST to appoint a member."""

    serializer_class = AssignmentSerializer
    lookup_field = "uuid"
    filterset_fields = {
        "status": ["exact"],
        "role__code": ["exact"],
        "member__membership_number": ["exact"],
    }
    search_fields = ["member__full_name", "member__membership_number", "role__name"]
    ordering_fields = ["created_at", "starts_at"]

    def get_permissions(self):
        if self.action == "create":
            return [requires(C.ASSIGN_ROLES)()]
        if self.action in ("suspend", "reactivate", "revoke", "restrict"):
            return [requires(C.MANAGE_ASSIGNMENTS)()]
        return [requires(*ROLE_READERS)()]

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return RoleAssignment.objects.none()
        user = self.request.user
        if engine.is_national_administrator(user):
            return _assignments()
        # An appointment is visible when one of its areas lies inside the caller's reach.
        areas = RoleAssignmentArea.objects.filter(within_scope_any_q(user, ROLE_READERS))
        return _assignments().filter(areas__in=areas).distinct()

    @extend_schema(request=AppointSerializer, responses={201: AssignmentSerializer})
    def create(self, request):
        data = AppointSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        values = data.validated_data
        member = get_object_or_404(Member, membership_number=values["member"])
        role = get_object_or_404(RoleDefinition, code=values["role"])
        assignment = services.appoint(
            actor=request.user,
            member=member,
            role=role,
            areas=values["areas"],
            restricted_permissions=values.get("restricted_permissions"),
            starts_at=values.get("starts_at"),
            ends_at=values.get("ends_at"),
            reason=values.get("reason", ""),
        )
        return Response(
            AssignmentSerializer(_assignments().get(pk=assignment.pk)).data,
            status=status.HTTP_201_CREATED,
        )

    def _change(self, request, change):
        reason = AccessReasonSerializer(data=request.data)
        reason.is_valid(raise_exception=True)
        assignment = change(
            actor=request.user,
            assignment=self.get_object(),
            reason=reason.validated_data.get("reason", ""),
        )
        return Response(AssignmentSerializer(_assignments().get(pk=assignment.pk)).data)

    @extend_schema(request=AccessReasonSerializer, responses=AssignmentSerializer)
    @action(detail=True, methods=["post"])
    def suspend(self, request, uuid=None):
        return self._change(request, services.suspend)

    @extend_schema(request=AccessReasonSerializer, responses=AssignmentSerializer)
    @action(detail=True, methods=["post"])
    def reactivate(self, request, uuid=None):
        return self._change(request, services.reactivate)

    @extend_schema(request=AccessReasonSerializer, responses=AssignmentSerializer)
    @action(detail=True, methods=["post"])
    def revoke(self, request, uuid=None):
        return self._change(request, services.revoke)

    @extend_schema(request=RestrictSerializer, responses=AssignmentSerializer)
    @action(detail=True, methods=["post"])
    def restrict(self, request, uuid=None):
        data = RestrictSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        assignment = services.restrict(
            actor=request.user,
            assignment=self.get_object(),
            permissions=data.validated_data["permissions"],
        )
        return Response(AssignmentSerializer(_assignments().get(pk=assignment.pk)).data)

    @extend_schema(responses=AuditEventSerializer(many=True))
    @action(detail=True, methods=["get"])
    def history(self, request, uuid=None):
        assignment = self.get_object()
        events = AuditEvent.objects.filter(
            target_type="roleAssignment", target_id=str(assignment.uuid)
        )
        return Response(AuditEventSerializer(events, many=True).data)


class MyAccessView(APIView):
    """Where the signed-in user's access comes from: each appointment or legacy role."""

    permission_classes = [IsAuthenticated]

    @extend_schema(responses=GrantSerializer(many=True))
    def get(self, request):
        return Response(GrantSerializer(engine.active_grants(request.user), many=True).data)


class MyAssignmentsViewSet(mixins.ListModelMixin, viewsets.GenericViewSet):
    """The signed-in member's own appointments: accept, decline or resign."""

    serializer_class = AssignmentSerializer
    lookup_field = "uuid"
    permission_classes = [IsAuthenticated]
    pagination_class = None

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return RoleAssignment.objects.none()
        return _assignments().filter(member__user=self.request.user)

    def _own(self, request, change, **extra):
        assignment = get_object_or_404(self.get_queryset(), uuid=self.kwargs["uuid"])
        result = change(user=request.user, assignment=assignment, **extra)
        return Response(AssignmentSerializer(_assignments().get(pk=result.pk)).data)

    @extend_schema(request=None, responses=AssignmentSerializer)
    @action(detail=True, methods=["post"])
    def accept(self, request, uuid=None):
        return self._own(request, services.accept)

    @extend_schema(request=AccessReasonSerializer, responses=AssignmentSerializer)
    @action(detail=True, methods=["post"])
    def decline(self, request, uuid=None):
        reason = AccessReasonSerializer(data=request.data)
        reason.is_valid(raise_exception=True)
        return self._own(request, services.decline, reason=reason.validated_data.get("reason", ""))

    @extend_schema(request=AccessReasonSerializer, responses=AssignmentSerializer)
    @action(detail=True, methods=["post"])
    def resign(self, request, uuid=None):
        reason = AccessReasonSerializer(data=request.data)
        reason.is_valid(raise_exception=True)
        return self._own(request, services.resign, reason=reason.validated_data.get("reason", ""))


class AuditEventViewSet(mixins.ListModelMixin, viewsets.GenericViewSet):
    """The security audit log. Readable with viewAudit held nationwide."""

    serializer_class = AuditEventSerializer
    permission_classes = [requires(C.VIEW_AUDIT)]
    filterset_fields = {
        "action": ["exact", "startswith"],
        "target_type": ["exact"],
        "target_id": ["exact"],
        "actor_label": ["exact"],
    }
    search_fields = ["target_label", "reason"]

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return AuditEvent.objects.none()
        user = self.request.user
        national = any(
            C.VIEW_AUDIT in grant.permissions and grant.is_national
            for grant in engine.active_grants(user)
        )
        return AuditEvent.objects.all() if national else AuditEvent.objects.none()

    @extend_schema(responses={200: dict})
    @action(detail=False, methods=["get"])
    def verify(self, request):
        """Check the hash chain: has any event been altered or removed?"""
        if not engine.is_national_administrator(request.user):
            return Response(status=status.HTTP_403_FORBIDDEN)
        broken = audit.verify_chain()
        return Response({"intact": broken is None, "first_broken_event": broken})

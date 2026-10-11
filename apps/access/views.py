"""Roles, appointments, delegated authority, National Administrator
approvals, the signed-in member's own appointments, and the audit log.

Views only check that the caller may use the endpoint at all; every rule
about who may do what, where, lives in services.py and engine.py.
"""

from django.db.models import Count, Q
from django.shortcuts import get_object_or_404
from drf_spectacular.utils import extend_schema
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import BasePermission, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.models import User
from apps.core.permissions import Capability, requires
from apps.geography.scope import any_of, within_scope_any_q
from apps.membership.models import Member

from . import audit, engine, services
from .models import (
    AdminActionRequest,
    AssignmentStatus,
    AuditEvent,
    DelegationGrant,
    RoleAssignment,
    RoleAssignmentArea,
    RoleDefinition,
)
from .registry import PERMISSIONS
from .serializers import (
    AccessReasonSerializer,
    AdminActionCreateSerializer,
    AdminActionSerializer,
    AdministratorSerializer,
    AppointSerializer,
    AssignmentSerializer,
    AuditEventSerializer,
    DelegateSerializer,
    DelegationSerializer,
    GrantSerializer,
    PermissionInfoSerializer,
    RestrictSerializer,
    RoleDefinitionSerializer,
)

C = Capability
ROLE_READERS = (C.VIEW_ROLES, C.MANAGE_ROLES, C.ASSIGN_ROLES, C.MANAGE_ASSIGNMENTS)


class IsNationalAdministrator(BasePermission):
    message = "Only a National Administrator can do this."

    def has_permission(self, request, view):
        user = request.user
        return bool(user and user.is_authenticated and engine.is_national_administrator(user))


class CanSeeAppointments(BasePermission):
    """Holds a role-reading permission somewhere, or holds a role others report to."""

    message = "Your role does not permit this action."

    def has_permission(self, request, view):
        user = request.user
        if not (user and user.is_authenticated):
            return False
        return bool(engine.permissions_for(user) & set(ROLE_READERS)) or services.supervises_any(
            user
        )


def _history(target_type, target_id):
    events = AuditEvent.objects.filter(target_type=target_type, target_id=str(target_id))
    return Response(AuditEventSerializer(events, many=True).data)


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
    """Appointments in the caller's areas, and those they supervise. POST to appoint."""

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
        if self.action in ("revoke", "restrict"):
            return [requires(C.MANAGE_ASSIGNMENTS)()]
        # Supervisors may also see, suspend and reactivate what they supervise.
        return [CanSeeAppointments()]

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return RoleAssignment.objects.none()
        user = self.request.user
        if engine.is_national_administrator(user):
            return _assignments()
        # Visible when one of its areas lies inside the caller's reach, or the
        # caller holds a role above it whose areas include it.
        areas = RoleAssignmentArea.objects.filter(within_scope_any_q(user, ROLE_READERS))
        visible = any_of(Q(areas__in=areas), services.supervised_q(user))
        return _assignments().filter(visible).distinct()

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
        return _history("roleAssignment", self.get_object().uuid)


def _grants():
    return DelegationGrant.objects.select_related(
        "grantor", "recipient", "revoked_by", "source_grant", "source_assignment"
    ).prefetch_related("areas")


class DelegationViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    viewsets.GenericViewSet,
):
    """Delegated authority made or received by the caller, or inside their
    delegating reach. POST to delegate; revoking never cascades."""

    serializer_class = DelegationSerializer
    lookup_field = "uuid"
    filterset_fields = {
        "status": ["exact"],
        "recipient__membership_number": ["exact"],
        "grantor__access_id": ["exact"],
    }
    search_fields = ["recipient__full_name", "recipient__membership_number", "reason"]
    ordering_fields = ["created_at", "starts_at"]

    def get_permissions(self):
        if self.action == "create":
            return [requires(C.DELEGATE_AUTHORITY)()]
        return [IsAuthenticated()]

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return DelegationGrant.objects.none()
        return services.delegations_visible_to(self.request.user)

    @extend_schema(request=DelegateSerializer, responses={201: DelegationSerializer})
    def create(self, request):
        data = DelegateSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        values = dict(data.validated_data)
        recipient = get_object_or_404(Member, membership_number=values.pop("recipient"))
        grant = services.delegate(actor=request.user, recipient=recipient, **values)
        return Response(
            DelegationSerializer(_grants().get(pk=grant.pk)).data,
            status=status.HTTP_201_CREATED,
        )

    @extend_schema(request=AccessReasonSerializer, responses=DelegationSerializer)
    @action(detail=True, methods=["post"])
    def revoke(self, request, uuid=None):
        reason = AccessReasonSerializer(data=request.data)
        reason.is_valid(raise_exception=True)
        grant = services.revoke_delegation(
            actor=request.user,
            grant=self.get_object(),
            reason=reason.validated_data.get("reason", ""),
        )
        return Response(DelegationSerializer(_grants().get(pk=grant.pk)).data)

    @extend_schema(responses=AuditEventSerializer(many=True))
    @action(detail=True, methods=["get"])
    def history(self, request, uuid=None):
        return _history("delegationGrant", self.get_object().uuid)


class AdminActionViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    viewsets.GenericViewSet,
):
    """Requests to suspend or remove a National Administrator.

    The request is its requester's approval; it runs when a second
    administrator approves. The administrator concerned can do neither.
    """

    serializer_class = AdminActionSerializer
    lookup_field = "uuid"
    permission_classes = [IsNationalAdministrator]
    filterset_fields = {"status": ["exact"], "kind": ["exact"]}
    ordering_fields = ["created_at"]

    def get_queryset(self):
        return AdminActionRequest.objects.select_related(
            "target", "requested_by", "cancelled_by"
        ).prefetch_related("approvals__approver")

    @extend_schema(request=AdminActionCreateSerializer, responses={201: AdminActionSerializer})
    def create(self, request):
        data = AdminActionCreateSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        values = data.validated_data
        target = get_object_or_404(User, access_id=values["target"].strip().upper())
        made = services.request_admin_action(
            actor=request.user, target=target, kind=values["kind"], reason=values["reason"]
        )
        return Response(
            AdminActionSerializer(self.get_queryset().get(pk=made.pk)).data,
            status=status.HTTP_201_CREATED,
        )

    @extend_schema(request=None, responses=AdminActionSerializer)
    @action(detail=True, methods=["post"])
    def approve(self, request, uuid=None):
        approved = services.approve_admin_action(actor=request.user, request=self.get_object())
        return Response(AdminActionSerializer(self.get_queryset().get(pk=approved.pk)).data)

    @extend_schema(request=AccessReasonSerializer, responses=AdminActionSerializer)
    @action(detail=True, methods=["post"])
    def cancel(self, request, uuid=None):
        reason = AccessReasonSerializer(data=request.data)
        reason.is_valid(raise_exception=True)
        cancelled = services.cancel_admin_action(
            actor=request.user,
            request=self.get_object(),
            reason=reason.validated_data.get("reason", ""),
        )
        return Response(AdminActionSerializer(self.get_queryset().get(pk=cancelled.pk)).data)


class AdministratorViewSet(mixins.ListModelMixin, viewsets.GenericViewSet):
    """National Administrator accounts and their standing; reinstate a suspended one."""

    serializer_class = AdministratorSerializer
    lookup_field = "access_id"
    permission_classes = [IsNationalAdministrator]
    pagination_class = None

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return User.objects.none()
        return services.administrator_accounts().order_by("full_name")

    def get_serializer_context(self):
        context = super().get_serializer_context()
        if not getattr(self, "swagger_fake_view", False):
            context["active"] = services.active_national_administrators()
        return context

    @extend_schema(request=AccessReasonSerializer, responses=AdministratorSerializer)
    @action(detail=True, methods=["post"])
    def reinstate(self, request, access_id=None):
        reason = AccessReasonSerializer(data=request.data)
        reason.is_valid(raise_exception=True)
        target = services.reinstate_administrator(
            actor=request.user,
            target=self.get_object(),
            reason=reason.validated_data.get("reason", ""),
        )
        return Response(AdministratorSerializer(target, context=self.get_serializer_context()).data)


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

from django.db.models import Prefetch
from drf_spectacular.utils import extend_schema
from rest_framework import mixins, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from apps.core.permissions import Capability, requires
from apps.geography.scope import SCOPE_FIELDS, within_scope_q

from . import services
from .models import AccreditedAgent, Member
from .serializers import (
    AccreditationEventSerializer,
    AgentSerializer,
    MemberCreateSerializer,
    MemberSerializer,
    ReadinessSerializer,
    StatusChangeSerializer,
    flatten_scope,
)

C = Capability
MEMBERSHIP_READERS = (C.MANAGE_MEMBERSHIP, C.ACCREDIT_AGENTS, C.MANAGE_AGENT_ASSIGNMENTS)

SCOPE_FILTERS = {
    "scope_level": ["exact"],
    "zone__code": ["exact"],
    "state__code": ["exact"],
    "senatorial_district__code": ["exact"],
    "lga__code": ["exact"],
    "ward__code": ["exact"],
    "polling_unit__code": ["exact"],
}


class MemberViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    mixins.UpdateModelMixin,
    viewsets.GenericViewSet,
):
    """Members registered within the caller's scope."""

    permission_classes = [requires(C.MANAGE_MEMBERSHIP, safe=MEMBERSHIP_READERS)]
    lookup_field = "membership_number"
    http_method_names = ["get", "post", "patch", "head", "options"]
    filterset_fields = {"status": ["exact"], "origin": ["exact"], **SCOPE_FILTERS}
    search_fields = ["full_name", "phone_number", "membership_number"]
    ordering_fields = ["created_at", "full_name"]

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):  # schema generation
            return Member.objects.none()
        return (
            Member.objects.filter(within_scope_q(self.request.user))
            .select_related(*SCOPE_FIELDS)
            .prefetch_related(Prefetch("accreditations"))
        )

    def get_serializer_class(self):
        return MemberCreateSerializer if self.action == "create" else MemberSerializer

    def perform_create(self, serializer):
        serializer.instance = services.create_member(
            actor=self.request.user, **flatten_scope(serializer.validated_data)
        )

    def perform_update(self, serializer):
        serializer.instance = services.update_member(
            actor=self.request.user,
            member=serializer.instance,
            **flatten_scope(serializer.validated_data),
        )


class AgentViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    viewsets.GenericViewSet,
):
    """Agent accreditations assigned within the caller's scope."""

    lookup_field = "agent_id"
    serializer_class = AgentSerializer
    filterset_fields = {"status": ["exact"], "role": ["exact"], **SCOPE_FILTERS}
    search_fields = ["agent_id", "member__full_name", "member__membership_number"]
    ordering_fields = ["created_at", "status"]

    def get_permissions(self):
        if self.action in ("create", "change_status"):
            classes = [requires(C.ACCREDIT_AGENTS, safe=MEMBERSHIP_READERS)]
        elif self.action == "readiness":
            classes = [requires(C.ACCREDIT_AGENTS, C.MANAGE_AGENT_ASSIGNMENTS)]
        else:
            classes = [requires(*MEMBERSHIP_READERS)]
        return [permission() for permission in classes]

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):  # schema generation
            return AccreditedAgent.objects.none()
        return AccreditedAgent.objects.filter(within_scope_q(self.request.user)).select_related(
            "member", *SCOPE_FIELDS
        )

    def perform_create(self, serializer):
        data = flatten_scope(serializer.validated_data)
        member = data.pop("member")
        serializer.instance = services.accredit(actor=self.request.user, member=member, **data)

    @extend_schema(request=ReadinessSerializer, responses=AgentSerializer)
    @action(detail=True, methods=["patch"])
    def readiness(self, request, agent_id=None):
        agent = self.get_object()
        serializer = ReadinessSerializer(agent, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        agent = services.update_readiness(
            actor=request.user, agent=agent, **serializer.validated_data
        )
        return Response(AgentSerializer(agent, context=self.get_serializer_context()).data)

    @extend_schema(request=StatusChangeSerializer, responses=AgentSerializer)
    @action(detail=True, methods=["post"], url_path="status")
    def change_status(self, request, agent_id=None):
        agent = self.get_object()
        serializer = StatusChangeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        agent = services.change_status(actor=request.user, agent=agent, **serializer.validated_data)
        return Response(AgentSerializer(agent, context=self.get_serializer_context()).data)

    @extend_schema(responses=AccreditationEventSerializer(many=True))
    @action(detail=True, methods=["get"])
    def events(self, request, agent_id=None):
        agent = self.get_object()
        events = agent.events.select_related("actor")
        return Response(AccreditationEventSerializer(events, many=True).data)

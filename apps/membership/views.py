from django.db.models import Prefetch
from drf_spectacular.utils import extend_schema
from rest_framework import mixins, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from apps.core.permissions import Capability, requires
from apps.geography.scope import SCOPE_FIELDS, within_scope_q

from . import services
from .models import AccreditedAgent, Member, VolunteerApplication
from .registration_views import _photo_response
from .serializers import (
    AccreditationEventSerializer,
    AgentSerializer,
    DecisionSerializer,
    MemberSerializer,
    ReadinessSerializer,
    StatusChangeSerializer,
    VolunteerApplicationSerializer,
    flatten_scope,
)

C = Capability
MEMBERSHIP_READERS = (
    C.MANAGE_MEMBERSHIP,
    C.REGISTER_MEMBERS,
    C.ACCREDIT_AGENTS,
    C.MANAGE_AGENT_ASSIGNMENTS,
)

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
    mixins.UpdateModelMixin,
    viewsets.GenericViewSet,
):
    """Members registered within the caller's scope.

    New members are created through registration: `POST /register/` (self,
    in the app) or `POST /members/register/` (a coordinator, in person).
    """

    serializer_class = MemberSerializer
    permission_classes = [requires(C.MANAGE_MEMBERSHIP, safe=MEMBERSHIP_READERS)]
    lookup_field = "membership_number"
    http_method_names = ["get", "patch", "head", "options"]
    filterset_fields = {
        "status": ["exact"],
        "origin": ["exact"],
        "registration_channel": ["exact"],
        "gender": ["exact"],
        **SCOPE_FILTERS,
    }
    search_fields = ["full_name", "phone_number", "membership_number"]
    ordering_fields = ["created_at", "full_name"]

    def get_permissions(self):
        if self.action == "photo":
            return [requires(*MEMBERSHIP_READERS)()]
        return super().get_permissions()

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):  # schema generation
            return Member.objects.none()
        return (
            Member.objects.filter(within_scope_q(self.request.user))
            .select_related(*SCOPE_FIELDS)
            .prefetch_related(Prefetch("accreditations"))
        )

    @extend_schema(responses={(200, "image/jpeg"): bytes})
    @action(detail=True, methods=["get"])
    def photo(self, request, membership_number=None):
        return _photo_response(self.get_object())

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


class VolunteerViewSet(mixins.ListModelMixin, viewsets.GenericViewSet):
    """Members' applications to serve as agents, for coordinators to decide."""

    serializer_class = VolunteerApplicationSerializer
    lookup_field = "uuid"
    filterset_fields = {
        "status": ["exact"],
        **{f"member__{k}": v for k, v in SCOPE_FILTERS.items()},
    }

    def get_permissions(self):
        if self.action in ("accept", "decline"):
            return [requires(C.ACCREDIT_AGENTS)()]
        return [requires(*MEMBERSHIP_READERS)()]

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return VolunteerApplication.objects.none()
        return VolunteerApplication.objects.filter(
            within_scope_q(self.request.user, prefix="member__")
        ).select_related("member__polling_unit", "agent")

    def _decide(self, request, decide):
        serializer = DecisionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        application = decide(
            actor=request.user,
            application=self.get_object(),
            reason=serializer.validated_data.get("reason", ""),
        )
        return Response(VolunteerApplicationSerializer(application).data)

    @extend_schema(request=DecisionSerializer, responses=VolunteerApplicationSerializer)
    @action(detail=True, methods=["post"])
    def accept(self, request, uuid=None):
        return self._decide(request, services.accept_volunteer)

    @extend_schema(request=DecisionSerializer, responses=VolunteerApplicationSerializer)
    @action(detail=True, methods=["post"])
    def decline(self, request, uuid=None):
        return self._decide(request, services.decline_volunteer)

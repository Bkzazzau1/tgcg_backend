from collections import Counter
from datetime import datetime, time, timedelta

from django.conf import settings
from django.db.models import OuterRef, Q, Subquery
from django.utils import timezone
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema, inline_serializer
from rest_framework import mixins, serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.models import User
from apps.core.choices import GeographyLevel, MembershipState, Role, VerificationStatus
from apps.core.permissions import Capability, requires
from apps.geography.gps import GpsFixSerializer, GpsReadSerializer, LocationStatus
from apps.geography.scope import (
    LEVEL_FIELD,
    ScopeRef,
    actor_area,
    any_of,
    scope_contains,
    scope_payload,
    within_scope_any_q,
    within_scope_q,
)
from apps.results.collation import CHILD_LEVEL, _units_within

from . import services
from .models import AgentCheckIn, DutySession, LocationPing

C = Capability


class CheckInSerializer(serializers.ModelSerializer):
    id = serializers.UUIDField(source="uuid", read_only=True)
    agent = serializers.CharField(source="agent.access_id", read_only=True)
    agent_name = serializers.CharField(source="agent.full_name", read_only=True)
    polling_unit = serializers.CharField(source="polling_unit.code", read_only=True)
    polling_unit_name = serializers.CharField(source="polling_unit.name", read_only=True)
    location = GpsReadSerializer(source="*", read_only=True)
    confirmed_by = serializers.CharField(
        source="confirmed_by.access_id", read_only=True, default=None
    )

    class Meta:
        model = AgentCheckIn
        fields = [
            "id",
            "agent",
            "agent_name",
            "polling_unit",
            "polling_unit_name",
            "location",
            "confirmed_by",
            "confirmed_at",
            "client_id",
            "created_at",
        ]


class CheckInCreateSerializer(serializers.Serializer):
    gps = GpsFixSerializer()
    client_id = serializers.CharField(max_length=64, required=False, allow_null=True)


class CheckInViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    """Agent check-ins. Agents see their own; situation-room users see their scope."""

    serializer_class = CheckInSerializer
    lookup_field = "uuid"
    filterset_fields = {
        "location_status": ["exact"],
        "polling_unit__code": ["exact"],
        "polling_unit__ward__code": ["exact"],
        "polling_unit__ward__lga__code": ["exact"],
        "polling_unit__ward__lga__state__code": ["exact"],
    }
    ordering_fields = ["created_at"]

    def get_permissions(self):
        if self.action == "confirm_location":
            return [requires(C.CONFIRM_AGENT_LOCATION)()]
        return [IsAuthenticated()]

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return AgentCheckIn.objects.none()
        user = self.request.user
        queryset = AgentCheckIn.objects.filter(
            any_of(
                within_scope_any_q(
                    user, (C.VIEW_SITUATION_ROOM, C.CONFIRM_AGENT_LOCATION), prefix="agent__"
                ),
                Q(agent=user),
            )
        )
        return queryset.select_related("agent", "polling_unit", "confirmed_by")

    @extend_schema(request=CheckInCreateSerializer, responses={201: CheckInSerializer})
    def create(self, request):
        serializer = CheckInCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        check = services.check_in(agent=request.user, **serializer.validated_data)
        return Response(CheckInSerializer(check).data, status=status.HTTP_201_CREATED)

    @extend_schema(request=None, responses=CheckInSerializer)
    @action(detail=True, methods=["post"], url_path="confirm-location")
    def confirm_location(self, request, uuid=None):
        check = services.confirm_check_in(actor=request.user, check_in=self.get_object())
        return Response(CheckInSerializer(check).data)


class DutyView(APIView):
    """Start or stop sharing live location (field agents and observers)."""

    @extend_schema(
        request=inline_serializer(
            "DutyRequest",
            {
                "on_duty": serializers.BooleanField(),
                "at": serializers.DateTimeField(
                    required=False, help_text="Phone time of the change (for offline sync)"
                ),
                "gps": GpsFixSerializer(required=False, allow_null=True),
            },
        ),
        responses=inline_serializer(
            "DutyState",
            {"on_duty": serializers.BooleanField(), "since": serializers.DateTimeField()},
        ),
    )
    def post(self, request):
        on_duty = request.data.get("on_duty") in (True, "true", "True", "1", 1)
        at = None
        if request.data.get("at"):
            stamp = serializers.DateTimeField()
            at = stamp.to_internal_value(request.data["at"])
        if on_duty:
            gps = None
            if request.data.get("gps"):
                fix = GpsFixSerializer(data=request.data["gps"])
                fix.is_valid(raise_exception=True)
                gps = fix.validated_data
            session = services.start_duty(user=request.user, gps=gps, at=at)
            return Response({"on_duty": True, "since": session.started_at})
        services.stop_duty(user=request.user, at=at)
        return Response({"on_duty": False, "since": None})

    @extend_schema(responses={200: OpenApiTypes.OBJECT})
    def get(self, request):
        session = DutySession.objects.filter(user=request.user, ended_at__isnull=True).first()
        return Response({"on_duty": session is not None, "since": session and session.started_at})


class PingsView(APIView):
    """Upload queued on-duty location readings (up to 500 per request)."""

    @extend_schema(
        request=inline_serializer("PingBatch", {"pings": GpsFixSerializer(many=True)}),
        responses=inline_serializer("PingsStored", {"stored": serializers.IntegerField()}),
    )
    def post(self, request):
        serializer = GpsFixSerializer(data=request.data.get("pings", []), many=True)
        serializer.is_valid(raise_exception=True)
        if len(serializer.validated_data) > 500:
            return Response(
                {"pings": "Send at most 500 readings at a time."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        stored = services.record_pings(user=request.user, fixes=serializer.validated_data)
        return Response({"stored": stored})


def _area(request, permission=Capability.VIEW_SITUATION_ROOM):
    """The area asked for (level and code), or the user's own for `permission`."""
    level = request.query_params.get("level")
    scope = (
        ScopeRef.resolve(level, request.query_params.get("code"))
        if level
        else actor_area(request.user, permission)
    )
    if not scope_contains(request.user, scope, permission=permission):
        raise PermissionDenied("This area is outside your operational scope.")
    return scope


AREA_PARAMS = [
    OpenApiParameter(
        "level", str, enum=GeographyLevel.values, description="Defaults to your scope"
    ),
    OpenApiParameter("code", str, description="Unit code for the level"),
]


# Memberships that share no location and count for nothing.
STOPPED = [MembershipState.SUSPENDED, MembershipState.DEACTIVATED]


class LiveAgentsView(APIView):
    """On-duty agents with their latest position, for the live map."""

    permission_classes = [requires(C.VIEW_SITUATION_ROOM)]

    @extend_schema(parameters=AREA_PARAMS, responses={200: OpenApiTypes.OBJECT})
    def get(self, request):
        scope = _area(request)
        latest = LocationPing.objects.filter(session=OuterRef("pk")).order_by("-gps_captured_at")
        sessions = (
            DutySession.objects.filter(ended_at__isnull=True)
            .filter(within_scope_q(scope, prefix="user__"))
            .exclude(user__member__membership_state__in=STOPPED)
            .select_related("user__polling_unit")
            .annotate(ping_id=Subquery(latest.values("pk")[:1]))
        )
        pings = LocationPing.objects.in_bulk([s.ping_id for s in sessions if s.ping_id])
        stale_before = timezone.now() - timedelta(minutes=settings.LIVE_STALE_MINUTES)
        agents = []
        for session in sessions:
            ping = pings.get(session.ping_id)
            user = session.user
            agents.append(
                {
                    "agent": user.access_id,
                    "name": user.full_name,
                    "role": user.role,
                    "polling_unit": user.polling_unit.code if user.polling_unit else None,
                    "on_duty_since": session.started_at,
                    "position": GpsReadSerializer(ping).data if ping else None,
                    "stale": ping is None or ping.gps_captured_at < stale_before,
                }
            )
        return Response({"scope": scope_payload(scope), "count": len(agents), "agents": agents})


class CoverageView(APIView):
    """Per-area check-in coverage of polling-unit agents for one day."""

    permission_classes = [requires(C.VIEW_SITUATION_ROOM)]

    @extend_schema(
        parameters=[
            *AREA_PARAMS,
            OpenApiParameter("date", OpenApiTypes.DATE, description="Defaults to today"),
        ],
        responses={200: OpenApiTypes.OBJECT},
    )
    def get(self, request):
        scope = _area(request)
        day = request.query_params.get("date")
        day = datetime.fromisoformat(day).date() if day else timezone.localdate()
        tz = timezone.get_current_timezone()
        start = timezone.make_aware(datetime.combine(day, time.min), tz)

        # Only agents who can work count: verified members in good standing.
        agents = (
            User.objects.filter(
                role=Role.POLLING_UNIT_AGENT,
                is_active=True,
                polling_unit__isnull=False,
                member__verification_status=VerificationStatus.VERIFIED,
            )
            .filter(within_scope_q(scope))
            .exclude(member__membership_state__in=STOPPED)
        )
        check_ins = (
            AgentCheckIn.objects.filter(
                created_at__gte=start, created_at__lt=start + timedelta(days=1)
            )
            .filter(within_scope_q(scope, prefix="agent__"))
            .order_by("created_at")
            .values_list("agent_id", "location_status")
        )
        latest = dict(check_ins)  # later check-ins overwrite earlier ones
        on_duty = set(
            DutySession.objects.filter(ended_at__isnull=True)
            .filter(within_scope_q(scope, prefix="user__"))
            .exclude(user__member__membership_state__in=STOPPED)
            .values_list("user_id", flat=True)
        )

        def tally(agent_ids):
            statuses = Counter(latest[a] for a in agent_ids if a in latest)
            checked = sum(statuses.values())
            return {
                "agents": len(agent_ids),
                "checked_in": checked,
                "not_checked_in": len(agent_ids) - checked,
                "on_site": statuses[LocationStatus.ON_SITE],
                "away": statuses[LocationStatus.AWAY],
                "unverified": statuses[LocationStatus.UNVERIFIED],
                "suspect": statuses[LocationStatus.SUSPECT],
                "on_duty_now": sum(1 for a in agent_ids if a in on_duty),
            }

        body = {
            "date": day,
            "scope": scope_payload(scope),
            **tally(list(agents.values_list("pk", flat=True))),
        }
        if scope.scope_level in CHILD_LEVEL:
            child_level = CHILD_LEVEL[scope.scope_level]
            child_field = LEVEL_FIELD[child_level]
            by_child = {}
            for agent_id, child_id in agents.values_list("pk", f"{child_field}_id"):
                by_child.setdefault(child_id, []).append(agent_id)
            rows = []
            for unit in _units_within(child_level, scope).order_by("name"):
                rows.append(
                    {
                        "level": child_level,
                        "code": unit.code,
                        "name": unit.name,
                        **tally(by_child.get(unit.pk, [])),
                    }
                )
            body["children"] = rows
        return Response(body)

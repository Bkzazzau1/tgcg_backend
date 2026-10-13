from datetime import timedelta

from django.db.models import Count
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from apps.core.permissions import Capability, requires
from apps.geography.scope import SCOPE_FIELDS
from apps.membership.serializers import flatten_scope

from . import livekit, services
from .models import Meeting
from .serializers import (
    CancelSerializer,
    JoinPassSerializer,
    MeetingCreateSerializer,
    MeetingSerializer,
)

C = Capability
ANYONE_IN_MEETINGS = (C.VIEW_MEETING_ROOM, C.JOIN_MEETING, C.START_MEETING)


class MeetingViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    """Meetings the caller called, is invited to, or that are for their area.

    The calls run on LiveKit: `join` hands out a short-lived pass for the
    meeting's room while it is open (from 15 minutes before the start).
    """

    serializer_class = MeetingSerializer
    lookup_field = "reference"

    def get_permissions(self):
        if self.action == "create":
            return [requires(C.START_MEETING)()]
        if self.action == "join":
            return [requires(C.JOIN_MEETING, C.START_MEETING)()]
        return [requires(*ANYONE_IN_MEETINGS)()]

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return Meeting.objects.none()
        meetings = (
            services.visible_meetings(self.request.user)
            .select_related("host", *SCOPE_FIELDS)
            .prefetch_related("invitees")
            .annotate(admitted_count=Count("admissions", distinct=True))
        )
        if self.action == "list":
            since = parse_datetime(self.request.query_params.get("since", ""))
            meetings = meetings.filter(starts_at__gte=since or timezone.now() - timedelta(days=1))
        return meetings

    @extend_schema(
        parameters=[
            OpenApiParameter(
                "since",
                str,
                description="Meetings starting from this time (default: the last day on).",
            )
        ]
    )
    def list(self, request, *args, **kwargs):
        return super().list(request, *args, **kwargs)

    def _respond(self, meeting, code=status.HTTP_200_OK):
        meeting = self.get_queryset().get(pk=meeting.pk)
        return Response(self.get_serializer(meeting).data, status=code)

    @extend_schema(request=MeetingCreateSerializer, responses={201: MeetingSerializer})
    def create(self, request):
        data = MeetingCreateSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        fields = flatten_scope(data.validated_data)
        invitees = fields.pop("invitees", [])
        meeting = services.create_meeting(actor=request.user, invitees=invitees, **fields)
        return self._respond(meeting, status.HTTP_201_CREATED)

    @extend_schema(request=None, responses={200: JoinPassSerializer})
    @action(detail=True, methods=["post"])
    def join(self, request, reference=None):
        try:
            joining = services.admit(actor=request.user, meeting=self.get_object())
        except livekit.NotConfigured:
            return Response(
                {"detail": "Calls are not set up on the server yet."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        return Response(JoinPassSerializer(joining).data)

    @extend_schema(request=None, responses=MeetingSerializer)
    @action(detail=True, methods=["post"])
    def end(self, request, reference=None):
        return self._respond(services.end_meeting(actor=request.user, meeting=self.get_object()))

    @extend_schema(request=CancelSerializer, responses=MeetingSerializer)
    @action(detail=True, methods=["post"])
    def cancel(self, request, reference=None):
        data = CancelSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        meeting = services.cancel_meeting(
            actor=request.user, meeting=self.get_object(), reason=data.validated_data["reason"]
        )
        return self._respond(meeting)

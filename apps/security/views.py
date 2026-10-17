from datetime import timedelta

from django.db.models import Q
from django.shortcuts import get_object_or_404
from django.utils import timezone
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import status
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.models import User
from apps.core.permissions import Capability, requires
from apps.field.views import visible_incidents
from apps.geography.scope import SCOPE_FIELDS, scope_contains

from . import services
from .models import ACTIVE, SecurityDispatch
from .serializers import (
    CandidateSerializer,
    DispatchCancelSerializer,
    DispatchRespondSerializer,
    DispatchSendSerializer,
    DispatchSerializer,
    MyDispatchSerializer,
)

C = Capability
# Finished answers stay on the officer's page this long.
RECENT = timedelta(days=7)


def _incident(request, reference):
    return get_object_or_404(
        visible_incidents(request.user).select_related(*SCOPE_FIELDS), reference=reference
    )


def _dispatches(incident):
    return SecurityDispatch.objects.filter(incident=incident).select_related(
        "incident", "officer__member", "sent_by", "cancelled_by"
    )


class IncidentSecurityView(APIView):
    """The security officers an incident was sent to, newest first; POST sends
    it to one picked from `nearest/` (the Situation Room only)."""

    permission_classes = [IsAuthenticated]

    @extend_schema(responses={200: OpenApiTypes.OBJECT})
    def get(self, request, reference):
        """`{"can_send", "dispatches": [...]}`."""
        incident = _incident(request, reference)
        can_send = services.can_send(request.user, incident)
        if not can_send and not scope_contains(request.user, incident, permission=C.VIEW_INCIDENTS):
            raise PermissionDenied("Your role does not include following incidents.")
        context = {"can_send": can_send}
        return Response(
            {
                "can_send": can_send,
                "dispatches": DispatchSerializer(
                    _dispatches(incident), many=True, context=context
                ).data,
            }
        )

    @extend_schema(request=DispatchSendSerializer, responses={201: DispatchSerializer})
    def post(self, request, reference):
        incident = _incident(request, reference)
        if not services.can_send(request.user, incident):
            raise PermissionDenied("Only the Situation Room sends incidents to security officers.")
        data = DispatchSendSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        officer = get_object_or_404(User, access_id=data.validated_data["officer"].strip().upper())
        dispatch = services.send(
            actor=request.user,
            incident=incident,
            officer=officer,
            note=data.validated_data["note"],
        )
        return Response(
            DispatchSerializer(dispatch, context={"can_send": True}).data,
            status=status.HTTP_201_CREATED,
        )


class NearestOfficersView(APIView):
    """Security officers nearest to the incident, for the Situation Room."""

    permission_classes = [IsAuthenticated]

    @extend_schema(
        parameters=[
            OpenApiParameter("search", OpenApiTypes.STR, description="Name or access ID"),
            OpenApiParameter("limit", OpenApiTypes.INT, description="At most 50 (default 10)"),
        ],
        responses={200: OpenApiTypes.OBJECT},
    )
    def get(self, request, reference):
        """`{"located", "officers": [...]}`; `located` is false when the
        incident's place is unknown, so distances are too."""
        incident = _incident(request, reference)
        try:
            limit = min(max(int(request.query_params.get("limit", services.NEAREST)), 1), 50)
        except ValueError:
            limit = services.NEAREST
        candidates = services.nearest(
            actor=request.user,
            incident=incident,
            search=request.query_params.get("search", ""),
            limit=limit,
        )
        return Response(
            {
                "located": services.incident_point(incident) is not None,
                "officers": CandidateSerializer(candidates, many=True).data,
            }
        )


class CancelDispatchView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(request=DispatchCancelSerializer, responses={200: DispatchSerializer})
    def post(self, request, uuid):
        dispatch = get_object_or_404(SecurityDispatch, uuid=uuid)
        # Only those who may see the incident learn that it exists.
        _incident(request, dispatch.incident.reference)
        data = DispatchCancelSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        dispatch = services.cancel(
            actor=request.user, dispatch=dispatch, reason=data.validated_data["reason"]
        )
        return Response(DispatchSerializer(dispatch, context={"can_send": True}).data)


class MyDispatchesView(APIView):
    """Incidents sent to the signed-in security officer: those still with them,
    and those finished in the last week."""

    permission_classes = [requires(C.RESPOND_TO_SECURITY)]

    @extend_schema(responses=MyDispatchSerializer(many=True))
    def get(self, request):
        since = timezone.now() - RECENT
        dispatches = (
            SecurityDispatch.objects.filter(officer=request.user)
            .filter(Q(status__in=ACTIVE) | Q(finished_at__gte=since))
            .select_related(
                "sent_by", "incident__reporter__member", *(f"incident__{f}" for f in SCOPE_FIELDS)
            )
        )
        return Response(MyDispatchSerializer(dispatches, many=True).data)


class RespondView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(request=DispatchRespondSerializer, responses={200: MyDispatchSerializer})
    def post(self, request, uuid):
        dispatch = get_object_or_404(SecurityDispatch, uuid=uuid, officer=request.user)
        data = DispatchRespondSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        dispatch = services.respond(
            officer=request.user,
            dispatch=dispatch,
            step=data.validated_data["step"],
            note=data.validated_data["note"],
        )
        return Response(MyDispatchSerializer(dispatch).data)

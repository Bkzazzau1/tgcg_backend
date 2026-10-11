from django.db.models import Q
from django.shortcuts import get_object_or_404
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core.choices import GeographyLevel
from apps.core.permissions import Capability, requires
from apps.field.views import EvidenceActionsMixin
from apps.geography.scope import (
    SCOPE_FIELDS,
    ScopeRef,
    actor_area,
    any_of,
    scope_contains,
    within_scope_any_q,
)

from . import collation, services
from .models import Election, Party, ResultSubmission
from .serializers import (
    ElectionSerializer,
    PartySerializer,
    ReasonSerializer,
    ResultEventSerializer,
    ResultSubmissionSerializer,
    ResultSubmitSerializer,
    ReviewSerializer,
)

C = Capability
RESULT_READERS = (C.VIEW_COLLATION, C.VERIFY_ELECTION_RESULT, C.DISPUTE_ELECTION_RESULT)


def visible_results(user):
    """Results in scope for reviewers and collation viewers; otherwise own only."""
    return ResultSubmission.objects.filter(
        any_of(within_scope_any_q(user, RESULT_READERS), Q(submitted_by=user))
    )


class PartyViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = Party.objects.filter(is_active=True)
    serializer_class = PartySerializer
    lookup_field = "acronym"
    pagination_class = None


class ElectionViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = Election.objects.prefetch_related("parties").select_related(*SCOPE_FIELDS)
    serializer_class = ElectionSerializer
    lookup_field = "code"
    pagination_class = None
    filterset_fields = ["status", "type"]


class ResultViewSet(
    EvidenceActionsMixin,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    viewsets.GenericViewSet,
):
    """Polling-unit result submissions. Submissions are immutable; resubmit to correct."""

    serializer_class = ResultSubmissionSerializer
    lookup_field = "reference"
    filterset_fields = {
        "election__code": ["exact"],
        "status": ["exact"],
        "source": ["exact"],
        "polling_unit__code": ["exact"],
        "ward__code": ["exact"],
        "lga__code": ["exact"],
        "senatorial_district__code": ["exact"],
        "state__code": ["exact"],
        "zone__code": ["exact"],
    }
    search_fields = ["reference", "polling_unit__code", "polling_unit__name"]
    ordering_fields = ["submitted_at", "status"]

    def get_permissions(self):
        if self.action == "create":
            return [requires(C.SUBMIT_ELECTION_RESULT)()]
        return [IsAuthenticated()]

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return ResultSubmission.objects.none()
        return (
            visible_results(self.request.user)
            .select_related("election", "submitted_by", "verified_by", *SCOPE_FIELDS)
            .prefetch_related("party_votes__party", "evidence__uploader")
        )

    def _respond(self, submission, code=status.HTTP_200_OK):
        submission = self.get_queryset().get(pk=submission.pk)
        return Response(
            ResultSubmissionSerializer(submission, context=self.get_serializer_context()).data,
            status=code,
        )

    @extend_schema(request=ResultSubmitSerializer, responses={201: ResultSubmissionSerializer})
    def create(self, request):
        serializer = ResultSubmitSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        submission = services.submit_result(actor=request.user, **serializer.validated_data)
        return self._respond(submission, status.HTTP_201_CREATED)

    @extend_schema(request=ReviewSerializer, responses=ResultSubmissionSerializer)
    @action(detail=True, methods=["post"])
    def verify(self, request, reference=None):
        serializer = ReviewSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        submission = services.verify_result(
            actor=request.user, submission=self.get_object(), **serializer.validated_data
        )
        return self._respond(submission)

    @extend_schema(request=ReasonSerializer, responses=ResultSubmissionSerializer)
    @action(detail=True, methods=["post"])
    def dispute(self, request, reference=None):
        serializer = ReasonSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        submission = services.dispute_result(
            actor=request.user, submission=self.get_object(), **serializer.validated_data
        )
        return self._respond(submission)

    @extend_schema(request=ReasonSerializer, responses=ResultSubmissionSerializer)
    @action(detail=True, methods=["post"])
    def reject(self, request, reference=None):
        serializer = ReasonSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        submission = services.reject_result(
            actor=request.user, submission=self.get_object(), **serializer.validated_data
        )
        return self._respond(submission)

    @extend_schema(responses=ResultEventSerializer(many=True))
    @action(detail=True, methods=["get"])
    def events(self, request, reference=None):
        submission = self.get_object()
        events = submission.events.select_related("actor")
        return Response(ResultEventSerializer(events, many=True).data)


class CollationView(APIView):
    """Unofficial collation of verified results for an area, with a child breakdown."""

    permission_classes = [requires(C.VIEW_COLLATION)]

    @extend_schema(
        parameters=[
            OpenApiParameter("election", str, required=True, description="Election code"),
            OpenApiParameter(
                "level",
                str,
                enum=GeographyLevel.values,
                description="Defaults to your own scope",
            ),
            OpenApiParameter("code", str, description="Unit code for the level, e.g. KD"),
            OpenApiParameter(
                "children", bool, description="Include child breakdown (default true)"
            ),
        ],
        responses={200: OpenApiTypes.OBJECT},
    )
    def get(self, request):
        election = get_object_or_404(
            Election.objects.prefetch_related("parties").select_related(*SCOPE_FIELDS),
            code=request.query_params.get("election"),
        )
        level = request.query_params.get("level")
        scope = (
            ScopeRef.resolve(level, request.query_params.get("code"))
            if level
            else actor_area(request.user, C.VIEW_COLLATION)
        )
        if not scope_contains(request.user, scope, permission=C.VIEW_COLLATION):
            raise PermissionDenied("This area is outside your operational scope.")
        if not (scope_contains(election, scope) or scope_contains(scope, election)):
            return Response(
                {"detail": "This area is not covered by the election."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        include_children = request.query_params.get("children", "true").lower() != "false"
        return Response(collation.summarize(election, scope, include_children=include_children))

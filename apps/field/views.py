from django.db.models import Q
from django.http import FileResponse
from django.shortcuts import get_object_or_404
from django_filters import rest_framework as filters
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core.choices import SEVERITY_RANK, IncidentSeverity
from apps.core.permissions import Capability, has_capability, requires
from apps.geography.scope import SCOPE_FIELDS, within_scope_q
from apps.membership.serializers import flatten_scope

from . import services
from .models import OPEN_INCIDENT_STATUSES, Evidence, FieldReport, Incident
from .serializers import (
    EvidenceSerializer,
    EvidenceUploadSerializer,
    FieldReportSerializer,
    IncidentAssignSerializer,
    IncidentEventSerializer,
    IncidentSerializer,
    IncidentStatusSerializer,
    ReportReviewSerializer,
)

C = Capability


def visible_incidents(user):
    """Incidents a user may see: everything in scope, or only their own."""
    if has_capability(user, C.VIEW_INCIDENTS):
        return Incident.objects.filter(within_scope_q(user))
    return Incident.objects.filter(reporter=user)


def visible_reports(user):
    if has_capability(user, C.VERIFY_FIELD_REPORT, C.VIEW_INCIDENTS):
        return FieldReport.objects.filter(within_scope_q(user))
    return FieldReport.objects.filter(reporter=user)


SCOPE_FILTER_FIELDS = {
    "zone__code": ["exact"],
    "state__code": ["exact"],
    "senatorial_district__code": ["exact"],
    "lga__code": ["exact"],
    "ward__code": ["exact"],
    "polling_unit__code": ["exact"],
}


class IncidentFilter(filters.FilterSet):
    min_severity = filters.ChoiceFilter(
        choices=IncidentSeverity.choices,
        method="filter_min_severity",
        label="Minimum severity",
    )
    open = filters.BooleanFilter(method="filter_open", label="Only unresolved incidents")

    class Meta:
        model = Incident
        fields = {"status": ["exact"], "severity": ["exact"], "category": ["exact"]}
        fields.update(SCOPE_FILTER_FIELDS)

    def filter_min_severity(self, queryset, name, value):
        return queryset.filter(severity_rank__gte=SEVERITY_RANK[value])

    def filter_open(self, queryset, name, value):
        condition = Q(status__in=OPEN_INCIDENT_STATUSES)
        return queryset.filter(condition if value else ~condition)


class EvidenceActionsMixin:
    """`GET|POST {record}/evidence/` for incidents and field reports."""

    @extend_schema(
        methods=["POST"],
        request={"multipart/form-data": EvidenceUploadSerializer},
        responses={201: EvidenceSerializer},
    )
    @extend_schema(methods=["GET"], responses=EvidenceSerializer(many=True))
    @action(
        detail=True,
        methods=["get", "post"],
        parser_classes=[MultiPartParser, FormParser],
    )
    def evidence(self, request, reference=None):
        parent = self.get_object()
        if request.method == "GET":
            if not (
                parent.reporter_id == request.user.pk
                or has_capability(request.user, C.VIEW_EVIDENCE)
            ):
                raise PermissionDenied("Your role does not permit viewing evidence.")
            return Response(
                EvidenceSerializer(
                    parent.evidence.select_related("uploader"),
                    many=True,
                    context=self.get_serializer_context(),
                ).data
            )

        serializer = EvidenceUploadSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = dict(serializer.validated_data)
        evidence = services.add_evidence(
            actor=request.user, parent=parent, upload=data.pop("file"), **data
        )
        return Response(
            EvidenceSerializer(evidence, context=self.get_serializer_context()).data,
            status=status.HTTP_201_CREATED,
        )


class IncidentViewSet(
    EvidenceActionsMixin,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    viewsets.GenericViewSet,
):
    """Field incidents. Reporters without `viewIncidents` see only their own."""

    serializer_class = IncidentSerializer
    lookup_field = "reference"
    filterset_class = IncidentFilter
    search_fields = ["reference", "title", "summary"]
    ordering_fields = ["reported_at", "severity_rank", "status"]

    def get_permissions(self):
        if self.action == "create":
            return [requires(C.CREATE_INCIDENT)()]
        return [IsAuthenticated()]

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return Incident.objects.none()
        return (
            visible_incidents(self.request.user)
            .select_related("reporter", *SCOPE_FIELDS)
            .prefetch_related("evidence__uploader")
        )

    def perform_create(self, serializer):
        serializer.instance = services.report_incident(
            actor=self.request.user, **flatten_scope(serializer.validated_data)
        )

    def _respond(self, incident):
        return Response(IncidentSerializer(incident, context=self.get_serializer_context()).data)

    @extend_schema(request=IncidentStatusSerializer, responses=IncidentSerializer)
    @action(detail=True, methods=["post"], url_path="status")
    def change_status(self, request, reference=None):
        serializer = IncidentStatusSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        incident = services.change_incident_status(
            actor=request.user, incident=self.get_object(), **serializer.validated_data
        )
        return self._respond(incident)

    @extend_schema(request=IncidentAssignSerializer, responses=IncidentSerializer)
    @action(detail=True, methods=["post"])
    def assign(self, request, reference=None):
        serializer = IncidentAssignSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        incident = services.assign_incident(
            actor=request.user, incident=self.get_object(), **serializer.validated_data
        )
        return self._respond(incident)

    @extend_schema(responses=IncidentEventSerializer(many=True))
    @action(detail=True, methods=["get"])
    def events(self, request, reference=None):
        incident = self.get_object()
        events = incident.events.select_related("actor")
        return Response(IncidentEventSerializer(events, many=True).data)


class FieldReportViewSet(
    EvidenceActionsMixin,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    viewsets.GenericViewSet,
):
    """Field reports. Reporters without review rights see only their own."""

    serializer_class = FieldReportSerializer
    lookup_field = "reference"
    filterset_fields = {"status": ["exact"], "category": ["exact"], **SCOPE_FILTER_FIELDS}
    search_fields = ["reference", "summary"]
    ordering_fields = ["reported_at", "status"]

    def get_permissions(self):
        if self.action == "create":
            return [requires(C.SUBMIT_FIELD_REPORT)()]
        if self.action == "review":
            return [requires(C.VERIFY_FIELD_REPORT)()]
        return [IsAuthenticated()]

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return FieldReport.objects.none()
        return (
            visible_reports(self.request.user)
            .select_related("reporter", "reviewed_by", "incident", *SCOPE_FIELDS)
            .prefetch_related("evidence__uploader")
        )

    def get_serializer(self, *args, **kwargs):
        serializer = super().get_serializer(*args, **kwargs)
        if getattr(self, "swagger_fake_view", False):
            return serializer
        if hasattr(serializer, "fields") and "incident" in serializer.fields:
            # Only incidents the caller can see may be linked.
            serializer.fields["incident"].queryset = visible_incidents(self.request.user)
        return serializer

    def perform_create(self, serializer):
        serializer.instance = services.submit_report(
            actor=self.request.user, **flatten_scope(serializer.validated_data)
        )

    @extend_schema(request=ReportReviewSerializer, responses=FieldReportSerializer)
    @action(detail=True, methods=["post"])
    def review(self, request, reference=None):
        serializer = ReportReviewSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        report = services.review_report(
            actor=request.user, report=self.get_object(), **serializer.validated_data
        )
        return Response(FieldReportSerializer(report, context=self.get_serializer_context()).data)


class EvidenceDownloadView(APIView):
    """Stream an evidence file after checking the caller may see its record."""

    @extend_schema(responses={(200, "application/octet-stream"): OpenApiTypes.BINARY})
    def get(self, request, uuid):
        evidence = get_object_or_404(
            Evidence.objects.select_related("incident", "report", "result"), uuid=uuid
        )
        user = request.user
        if evidence.incident_id:
            visible = visible_incidents(user).filter(pk=evidence.incident_id).exists()
        elif evidence.report_id:
            visible = visible_reports(user).filter(pk=evidence.report_id).exists()
        else:
            from apps.results.views import visible_results

            visible = visible_results(user).filter(pk=evidence.result_id).exists()
        if not visible:
            return Response(status=status.HTTP_404_NOT_FOUND)
        is_own = evidence.parent.reporter_id == user.pk or evidence.uploader_id == user.pk
        if not (is_own or has_capability(user, C.VIEW_EVIDENCE)):
            raise PermissionDenied("Your role does not permit viewing evidence.")

        response = FileResponse(
            evidence.file.open("rb"),
            as_attachment=True,
            filename=evidence.file_name,
            content_type=evidence.mime_type,
        )
        response["X-Content-SHA256"] = evidence.content_hash
        response["Cache-Control"] = "private, no-store"
        return response

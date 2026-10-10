from django.urls import reverse
from rest_framework import serializers

from apps.core.choices import EvidenceType, IncidentSeverity, IncidentStatus
from apps.geography.scope import ScopeField

from .models import Evidence, FieldReport, Incident, IncidentEvent


class EvidenceSerializer(serializers.ModelSerializer):
    id = serializers.UUIDField(source="uuid", read_only=True)
    uploader = serializers.SlugRelatedField(slug_field="access_id", read_only=True)
    download_url = serializers.SerializerMethodField()

    class Meta:
        model = Evidence
        fields = [
            "id",
            "type",
            "file_name",
            "mime_type",
            "size",
            "content_hash",
            "caption",
            "latitude",
            "longitude",
            "captured_at",
            "uploader",
            "origin",
            "client_id",
            "download_url",
            "created_at",
        ]
        read_only_fields = fields

    def get_download_url(self, evidence) -> str:
        path = reverse("evidence-download", kwargs={"uuid": evidence.uuid})
        request = self.context.get("request")
        return request.build_absolute_uri(path) if request else path


class EvidenceUploadSerializer(serializers.Serializer):
    file = serializers.FileField()
    type = serializers.ChoiceField(
        choices=[c for c in EvidenceType.choices if c[0] != EvidenceType.LOCATION]
    )
    caption = serializers.CharField(required=False, allow_blank=True, max_length=500)
    latitude = serializers.DecimalField(
        max_digits=9, decimal_places=6, required=False, allow_null=True
    )
    longitude = serializers.DecimalField(
        max_digits=9, decimal_places=6, required=False, allow_null=True
    )
    captured_at = serializers.DateTimeField(required=False, allow_null=True)
    content_hash = serializers.RegexField(
        r"^[0-9a-fA-F]{64}$",
        required=False,
        allow_blank=True,
        help_text="SHA-256 recorded on the device; the upload is rejected if it differs.",
    )
    client_id = serializers.CharField(required=False, allow_null=True, max_length=64)


class IncidentSerializer(serializers.ModelSerializer):
    id = serializers.UUIDField(source="uuid", read_only=True)
    scope = ScopeField(required=False, help_text="Defaults to the reporter's own scope.")
    reporter = serializers.SlugRelatedField(slug_field="access_id", read_only=True)
    evidence = EvidenceSerializer(many=True, read_only=True)
    is_open = serializers.BooleanField(read_only=True)

    class Meta:
        model = Incident
        fields = [
            "id",
            "reference",
            "title",
            "category",
            "severity",
            "status",
            "is_open",
            "scope",
            "summary",
            "assigned_team",
            "latitude",
            "longitude",
            "reporter",
            "reported_at",
            "acknowledged_at",
            "resolved_at",
            "closed_at",
            "origin",
            "client_id",
            "evidence",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "reference",
            "status",
            "assigned_team",
            "acknowledged_at",
            "resolved_at",
            "closed_at",
            "created_at",
            "updated_at",
        ]
        extra_kwargs = {"client_id": {"validators": []}}
        validators = []


class IncidentStatusSerializer(serializers.Serializer):
    status = serializers.ChoiceField(
        choices=[c for c in IncidentStatus.choices if c[0] != IncidentStatus.ASSIGNED]
    )
    note = serializers.CharField(required=False, allow_blank=True, max_length=2000)


class IncidentAssignSerializer(serializers.Serializer):
    team = serializers.CharField(max_length=120)
    note = serializers.CharField(required=False, allow_blank=True, max_length=2000)


class IncidentEventSerializer(serializers.ModelSerializer):
    actor = serializers.SlugRelatedField(slug_field="access_id", read_only=True)

    class Meta:
        model = IncidentEvent
        fields = ["action", "from_status", "to_status", "note", "actor", "created_at"]


class FieldReportSerializer(serializers.ModelSerializer):
    id = serializers.UUIDField(source="uuid", read_only=True)
    scope = ScopeField(required=False, help_text="Defaults to the reporter's own scope.")
    reporter = serializers.SlugRelatedField(slug_field="access_id", read_only=True)
    reviewed_by = serializers.SlugRelatedField(slug_field="access_id", read_only=True)
    incident = serializers.SlugRelatedField(
        slug_field="reference",
        queryset=Incident.objects.all(),
        required=False,
        allow_null=True,
    )
    evidence = EvidenceSerializer(many=True, read_only=True)

    class Meta:
        model = FieldReport
        fields = [
            "id",
            "reference",
            "category",
            "summary",
            "status",
            "scope",
            "incident",
            "reporter",
            "reported_at",
            "reviewed_by",
            "reviewed_at",
            "review_reason",
            "origin",
            "client_id",
            "evidence",
            "created_at",
        ]
        read_only_fields = [
            "reference",
            "status",
            "reviewed_by",
            "reviewed_at",
            "review_reason",
            "created_at",
        ]
        extra_kwargs = {"client_id": {"validators": []}}
        validators = []


class ReportReviewSerializer(serializers.Serializer):
    accepted = serializers.BooleanField()
    reason = serializers.CharField(required=False, allow_blank=True, max_length=2000)


SEVERITY_CHOICES = IncidentSeverity.values

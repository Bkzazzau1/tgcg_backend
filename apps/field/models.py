"""Field incidents, field reports and their evidence.

Mirrors FieldIncident / FieldReport / EvidenceAttachment in the Flutter client.
"""

import uuid
from pathlib import Path

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models import Q
from django.utils import timezone

from apps.core.choices import (
    SEVERITY_RANK,
    EvidenceType,
    IncidentCategory,
    IncidentSeverity,
    IncidentStatus,
    RecordOrigin,
    RecordStatus,
    ReportCategory,
)
from apps.core.models import TimeStampedModel
from apps.geography.gps import GpsCheckFields
from apps.geography.scope import ScopedModel

# Generous bounding box around Nigeria; rejects swapped or zeroed coordinates.
NIGERIA_LAT = (3.5, 14.5)
NIGERIA_LNG = (2.0, 15.0)

OPEN_INCIDENT_STATUSES = (
    IncidentStatus.REPORTED,
    IncidentStatus.ACKNOWLEDGED,
    IncidentStatus.ASSIGNED,
    IncidentStatus.INVESTIGATING,
    IncidentStatus.ESCALATED,
)


def _latitude_field():
    return models.DecimalField(
        max_digits=9,
        decimal_places=6,
        null=True,
        blank=True,
        validators=[MinValueValidator(-90), MaxValueValidator(90)],
    )


def _longitude_field():
    return models.DecimalField(
        max_digits=9,
        decimal_places=6,
        null=True,
        blank=True,
        validators=[MinValueValidator(-180), MaxValueValidator(180)],
    )


def validate_location(instance):
    lat, lng = instance.latitude, instance.longitude
    if (lat is None) != (lng is None):
        raise ValidationError("Provide both latitude and longitude, or neither.")
    if lat is None:
        return
    if not (NIGERIA_LAT[0] <= lat <= NIGERIA_LAT[1] and NIGERIA_LNG[0] <= lng <= NIGERIA_LNG[1]):
        raise ValidationError({"latitude": "The location is outside Nigeria."})


class FieldRecord(TimeStampedModel, ScopedModel):
    """Common fields for reports from the field."""

    reference = models.CharField(max_length=16, unique=True, editable=False)
    reporter = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    reported_at = models.DateTimeField(
        default=timezone.now, help_text="When the reporter recorded it (device time)."
    )
    origin = models.CharField(
        max_length=16, choices=RecordOrigin.choices, default=RecordOrigin.LOCAL_ENTRY
    )
    client_id = models.CharField(  # noqa: DJ001 - NULL keeps the unique index sparse
        max_length=64,
        unique=True,
        null=True,
        blank=True,
        help_text="Device-generated id, so offline sync can retry without duplicates.",
    )

    class Meta:
        abstract = True


class Incident(FieldRecord, GpsCheckFields):
    title = models.CharField(max_length=200)
    category = models.CharField(max_length=40, choices=IncidentCategory.choices)
    severity = models.CharField(max_length=16, choices=IncidentSeverity.choices)
    severity_rank = models.PositiveSmallIntegerField(editable=False, db_index=True)
    status = models.CharField(
        max_length=16, choices=IncidentStatus.choices, default=IncidentStatus.REPORTED
    )
    summary = models.TextField(blank=True)
    assigned_team = models.CharField(max_length=120, blank=True)
    latitude = _latitude_field()
    longitude = _longitude_field()
    acknowledged_at = models.DateTimeField(null=True, blank=True)
    resolved_at = models.DateTimeField(null=True, blank=True)
    closed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-severity_rank", "-reported_at"]
        indexes = [models.Index(fields=["status", "severity_rank"])]

    def __str__(self):
        return f"{self.reference} {self.title}"

    @property
    def is_open(self) -> bool:
        return self.status in OPEN_INCIDENT_STATUSES

    def clean(self):
        super().clean()
        self.title = " ".join(self.title.split())
        self.summary = self.summary.strip()
        self.assigned_team = self.assigned_team.strip()
        validate_location(self)
        self.normalize_scope()

    def save(self, *args, **kwargs):
        self.severity_rank = SEVERITY_RANK[self.severity]
        super().save(*args, **kwargs)


class IncidentEvent(models.Model):
    """Append-only audit trail for an incident."""

    class Action(models.TextChoices):
        REPORTED = "reported", "Reported"
        STATUS_CHANGED = "statusChanged", "Status changed"
        ASSIGNED = "assigned", "Assigned"
        EVIDENCE_ADDED = "evidenceAdded", "Evidence added"

    incident = models.ForeignKey(Incident, on_delete=models.CASCADE, related_name="events")
    action = models.CharField(max_length=16, choices=Action.choices)
    from_status = models.CharField(max_length=16, choices=IncidentStatus.choices, blank=True)
    to_status = models.CharField(max_length=16, choices=IncidentStatus.choices, blank=True)
    note = models.TextField(blank=True)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "id"]

    def __str__(self):
        return f"{self.incident.reference}: {self.action}"


class FieldReport(FieldRecord):
    category = models.CharField(max_length=40, choices=ReportCategory.choices)
    summary = models.TextField()
    status = models.CharField(
        max_length=16, choices=RecordStatus.choices, default=RecordStatus.SUBMITTED
    )
    incident = models.ForeignKey(
        Incident, on_delete=models.SET_NULL, null=True, blank=True, related_name="reports"
    )
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    review_reason = models.TextField(blank=True)

    class Meta:
        ordering = ["-reported_at"]

    def __str__(self):
        return f"{self.reference} {self.category}"

    def clean(self):
        super().clean()
        self.summary = self.summary.strip()
        if not self.summary:
            raise ValidationError({"summary": "A summary is required."})
        self.normalize_scope()


def evidence_upload_path(instance, filename):
    # Random stored name: never trust or expose the uploaded file name on disk.
    suffix = Path(filename).suffix.lower()[:10]
    now = timezone.now()
    return f"evidence/{now:%Y/%m}/{uuid.uuid4().hex}{suffix}"


class Evidence(TimeStampedModel):
    """A file attached to an incident, a field report or a result submission."""

    type = models.CharField(max_length=16, choices=EvidenceType.choices)
    file = models.FileField(upload_to=evidence_upload_path)
    file_name = models.CharField(max_length=255, help_text="Original name from the device.")
    mime_type = models.CharField(max_length=100)
    size = models.PositiveBigIntegerField()
    content_hash = models.CharField(max_length=64, help_text="SHA-256 computed by the server.")
    caption = models.CharField(max_length=500, blank=True)
    latitude = _latitude_field()
    longitude = _longitude_field()
    captured_at = models.DateTimeField(null=True, blank=True)
    uploader = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    origin = models.CharField(
        max_length=16, choices=RecordOrigin.choices, default=RecordOrigin.LOCAL_ENTRY
    )
    client_id = models.CharField(  # noqa: DJ001 - NULL keeps the unique index sparse
        max_length=64, unique=True, null=True, blank=True
    )
    incident = models.ForeignKey(
        Incident, on_delete=models.PROTECT, null=True, blank=True, related_name="evidence"
    )
    report = models.ForeignKey(
        FieldReport, on_delete=models.PROTECT, null=True, blank=True, related_name="evidence"
    )
    result = models.ForeignKey(
        "results.ResultSubmission",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="evidence",
    )

    class Meta:
        ordering = ["created_at"]
        constraints = [
            models.CheckConstraint(
                condition=Q(incident__isnull=False, report__isnull=True, result__isnull=True)
                | Q(incident__isnull=True, report__isnull=False, result__isnull=True)
                | Q(incident__isnull=True, report__isnull=True, result__isnull=False),
                name="evidence_belongs_to_one_record",
            ),
        ]

    def __str__(self):
        return f"{self.get_type_display()} {self.file_name}"

    @property
    def parent(self):
        return self.incident or self.report or self.result

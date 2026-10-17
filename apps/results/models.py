"""Elections, polling-unit result submissions and their audit trail.

Mirrors ElectionResultSubmission / ResultValidationSummary in the Flutter
client. TGCG collation is unofficial: it never replaces INEC declarations.
"""

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q

from apps.core.choices import (
    ElectionStatus,
    ElectionType,
    GeographyLevel,
    RecordOrigin,
    RecordStatus,
    SubmissionSource,
)
from apps.core.models import TimeStampedModel
from apps.geography.gps import GpsCheckFields, LocationStatus
from apps.geography.scope import ScopedModel

# Statuses that still count as a live claim for a polling unit.
ACTIVE_RESULT_STATUSES = (
    RecordStatus.SUBMITTED,
    RecordStatus.UNDER_REVIEW,
    RecordStatus.VERIFIED,
    RecordStatus.DISPUTED,
)

ELECTION_TYPE_LEVEL = {
    ElectionType.PRESIDENTIAL: GeographyLevel.COUNTRY,
    ElectionType.GOVERNORSHIP: GeographyLevel.STATE,
    ElectionType.SENATORIAL: GeographyLevel.SENATORIAL_DISTRICT,
}


class Party(models.Model):
    acronym = models.CharField(max_length=12, unique=True)
    name = models.CharField(max_length=150)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["acronym"]
        verbose_name_plural = "parties"

    def __str__(self):
        return self.acronym


class Election(ScopedModel):
    """An election whose scope is the area it covers (e.g. a state for governorship)."""

    code = models.SlugField(max_length=40, unique=True, help_text="e.g. 2027-presidential")
    name = models.CharField(max_length=150)
    type = models.CharField(max_length=32, choices=ElectionType.choices)
    election_date = models.DateField()
    status = models.CharField(
        max_length=16, choices=ElectionStatus.choices, default=ElectionStatus.DRAFT
    )
    parties = models.ManyToManyField(Party, related_name="elections")

    class Meta:
        ordering = ["-election_date", "name"]

    def __str__(self):
        return self.name

    def clean(self):
        super().clean()
        self.normalize_scope()
        required = ELECTION_TYPE_LEVEL.get(self.type)
        if required and self.scope_level != required:
            raise ValidationError(
                {
                    "scope_level": (
                        f"A {self.get_type_display().lower()} election covers a "
                        f"{GeographyLevel(required).label.lower()}."
                    )
                }
            )


class ResultSubmission(TimeStampedModel, ScopedModel, GpsCheckFields):
    """One polling unit's result sheet as submitted from the field.

    The scope is always the polling unit (with its derived ward, LGA, etc).
    Submissions are immutable; corrections are new submissions.
    """

    reference = models.CharField(max_length=16, unique=True, editable=False)
    election = models.ForeignKey(Election, on_delete=models.PROTECT, related_name="submissions")
    submitted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    submitted_at = models.DateTimeField(help_text="When the sheet was captured (device time).")
    source = models.CharField(max_length=8, choices=SubmissionSource.choices)
    source_reference = models.CharField(max_length=120, blank=True)
    origin = models.CharField(
        max_length=16, choices=RecordOrigin.choices, default=RecordOrigin.LOCAL_ENTRY
    )
    client_id = models.CharField(  # noqa: DJ001 - NULL keeps the unique index sparse
        max_length=64, unique=True, null=True, blank=True
    )
    project = models.ForeignKey(
        "projects.Project",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
        help_text="The project the submitter was taking part in when they captured it.",
    )
    status = models.CharField(max_length=16, choices=RecordStatus.choices)

    registered_voters = models.PositiveIntegerField(null=True, blank=True)
    accredited_voters = models.PositiveIntegerField()
    total_valid_votes = models.PositiveIntegerField(
        help_text="Total votes recorded on the sheet (client: totalVotesRecorded)."
    )
    rejected_votes = models.PositiveIntegerField(null=True, blank=True)

    # Where the submitter was (required for app submissions).
    latitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    longitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)

    # Integrity checks (client: ResultValidationSummary)
    arithmetic_valid = models.BooleanField()
    duplicate_suspected = models.BooleanField()
    polling_unit_matched = models.BooleanField(default=True)
    agent_scope_matched = models.BooleanField()
    ocr_confidence = models.FloatField(null=True, blank=True)
    ocr_matched_manual_entry = models.BooleanField(null=True, blank=True)
    validation_notes = models.JSONField(default=list, blank=True)

    verified_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
    )
    verified_at = models.DateTimeField(null=True, blank=True)
    review_reason = models.TextField(blank=True, help_text="Dispute or rejection reason.")

    class Meta:
        ordering = ["-submitted_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["election", "polling_unit"],
                condition=Q(status=RecordStatus.VERIFIED),
                name="one_verified_result_per_polling_unit",
                violation_error_message=(
                    "Another result for this polling unit is already verified."
                ),
            ),
        ]
        indexes = [
            models.Index(fields=["election", "status"]),
            models.Index(fields=["election", "polling_unit"]),
        ]

    def __str__(self):
        return f"{self.reference} {self.polling_unit_id}"

    @property
    def reporter_id(self):
        """Uniform with incidents and reports for evidence permissions."""
        return self.submitted_by_id

    @property
    def requires_human_review(self) -> bool:
        return (
            not self.arithmetic_valid
            or self.duplicate_suspected
            or not self.polling_unit_matched
            or not self.agent_scope_matched
            or self.ocr_matched_manual_entry is False
            or self.location_status in (LocationStatus.AWAY, LocationStatus.SUSPECT)
        )


class PartyVote(models.Model):
    submission = models.ForeignKey(
        ResultSubmission, on_delete=models.CASCADE, related_name="party_votes"
    )
    party = models.ForeignKey(Party, on_delete=models.PROTECT, related_name="+")
    votes = models.PositiveIntegerField()

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["submission", "party"], name="one_row_per_party"),
        ]

    def __str__(self):
        return f"{self.party_id}: {self.votes}"


class ResultEvent(models.Model):
    """Append-only audit trail for a result submission."""

    submission = models.ForeignKey(
        ResultSubmission, on_delete=models.CASCADE, related_name="events"
    )
    from_status = models.CharField(max_length=16, choices=RecordStatus.choices, blank=True)
    to_status = models.CharField(max_length=16, choices=RecordStatus.choices)
    note = models.TextField(blank=True)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "id"]

    def __str__(self):
        return f"{self.submission.reference}: {self.from_status} → {self.to_status}"

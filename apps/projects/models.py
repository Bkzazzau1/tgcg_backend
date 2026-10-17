"""Projects: scheduled periods of field work, such as an election.

A National Administrator creates a project with a start, an end, the areas it
covers and, for an election, the election it runs. Everyone with a role in
those areas sees it while it runs. Coordinators assign people with roles to
it, each at a place (usually a polling unit). Once a person accepts, their app
shares their location for the length of the project, judged against that
place, and the work they report then counts for the project. Outside a
project nobody shares location.
"""

from django.conf import settings
from django.db import models
from django.db.models import F, Q
from django.utils import timezone

from apps.core.models import TimeStampedModel
from apps.geography.scope import ScopedModel


class ProjectStatus(models.TextChoices):
    SCHEDULED = "scheduled", "Scheduled"
    ACTIVE = "active", "On now"
    ENDED = "ended", "Ended"
    CANCELLED = "cancelled", "Cancelled"


class Project(TimeStampedModel):
    name = models.CharField(max_length=120)
    description = models.TextField(
        blank=True, max_length=2000, help_text="What the people taking part are to do."
    )
    starts_at = models.DateTimeField()
    ends_at = models.DateTimeField()
    election = models.ForeignKey(
        "results.Election",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="projects",
        help_text="Its results are accepted only while a project naming it runs.",
    )
    check_place = models.BooleanField(
        default=True,
        help_text="Compare where the people taking part are with their polling unit.",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    cancelled_at = models.DateTimeField(null=True, blank=True)
    cancelled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
    )
    cancel_reason = models.TextField(blank=True)

    class Meta:
        ordering = ["-starts_at", "-id"]
        constraints = [
            models.CheckConstraint(
                condition=Q(ends_at__gt=F("starts_at")), name="project_ends_after_it_starts"
            )
        ]

    def __str__(self):
        return self.name

    def status_at(self, now=None) -> str:
        now = now or timezone.now()
        if self.cancelled_at is not None:
            return ProjectStatus.CANCELLED
        if now < self.starts_at:
            return ProjectStatus.SCHEDULED
        if now < self.ends_at:
            return ProjectStatus.ACTIVE
        return ProjectStatus.ENDED

    def is_active(self, now=None) -> bool:
        return self.status_at(now) == ProjectStatus.ACTIVE

    @property
    def finished_at(self):
        """When it stopped: its end, or when it was cancelled if that came first."""
        if self.cancelled_at is not None and self.cancelled_at < self.ends_at:
            return self.cancelled_at
        return self.ends_at


class ProjectArea(ScopedModel):
    """One of the areas a project covers."""

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="areas")

    def __str__(self):
        unit = self.scope_unit_field
        return "Nigeria" if unit is None else str(getattr(self, unit))

    def clean(self):
        super().clean()
        self.normalize_scope()


class ParticipationStatus(models.TextChoices):
    INVITED = "invited", "Waiting for an answer"
    ACCEPTED = "accepted", "Taking part"
    DECLINED = "declined", "Declined"
    WITHDRAWN = "withdrawn", "Withdrew"
    REMOVED = "removed", "Removed"


LIVE_PARTICIPATION = (ParticipationStatus.INVITED, ParticipationStatus.ACCEPTED)


class Participation(TimeStampedModel, ScopedModel):
    """Someone assigned to a project. Its scope is the place they work."""

    project = models.ForeignKey(Project, on_delete=models.PROTECT, related_name="participants")
    member = models.ForeignKey(
        "membership.Member", on_delete=models.PROTECT, related_name="project_participations"
    )
    status = models.CharField(
        max_length=12, choices=ParticipationStatus.choices, default=ParticipationStatus.INVITED
    )
    assigned_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    responded_at = models.DateTimeField(
        null=True, blank=True, help_text="When the person accepted or declined."
    )
    ended_at = models.DateTimeField(null=True, blank=True)
    ended_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
    )
    reason = models.TextField(blank=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["project", "member"],
                condition=Q(status__in=["invited", "accepted"]),
                name="one_live_participation_per_project",
            )
        ]

    def __str__(self):
        return f"{self.member_id} in {self.project_id} ({self.status})"

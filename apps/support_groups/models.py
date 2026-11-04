"""Support groups (3 November 2026): organizations allied with TGCG, and
their members.

An organization registers on the joining page, its leader joining TGCG at
the same time, or a member registers one from their account. Holders of
manageSupportGroups for all of Nigeria vet it. Its serial number follows the
paper registration form, TGCG/0001/BAT/27. Once approved, its leader adds
members one by one, uploads the group's list, or shares the group's code and
link.

Which group a member is in shows nowhere else: not on the card, the profile,
member lists or the Social Forum. Only the member, the group's leader and
those who vet groups see it. Someone already registered with TGCG joins a
group only when they say yes in the app.

Every group is of one type (3 November 2026), chosen when it registers:
Support Groups, Women Groups, Youth Groups or People With Disability (PWD)
Groups to start with. Those who vet groups add more types, rename them,
stop offering one, and move a group to another type.
"""

from django.conf import settings
from django.db import models

from apps.core.models import TimeStampedModel


class GroupKind(TimeStampedModel):
    """A type of support group."""

    code = models.SlugField(max_length=40, unique=True, editable=False)
    name = models.CharField(max_length=80, unique=True)
    position = models.PositiveSmallIntegerField(default=0, help_text="Its place in lists.")
    offered = models.BooleanField(default=True, help_text="Offered when a group registers.")

    class Meta:
        ordering = ["position", "id"]

    def __str__(self):
        return self.name


class SupportGroup(TimeStampedModel):
    class Status(models.TextChoices):
        PENDING = "pending", "Waiting for vetting"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Not approved"
        SUSPENDED = "suspended", "Suspended"

    class Coverage(models.TextChoices):
        NATIONAL = "national", "National"
        REGIONAL = "regional", "Regional"
        STATE = "state", "State"

    serial_number = models.CharField(max_length=24, unique=True, editable=False)
    name = models.CharField(max_length=150)
    kind = models.ForeignKey(GroupKind, on_delete=models.PROTECT, related_name="groups")
    coverage = models.CharField(max_length=10, choices=Coverage.choices)
    zone = models.ForeignKey(
        "geography.Zone",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
        help_text="The region, when the group covers one.",
    )
    state = models.ForeignKey(
        "geography.State",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
        help_text="The state, when the group covers one.",
    )
    email = models.EmailField(blank=True)
    officials = models.JSONField(
        default=list, blank=True, help_text="Up to three of its leaders: name, position, phone."
    )
    leader = models.ForeignKey(
        "membership.Member", on_delete=models.PROTECT, related_name="led_support_groups"
    )
    leader_confirmed_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Empty while a member someone else named as leader has not said yes.",
    )
    join_code = models.CharField(  # noqa: DJ001 - NULL keeps the unique index sparse
        max_length=7, unique=True, null=True, blank=True, editable=False
    )
    status = models.CharField(
        max_length=10, choices=Status.choices, default=Status.PENDING, db_index=True
    )
    registered_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    decided_at = models.DateTimeField(null=True, blank=True)
    decision_reason = models.TextField(blank=True, max_length=1000)

    class Meta:
        ordering = ["-created_at", "-id"]

    def __str__(self):
        return f"{self.serial_number} {self.name}"


class GroupMember(TimeStampedModel):
    """A member in a support group, or asked to confirm that they are."""

    class Status(models.TextChoices):
        INVITED = "invited", "Asked to confirm"
        ACTIVE = "active", "Member"
        DECLINED = "declined", "Said no"
        LEFT = "left", "Left the group"
        REMOVED = "removed", "Removed by the group"

    class Via(models.TextChoices):
        LEADER = "leader", "Leads the group"
        REGISTERED = "registered", "Registered by the group"
        CONFIRMED = "confirmed", "Already a member, said yes in the app"
        CODE = "code", "Joined with the group's code"
        LINK = "link", "Joined TGCG with the group's link"
        LIST = "list", "From the group's list, placed by TGCG"

    group = models.ForeignKey(SupportGroup, on_delete=models.CASCADE, related_name="memberships")
    member = models.ForeignKey(
        "membership.Member", on_delete=models.CASCADE, related_name="support_group_memberships"
    )
    status = models.CharField(max_length=10, choices=Status.choices)
    via = models.CharField(max_length=10, choices=Via.choices)
    given_name = models.CharField(
        max_length=150,
        blank=True,
        help_text="The name the group gave, shown to it until the member says yes.",
    )
    added_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    answered_at = models.DateTimeField(null=True, blank=True)
    ended_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [models.Index(fields=["group", "status"])]
        constraints = [
            models.UniqueConstraint(fields=["group", "member"], name="one_place_per_support_group")
        ]

    def __str__(self):
        return f"{self.group_id}: {self.member_id} ({self.status})"

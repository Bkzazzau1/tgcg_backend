"""Members and agent accreditation (mirrors TgcgMember / AccreditedAgent)."""

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q

from apps.accounts.phone import normalize_ng_phone
from apps.core.choices import AccreditationStatus, RecordOrigin, RecordStatus, Role
from apps.core.models import TimeStampedModel
from apps.geography.scope import ScopedModel

# Roles a member can be accredited into. National and support roles are
# provisioned as user accounts, not through field accreditation.
ACCREDITABLE_ROLES = (
    Role.ZONAL_COORDINATOR,
    Role.STATE_COORDINATOR,
    Role.LGA_COORDINATOR,
    Role.WARD_COORDINATOR,
    Role.POLLING_UNIT_AGENT,
    Role.OBSERVER,
    Role.LEGAL_OFFICER,
)

ACTIVE_ACCREDITATION = (AccreditationStatus.PENDING, AccreditationStatus.APPROVED)


def _normalize_phone_field(instance, field, required):
    value = getattr(instance, field)
    if not value:
        if required:
            raise ValidationError({field: "A phone number is required."})
        setattr(instance, field, "")
        return
    normalized = normalize_ng_phone(value)
    if normalized is None:
        raise ValidationError({field: "Enter a valid Nigerian mobile number."})
    setattr(instance, field, normalized)


class Member(TimeStampedModel, ScopedModel):
    """A registered TGCG member. The scope is where the member registered."""

    membership_number = models.CharField(max_length=16, unique=True, editable=False)
    full_name = models.CharField(max_length=150)
    phone_number = models.CharField(max_length=16, unique=True)
    email = models.EmailField(blank=True)
    status = models.CharField(
        max_length=16, choices=RecordStatus.choices, default=RecordStatus.SUBMITTED
    )
    origin = models.CharField(
        max_length=16, choices=RecordOrigin.choices, default=RecordOrigin.LOCAL_ENTRY
    )
    client_id = models.CharField(
        max_length=64,
        unique=True,
        null=True,
        blank=True,
        help_text="Device-generated id, so offline sync can retry without duplicates.",
    )
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="member",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
    )

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["status"]), models.Index(fields=["full_name"])]

    def __str__(self):
        return f"{self.full_name} ({self.membership_number})"

    def clean(self):
        super().clean()
        self.full_name = " ".join(self.full_name.split())
        self.email = self.email.strip().lower()
        _normalize_phone_field(self, "phone_number", required=True)
        self.normalize_scope()


class AccreditedAgent(TimeStampedModel, ScopedModel):
    """A member accredited into a field role at an assigned scope."""

    agent_id = models.CharField(max_length=16, unique=True, editable=False)
    member = models.ForeignKey(Member, on_delete=models.PROTECT, related_name="accreditations")
    role = models.CharField(max_length=32, choices=Role.choices)
    status = models.CharField(
        max_length=16,
        choices=AccreditationStatus.choices,
        default=AccreditationStatus.PENDING,
    )
    registered_phone_number = models.CharField(max_length=16, blank=True)
    device_id = models.CharField(max_length=128, blank=True)
    sim_fingerprint = models.CharField(max_length=128, blank=True)
    biometric_enrolled = models.BooleanField(default=False)
    training_completed = models.BooleanField(default=False)
    origin = models.CharField(
        max_length=16, choices=RecordOrigin.choices, default=RecordOrigin.LOCAL_ENTRY
    )
    client_id = models.CharField(max_length=64, unique=True, null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
    )
    status_changed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["member"],
                condition=Q(status__in=ACTIVE_ACCREDITATION),
                name="one_active_accreditation_per_member",
                violation_error_message="This member already has an active accreditation.",
            ),
            models.UniqueConstraint(
                fields=["polling_unit"],
                condition=Q(status=AccreditationStatus.APPROVED, role=Role.POLLING_UNIT_AGENT),
                name="one_approved_agent_per_polling_unit",
                violation_error_message="This polling unit already has an approved agent.",
            ),
        ]
        indexes = [models.Index(fields=["status", "role"])]

    def __str__(self):
        return f"{self.agent_id} {self.get_role_display()}"

    @property
    def is_ready(self) -> bool:
        return self.training_completed and self.biometric_enrolled

    def clean(self):
        super().clean()
        if self.role not in ACCREDITABLE_ROLES:
            raise ValidationError({"role": "This role cannot be granted through accreditation."})
        self.device_id = self.device_id.strip()
        self.sim_fingerprint = self.sim_fingerprint.strip()
        _normalize_phone_field(self, "registered_phone_number", required=False)
        self.normalize_scope()
        self.validate_role_scope(self.role, field="scope")


class AccreditationEvent(models.Model):
    """Append-only audit trail of accreditation status changes."""

    agent = models.ForeignKey(AccreditedAgent, on_delete=models.CASCADE, related_name="events")
    from_status = models.CharField(
        max_length=16,
        choices=AccreditationStatus.choices,
        blank=True,
        help_text="Empty for the initial accreditation.",
    )
    to_status = models.CharField(max_length=16, choices=AccreditationStatus.choices)
    reason = models.TextField(blank=True)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "id"]

    def __str__(self):
        return f"{self.agent.agent_id}: {self.from_status} → {self.to_status}"

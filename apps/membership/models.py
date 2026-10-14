"""Members and agent accreditation (mirrors TgcgMember / AccreditedAgent)."""

import uuid
from pathlib import Path

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import RegexValidator
from django.db import models
from django.db.models import Q
from django.utils import timezone

from apps.accounts.phone import normalize_ng_phone
from apps.core.choices import (
    AccreditationStatus,
    Gender,
    MembershipState,
    RecordOrigin,
    RecordStatus,
    RegistrationChannel,
    Role,
    VerificationStatus,
)
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


# INEC voter identification numbers are 19 letters/digits.
VIN_VALIDATOR = RegexValidator(r"^[A-Z0-9]{19}$", "A VIN has 19 letters and digits.")


def _private_path(folder, filename):
    suffix = Path(filename).suffix.lower()[:10] or ".jpg"
    return f"{folder}/{timezone.now():%Y/%m}/{uuid.uuid4().hex}{suffix}"


def member_photo_path(instance, filename):
    return _private_path("members/photos", filename)


def member_pvc_path(instance, filename):
    return _private_path("members/pvc", filename)


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
    full_name = models.CharField(max_length=150, blank=True)
    phone_number = models.CharField(max_length=16, unique=True)
    email = models.EmailField(blank=True)

    # Identity from the Permanent Voter Card. Empty only for assisted
    # registrations a coordinator made without a PVC.
    vin = models.CharField(  # noqa: DJ001 - NULL keeps the unique index sparse
        "voter identification number",
        max_length=19,
        unique=True,
        null=True,
        blank=True,
        validators=[VIN_VALIDATOR],
    )
    gender = models.CharField(max_length=8, choices=Gender.choices, blank=True)
    date_of_birth = models.DateField(null=True, blank=True)
    occupation = models.CharField(max_length=100, blank=True)
    photo = models.ImageField(upload_to=member_photo_path, blank=True)
    pvc_image = models.ImageField(upload_to=member_pvc_path, blank=True)

    registration_channel = models.CharField(
        max_length=8, choices=RegistrationChannel.choices, default=RegistrationChannel.ASSISTED
    )
    # The member picked the polling unit from the lists instead of giving the
    # code on their PVC, so it may not be where INEC registered them.
    polling_unit_from_list = models.BooleanField(default=False)
    phone_verified_at = models.DateTimeField(null=True, blank=True)
    consent_at = models.DateTimeField(
        null=True, blank=True, help_text="When the member accepted the data-use notice."
    )
    status = models.CharField(
        max_length=16, choices=RecordStatus.choices, default=RecordStatus.SUBMITTED
    )
    # Operational roles need a verified identity and an active membership.
    verification_status = models.CharField(
        max_length=16,
        choices=VerificationStatus.choices,
        default=VerificationStatus.UNVERIFIED,
        db_index=True,
    )
    verified_at = models.DateTimeField(null=True, blank=True)
    verified_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
    )
    membership_state = models.CharField(
        max_length=16,
        choices=MembershipState.choices,
        default=MembershipState.ACTIVE,
        db_index=True,
    )
    state_changed_at = models.DateTimeField(null=True, blank=True)
    state_changed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
    )
    state_reason = models.TextField(blank=True)
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
        self.full_name = " ".join((self.full_name or "").split())
        if not self.full_name and self.registration_channel != RegistrationChannel.USSD:
            raise ValidationError({"full_name": "A full name is required."})
        self.email = self.email.strip().lower()
        self.occupation = " ".join(self.occupation.split())
        _normalize_phone_field(self, "phone_number", required=True)
        if self.vin:
            self.vin = "".join(self.vin.split()).upper()
        else:
            self.vin = None
        self.normalize_scope()

    @property
    def has_pvc(self) -> bool:
        return bool(self.vin)

    @property
    def can_hold_roles(self) -> bool:
        """Verified identity and active membership: eligible for operational roles."""
        return (
            self.verification_status == VerificationStatus.VERIFIED
            and self.membership_state == MembershipState.ACTIVE
        )


class UssdRegistrationDraft(TimeStampedModel):
    """Validated registration progress saved across provider sessions.

    No raw callback text or PIN is retained. A PIN awaiting confirmation is
    stored only as a Django password hash.
    """

    class Kind(models.TextChoices):
        JOIN = "join", "Join TGCG"
        PROFILE = "profile", "Complete app sign-in"

    class Step(models.TextChoices):
        VIN = "vin", "Voter identification number"
        UNIT = "unit", "Polling unit"
        CONFIRM = "confirm", "Confirm membership and consent"
        NAME = "name", "Full name"
        PIN = "pin", "App sign-in PIN"
        PIN_CONFIRM = "pin_confirm", "Confirm app sign-in PIN"

    uuid = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    phone_number = models.CharField(max_length=16, unique=True)
    kind = models.CharField(max_length=8, choices=Kind.choices, default=Kind.JOIN)
    step = models.CharField(max_length=16, choices=Step.choices, default=Step.VIN)
    vin = models.CharField(max_length=19, blank=True, validators=[VIN_VALIDATOR])
    polling_unit = models.ForeignKey(
        "geography.PollingUnit",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    unit_parts = models.JSONField(default=list, blank=True)
    full_name = models.CharField(max_length=150, blank=True)
    pin_hash = models.CharField(max_length=128, blank=True)
    active_session_key = models.UUIDField(null=True, blank=True)

    def clean(self):
        super().clean()
        _normalize_phone_field(self, "phone_number", required=True)
        self.vin = "".join(self.vin.split()).upper()
        self.full_name = " ".join(self.full_name.split())
        if not isinstance(self.unit_parts, list):
            raise ValidationError({"unit_parts": "Polling unit progress must be a list."})


class UssdSession(TimeStampedModel):
    """Callback replay metadata; callback answers are never stored."""

    uuid = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    phone_number = models.CharField(max_length=16)
    service_code = models.CharField(max_length=64)
    provider_session_id = models.CharField(max_length=128)
    draft = models.ForeignKey(
        UssdRegistrationDraft,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="sessions",
    )
    answer_count = models.PositiveIntegerField(default=0)
    prefix_digest = models.CharField(max_length=64, blank=True)
    last_response = models.CharField(max_length=170, blank=True)
    ended = models.BooleanField(default=False)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["phone_number", "service_code", "provider_session_id"],
                name="unique_ussd_phone_service_session",
            )
        ]

    def clean(self):
        super().clean()
        _normalize_phone_field(self, "phone_number", required=True)


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


class VolunteerApplication(TimeStampedModel):
    """A member offering to help with party work, answered by a coordinator.

    Volunteering is not a route to becoming an agent: polling-unit agents are
    accredited by coordinators and sign in with their own agent accounts.
    """

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        ACCEPTED = "accepted", "Accepted"
        DECLINED = "declined", "Declined"
        WITHDRAWN = "withdrawn", "Withdrawn"

    class Interest(models.TextChoices):
        MOBILISATION = "mobilisation", "Mobilisation and door-to-door"
        CAMPAIGN = "campaign", "Campaign events and rallies"
        LOGISTICS = "logistics", "Logistics and transport"
        PUBLICITY = "publicity", "Publicity and social media"
        ELECTION_DAY = "electionDay", "Election-day support"

    member = models.ForeignKey(Member, on_delete=models.CASCADE, related_name="volunteering")
    interests = models.JSONField(
        default=list, blank=True, help_text="What the member offers to help with (Interest values)."
    )
    message = models.TextField(blank=True, max_length=1000)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
    )
    decided_at = models.DateTimeField(null=True, blank=True)
    decision_reason = models.TextField(blank=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["member"],
                condition=Q(status="pending"),
                name="one_pending_application_per_member",
                violation_error_message="You already have a pending offer to volunteer.",
            ),
        ]

    def __str__(self):
        return f"{self.member.membership_number} volunteers ({self.status})"


class VerificationCheck(TimeStampedModel):
    """The latest result of one identity check for a member.

    A result is recorded only for what was actually done: checks the system
    cannot perform are `unsupported` and go to a coordinator's review, never
    `passed`. `detail` never holds the VIN, phone number or images.
    """

    class Kind(models.TextChoices):
        PVC_CAPTURED = "pvcCaptured", "PVC details captured"
        PVC_VALIDATED = "pvcValidated", "PVC confirmed with an authoritative source"
        DUPLICATES = "duplicates", "No duplicate registration"
        PHONE = "phoneVerified", "Phone number confirmed by SMS"
        SELFIE = "selfieCaptured", "Selfie captured"
        LIVENESS = "liveness", "Liveness detection"
        FACE_MATCH = "faceMatch", "Face matches a trusted reference image"

    class Result(models.TextChoices):
        PASSED = "passed", "Passed"
        FAILED = "failed", "Failed"
        PENDING = "pending", "Needs review"
        UNSUPPORTED = "unsupported", "Not available"

    member = models.ForeignKey(Member, on_delete=models.CASCADE, related_name="verification_checks")
    kind = models.CharField(max_length=24, choices=Kind.choices)
    result = models.CharField(max_length=16, choices=Result.choices)
    detail = models.CharField(max_length=300, blank=True)
    checked_at = models.DateTimeField(default=timezone.now)
    checked_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
        help_text="Empty when the system recorded it.",
    )

    class Meta:
        ordering = ["member", "kind"]
        constraints = [
            models.UniqueConstraint(fields=["member", "kind"], name="one_result_per_check")
        ]

    def __str__(self):
        return f"{self.member.membership_number} {self.kind}: {self.result}"


class PollingUnitChangeRequest(TimeStampedModel, ScopedModel):
    """A member asks to move their registered polling unit after an INEC transfer.

    Its scope is the requested polling unit. It takes effect only when a
    coordinator covering that unit has checked the updated voter card.
    Operational appointments are untouched.
    """

    class Status(models.TextChoices):
        PENDING = "pending", "Awaiting review"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"
        WITHDRAWN = "withdrawn", "Withdrawn"

    member = models.ForeignKey(
        Member, on_delete=models.CASCADE, related_name="polling_unit_changes"
    )
    from_polling_unit = models.ForeignKey(
        "geography.PollingUnit",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
    )
    delim = models.CharField(max_length=32, help_text="Code read from the updated voter card.")
    pvc_image = models.ImageField(upload_to=member_pvc_path)
    reason = models.TextField(blank=True, max_length=1000)
    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.PENDING, db_index=True
    )
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
    )
    decided_at = models.DateTimeField(null=True, blank=True)
    decision_reason = models.TextField(blank=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["member"],
                condition=Q(status="pending"),
                name="one_pending_polling_unit_change_per_member",
                violation_error_message="A polling unit change is already awaiting review.",
            ),
        ]

    def __str__(self):
        return f"{self.member.membership_number} to {self.polling_unit_id} ({self.status})"

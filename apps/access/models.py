"""Dynamic roles, role assignments and the security audit log.

See docs/access-control.md. Role names never decide anything: only the
permission codes on a role definition, evaluated per assignment against that
assignment's own areas.
"""

import hashlib
import json
import re

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone

from apps.core.choices import GeographyLevel, Role
from apps.core.models import TimeStampedModel
from apps.geography.scope import ScopedModel

from .registry import PROTECTED, unknown_codes

# The protected role's name; no other role may be called this or a variant of it.
RESERVED_NAME_KEYS = ("national administrator", "national admin", "national adminstrator")


def is_reserved_name(key: str) -> bool:
    return key in RESERVED_NAME_KEYS or key.startswith(RESERVED_NAME_KEYS[0])


def name_key(name: str) -> str:
    """Case-, space- and punctuation-insensitive form used for uniqueness."""
    return " ".join(re.sub(r"[^a-z0-9]+", " ", (name or "").lower()).split())


class RoleCategory(models.TextChoices):
    COMMAND = "command", "Command and administration"
    FIELD = "field", "Field operations"
    COMMUNICATIONS = "communications", "Communications and media"
    LEGAL = "legal", "Legal"
    TECHNICAL = "technical", "Technical support"
    MEMBERSHIP = "membership", "Membership and mobilisation"
    OTHER = "other", "Other"


class RoleStatus(models.TextChoices):
    ACTIVE = "active", "Active"
    ARCHIVED = "archived", "Archived"


class RoleDefinition(TimeStampedModel):
    """A named set of permissions that can be assigned at given geographic levels."""

    code = models.SlugField(max_length=64, unique=True)
    name = models.CharField(max_length=120)
    name_key = models.CharField(max_length=120, unique=True, editable=False)
    description = models.TextField(blank=True)
    category = models.CharField(
        max_length=16, choices=RoleCategory.choices, default=RoleCategory.OTHER
    )
    permitted_levels = models.JSONField(
        default=list, help_text="Geographic levels the role may be assigned at."
    )
    permissions = models.JSONField(default=list, help_text="Permission codes (see registry).")
    supervising_role = models.ForeignKey(
        "self",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="supervised_roles",
    )
    is_system = models.BooleanField(default=False, editable=False)
    is_protected = models.BooleanField(default=False, editable=False)
    legacy_role = models.CharField(max_length=32, choices=Role.choices, blank=True)
    status = models.CharField(
        max_length=16, choices=RoleStatus.choices, default=RoleStatus.ACTIVE, db_index=True
    )
    archived_at = models.DateTimeField(null=True, blank=True)
    archived_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    archive_reason = models.TextField(blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

    @property
    def is_active(self) -> bool:
        return self.status == RoleStatus.ACTIVE

    def clean(self):
        super().clean()
        self.name = " ".join((self.name or "").split())
        if not self.name:
            raise ValidationError({"name": "A role needs a name."})
        self.name_key = name_key(self.name)
        if not self.is_protected and is_reserved_name(self.name_key):
            raise ValidationError({"name": "This name is reserved for the National Administrator."})

        self.permissions = sorted(set(self.permissions or []))
        unknown = unknown_codes(self.permissions)
        if unknown:
            raise ValidationError({"permissions": f"Unknown permissions: {', '.join(unknown)}."})
        if not self.is_protected and set(self.permissions) & PROTECTED:
            raise ValidationError(
                {"permissions": "Only the National Administrator role holds these permissions."}
            )

        levels = list(dict.fromkeys(self.permitted_levels or []))
        if not levels:
            raise ValidationError({"permitted_levels": "Choose at least one geographic level."})
        bad = [level for level in levels if level not in GeographyLevel.values]
        if bad:
            raise ValidationError({"permitted_levels": f"Unknown levels: {', '.join(bad)}."})
        order = list(GeographyLevel.values)
        self.permitted_levels = sorted(levels, key=order.index)

        # A supervising chain must not loop back to this role.
        seen = {self.pk} if self.pk else set()
        parent = self.supervising_role
        while parent is not None:
            if parent.pk in seen or parent.pk == self.pk:
                raise ValidationError({"supervising_role": "Reporting lines cannot loop."})
            seen.add(parent.pk)
            parent = parent.supervising_role


class AssignmentStatus(models.TextChoices):
    ACTIVE = "active", "Active"
    SUSPENDED = "suspended", "Suspended"
    DECLINED = "declined", "Declined"
    RESIGNED = "resigned", "Resigned"
    REVOKED = "revoked", "Revoked"


# Derived from dates on an active assignment; never stored.
SCHEDULED = "scheduled"
EXPIRED = "expired"


class AuthoritySource(models.TextChoices):
    NATIONAL_ADMINISTRATOR = "nationalAdministrator", "National Administrator"
    ASSIGNMENT = "assignment", "Appointing role"
    DELEGATION = "delegation", "Delegated authority"
    TRANSITION = "transition", "Converted from a legacy account"
    RECOVERY = "recovery", "Server recovery procedure"


class RoleAssignment(TimeStampedModel):
    """One member holding one role over one or more areas."""

    member = models.ForeignKey(
        "membership.Member", on_delete=models.PROTECT, related_name="role_assignments"
    )
    role = models.ForeignKey(RoleDefinition, on_delete=models.PROTECT, related_name="assignments")
    restricted_permissions = models.JSONField(
        null=True,
        blank=True,
        help_text="Subset of the role's permissions this appointment is limited to (null = all).",
    )
    starts_at = models.DateTimeField(default=timezone.now)
    ends_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(
        max_length=16,
        choices=AssignmentStatus.choices,
        default=AssignmentStatus.ACTIVE,
        db_index=True,
    )
    accepted_at = models.DateTimeField(
        null=True, blank=True, help_text="When the member accepted; until then they may decline."
    )
    status_changed_at = models.DateTimeField(null=True, blank=True)
    status_changed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    status_reason = models.TextField(blank=True)
    granted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, related_name="+"
    )
    authority = models.CharField(max_length=24, choices=AuthoritySource.choices)
    authority_assignment = models.ForeignKey(
        "self", on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    authority_grant = models.ForeignKey(
        "DelegationGrant", on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["member", "status"])]

    def __str__(self):
        return f"{self.member} as {self.role}"

    def effective_status(self, now=None) -> str:
        now = now or timezone.now()
        if self.status != AssignmentStatus.ACTIVE:
            return self.status
        if self.ends_at and now >= self.ends_at:
            return EXPIRED
        if now < self.starts_at:
            return SCHEDULED
        return AssignmentStatus.ACTIVE

    def permission_set(self) -> frozenset[str]:
        """The role's permissions, narrowed by this appointment's restriction."""
        granted = set(self.role.permissions)
        if not self.role.is_protected:
            granted -= PROTECTED
        if self.restricted_permissions is not None:
            granted &= set(self.restricted_permissions)
        return frozenset(granted)

    def clean(self):
        super().clean()
        if self.ends_at and self.ends_at <= self.starts_at:
            raise ValidationError({"ends_at": "The end must come after the start."})
        if self.restricted_permissions is not None and self.role.is_protected:
            # The engine treats every administrator appointment as a full one.
            message = "A National Administrator appointment is never restricted."
            raise ValidationError({"restricted_permissions": message})
        if self.restricted_permissions is not None:
            restricted = sorted(set(self.restricted_permissions))
            outside = sorted(set(restricted) - set(self.role.permissions))
            if outside:
                raise ValidationError(
                    {
                        "restricted_permissions": (
                            "A restriction can only remove permissions from the role: "
                            f"{', '.join(outside)} is not part of it."
                        )
                    }
                )
            self.restricted_permissions = restricted


class RoleAssignmentArea(ScopedModel):
    """One of the areas an assignment covers."""

    assignment = models.ForeignKey(RoleAssignment, on_delete=models.CASCADE, related_name="areas")

    def __str__(self):
        unit = self.scope_unit_field
        return "Nigeria" if unit is None else str(getattr(self, unit))

    def clean(self):
        super().clean()
        self.normalize_scope()


class GrantStatus(models.TextChoices):
    ACTIVE = "active", "Active"
    REVOKED = "revoked", "Revoked"


# Powers a delegation grant can pass on. Its other permissions are the ceiling
# for the roles the recipient creates or appoints people to.
ADMIN_POWERS = frozenset({"assignRoles", "manageRoles", "manageAssignments", "delegateAuthority"})


class DelegationGrant(TimeStampedModel):
    """Authority to create roles, appoint and manage within limits.

    Validated against the grantor's own authority when created; its source is
    recorded as provenance. Revoking a grant does not revoke the grants made
    from it: each stays valid until it is revoked or expires.
    """

    grantor = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    recipient = models.ForeignKey(
        "membership.Member", on_delete=models.PROTECT, related_name="delegations_received"
    )
    permissions = models.JSONField(default=list)
    categories = models.JSONField(default=list, help_text="Role categories it covers.")
    levels = models.JSONField(default=list, help_text="Geographic levels it covers.")
    can_redelegate = models.BooleanField(default=False)
    depth = models.PositiveSmallIntegerField(
        default=0, help_text="How many further levels of delegation it allows."
    )
    starts_at = models.DateTimeField(default=timezone.now)
    ends_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(
        max_length=16, choices=GrantStatus.choices, default=GrantStatus.ACTIVE, db_index=True
    )
    revoked_at = models.DateTimeField(null=True, blank=True)
    revoked_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    revoke_reason = models.TextField(blank=True)
    source = models.CharField(max_length=24, choices=AuthoritySource.choices)
    source_grant = models.ForeignKey(
        "self", on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    source_assignment = models.ForeignKey(
        RoleAssignment, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    reason = models.TextField(blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Delegation to {self.recipient}"

    def is_effective(self, now=None) -> bool:
        now = now or timezone.now()
        return (
            self.status == GrantStatus.ACTIVE
            and self.starts_at <= now
            and (self.ends_at is None or now < self.ends_at)
        )

    def clean(self):
        super().clean()
        self.permissions = sorted(set(self.permissions or []))
        unknown = unknown_codes(self.permissions)
        if unknown:
            raise ValidationError({"permissions": f"Unknown permissions: {', '.join(unknown)}."})
        if set(self.permissions) & PROTECTED:
            raise ValidationError({"permissions": "Protected permissions cannot be delegated."})
        if not set(self.permissions) & ADMIN_POWERS:
            raise ValidationError(
                {
                    "permissions": "Delegate at least one power: appoint, create roles, manage "
                    "appointments or delegate."
                }
            )
        redelegates = "delegateAuthority" in self.permissions
        if self.can_redelegate != redelegates or (self.can_redelegate and self.depth < 1):
            raise ValidationError(
                {
                    "can_redelegate": "Re-delegation needs the delegate permission and a depth of "
                    "at least 1; without it the depth must be 0."
                }
            )
        if not self.can_redelegate and self.depth:
            raise ValidationError({"depth": "Without re-delegation the depth must be 0."})
        self.categories = sorted(set(self.categories or []))
        if not self.categories or not set(self.categories) <= set(RoleCategory.values):
            raise ValidationError({"categories": "Choose the role categories it covers."})
        order = list(GeographyLevel.values)
        levels = list(dict.fromkeys(self.levels or []))
        if not levels or any(level not in order for level in levels):
            raise ValidationError({"levels": "Choose the geographic levels it covers."})
        self.levels = sorted(levels, key=order.index)
        if self.ends_at and self.ends_at <= self.starts_at:
            raise ValidationError({"ends_at": "The end must come after the start."})


class DelegationGrantArea(ScopedModel):
    grant = models.ForeignKey(DelegationGrant, on_delete=models.CASCADE, related_name="areas")

    def __str__(self):
        unit = self.scope_unit_field
        return "Nigeria" if unit is None else str(getattr(self, unit))


class AdminActionKind(models.TextChoices):
    SUSPEND = "suspend", "Suspend"
    REMOVE = "remove", "Remove"


class AdminActionStatus(models.TextChoices):
    PENDING = "pending", "Awaiting approval"
    EXECUTED = "executed", "Carried out"
    CANCELLED = "cancelled", "Cancelled"


class AdminActionRequest(TimeStampedModel):
    """Suspending or removing a National Administrator: two distinct approvals.

    The request counts as its requester's approval; a second administrator
    must approve. Neither may be the administrator concerned.
    """

    target = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    kind = models.CharField(max_length=16, choices=AdminActionKind.choices)
    reason = models.TextField()
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    status = models.CharField(
        max_length=16,
        choices=AdminActionStatus.choices,
        default=AdminActionStatus.PENDING,
        db_index=True,
    )
    executed_at = models.DateTimeField(null=True, blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)
    cancelled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    cancel_reason = models.TextField(blank=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["target"],
                condition=models.Q(status="pending"),
                name="one_pending_admin_action_per_target",
                violation_error_message="A request about this administrator is already pending.",
            )
        ]

    def __str__(self):
        return f"{self.kind} {self.target} ({self.status})"


class AdminActionApproval(models.Model):
    request = models.ForeignKey(
        AdminActionRequest, on_delete=models.CASCADE, related_name="approvals"
    )
    approver = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["request", "approver"], name="one_approval_per_administrator"
            )
        ]

    def __str__(self):
        return f"{self.approver} approved {self.request_id}"


class AuditQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise ValidationError("Audit events cannot be changed.")

    def delete(self):
        raise ValidationError("Audit events cannot be deleted.")


class AuditEvent(models.Model):
    """Append-only, hash-chained record of a security-relevant change.

    Each event stores the hash of the one before it, so editing or deleting a
    row breaks the chain (see `verify_chain`). Rows cannot be updated or
    deleted through the ORM.
    """

    created_at = models.DateTimeField(default=timezone.now, db_index=True)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    actor_label = models.CharField(max_length=64, blank=True)
    action = models.CharField(max_length=64, db_index=True)
    target_type = models.CharField(max_length=32, db_index=True)
    target_id = models.CharField(max_length=64, db_index=True)
    target_label = models.CharField(max_length=200, blank=True)
    scope = models.JSONField(default=list, blank=True)
    changes = models.JSONField(default=dict, blank=True)
    reason = models.TextField(blank=True)
    provenance = models.JSONField(default=dict, blank=True)
    previous_hash = models.CharField(max_length=64, blank=True)
    hash = models.CharField(max_length=64, unique=True, editable=False)

    objects = AuditQuerySet.as_manager()

    class Meta:
        ordering = ["-id"]
        indexes = [models.Index(fields=["target_type", "target_id"])]

    def __str__(self):
        return f"{self.created_at:%Y-%m-%d %H:%M} {self.action} {self.target_type}:{self.target_id}"

    def save(self, *args, **kwargs):
        if self.pk is not None:
            raise ValidationError("Audit events cannot be changed.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Audit events cannot be deleted.")

    def compute_hash(self) -> str:
        payload = {
            "created_at": self.created_at.isoformat(),
            "actor": self.actor_id,
            "action": self.action,
            "target": [self.target_type, self.target_id, self.target_label],
            "scope": self.scope,
            "changes": self.changes,
            "reason": self.reason,
            "provenance": self.provenance,
            "previous": self.previous_hash,
        }
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(canonical.encode()).hexdigest()

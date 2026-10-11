"""Role and appointment rules. Views stay thin; every write goes through here.

Authority to create roles or appoint members comes from one *source* at a
time (see docs/access-control.md §5): a National Administrator, or one grant
holding the needed permission (assignRoles / manageRoles / manageAssignments)
over its own areas and up to its own permissions. Nobody acts on themselves.
"""

from dataclasses import dataclass

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.text import slugify

from apps.core.choices import GeographyLevel, MembershipState, Role, VerificationStatus
from apps.core.permissions import Capability
from apps.geography.scope import SCOPE_FIELDS, scope_payload

from . import audit, engine
from .models import (
    AssignmentStatus,
    AuthoritySource,
    RoleAssignment,
    RoleAssignmentArea,
    RoleDefinition,
    RoleStatus,
)
from .registry import PROTECTED

C = Capability
S = AssignmentStatus

# Administrative powers: passing them on is delegation and needs delegateAuthority.
ADMIN_PERMISSIONS = frozenset(
    {C.ASSIGN_ROLES, C.MANAGE_ROLES, C.MANAGE_ASSIGNMENTS, C.DELEGATE_AUTHORITY}
)


# --- authority -------------------------------------------------------------------


@dataclass(frozen=True)
class Authority:
    """One source of authority, never combined with another."""

    kind: str
    permissions: frozenset | None  # None: unlimited (National Administrator)
    areas: tuple | None  # None: nationwide
    key: str = ""

    def covers(self, area) -> bool:
        return self.areas is None or any(engine.area_contains(a, area) for a in self.areas)

    def allows_permissions(self, permissions) -> bool:
        permissions = set(permissions)
        if self.permissions is None:
            return True
        if permissions & PROTECTED or not permissions <= self.permissions:
            return False
        return not (permissions & ADMIN_PERMISSIONS) or C.DELEGATE_AUTHORITY in self.permissions


NATIONAL = Authority(kind=AuthoritySource.NATIONAL_ADMINISTRATOR, permissions=None, areas=None)


def authorities(actor, needed: str) -> list[Authority]:
    """Every source from which `actor` may exercise `needed`."""
    if engine.is_national_administrator(actor):
        return [NATIONAL]
    return [
        Authority(
            kind=AuthoritySource.ASSIGNMENT,
            permissions=grant.permissions,
            areas=grant.areas,
            key=grant.key,
        )
        for grant in engine.active_grants(actor)
        if needed in grant.permissions and grant.source == engine.ASSIGNMENT
    ]


def _authority_for(actor, needed, *, permissions, areas=()):
    """The first single source covering all `permissions` in every area."""
    for authority in authorities(actor, needed):
        if authority.allows_permissions(permissions) and all(authority.covers(a) for a in areas):
            return authority
    return None


def active_national_administrators() -> set[int]:
    """Ids of every account that can act as National Administrator right now."""
    from apps.accounts.models import User

    now = timezone.now()
    legacy = set(
        User.objects.filter(role=Role.NATIONAL_ADMINISTRATOR, is_active=True)
        .exclude(
            member__membership_state__in=[MembershipState.SUSPENDED, MembershipState.DEACTIVATED]
        )
        .values_list("pk", flat=True)
    )
    appointed = set(
        RoleAssignment.objects.filter(
            role__is_protected=True,
            role__status=RoleStatus.ACTIVE,
            status=S.ACTIVE,
            starts_at__lte=now,
            member__verification_status=VerificationStatus.VERIFIED,
            member__membership_state=MembershipState.ACTIVE,
            member__user__is_active=True,
        )
        .filter(Q(ends_at__isnull=True) | Q(ends_at__gt=now))
        .values_list("member__user_id", flat=True)
    )
    return legacy | {pk for pk in appointed if pk}


def _record(actor, action, assignment=None, role=None, reason="", changes=None, provenance=None):
    if assignment is not None:
        audit.record(
            actor=actor,
            action=action,
            target_type="roleAssignment",
            target_id=assignment.uuid,
            target_label=f"{assignment.member.full_name} as {assignment.role.name}",
            scope=[scope_payload(area) for area in assignment.areas.all()],
            changes=changes,
            reason=reason,
            provenance=provenance,
        )
    else:
        audit.record(
            actor=actor,
            action=action,
            target_type="role",
            target_id=role.code,
            target_label=role.name,
            changes=changes,
            reason=reason,
            provenance=provenance,
        )


# --- roles -----------------------------------------------------------------------

ROLE_FIELDS = (
    "name",
    "description",
    "category",
    "permitted_levels",
    "permissions",
    "supervising_role",
)


def _unique_code(name: str) -> str:
    base = slugify(name)[:56] or "role"
    code, n = base, 2
    while RoleDefinition.objects.filter(code=code).exists():
        code, n = f"{base}-{n}", n + 1
    return code


def _snapshot(role):
    return {
        "name": role.name,
        "description": role.description,
        "category": role.category,
        "permitted_levels": list(role.permitted_levels),
        "permissions": list(role.permissions),
        "supervising_role": role.supervising_role.code if role.supervising_role else None,
        "status": role.status,
    }


@transaction.atomic
def create_role(*, actor, **fields) -> RoleDefinition:
    role = RoleDefinition(created_by=actor, **{k: v for k, v in fields.items() if k in ROLE_FIELDS})
    role.code = _unique_code(role.name or "")
    role.full_clean(exclude=["code"])
    authority = _authority_for(actor, C.MANAGE_ROLES, permissions=role.permissions)
    if authority is None:
        raise PermissionDenied("This role carries permissions beyond your authority.")
    role.save()
    _record(
        actor,
        "role.created",
        role=role,
        changes={"after": _snapshot(role)},
        provenance={"authority": authority.kind, "via": authority.key},
    )
    return role


def _require_role_editor(actor, role, new_permissions=None):
    if engine.is_national_administrator(actor):
        return
    if role.is_system:
        raise PermissionDenied("Only National Administrators can change system roles.")
    if role.created_by_id != actor.pk:
        raise PermissionDenied("Only the role's creator or a National Administrator can change it.")
    permissions = new_permissions if new_permissions is not None else role.permissions
    if _authority_for(actor, C.MANAGE_ROLES, permissions=permissions) is None:
        raise PermissionDenied("This role carries permissions beyond your authority.")


@transaction.atomic
def update_role(*, actor, role: RoleDefinition, **fields) -> RoleDefinition:
    role = RoleDefinition.objects.select_for_update().get(pk=role.pk)
    fields = {k: v for k, v in fields.items() if k in ROLE_FIELDS}
    if role.is_system:
        locked = set(fields) - {"description", "supervising_role"}
        if role.is_protected:
            locked |= set(fields) & {"supervising_role"}
        if locked:
            raise ValidationError(
                {sorted(locked)[0]: "System roles keep their name, levels and permissions."}
            )
    _require_role_editor(actor, role, fields.get("permissions"))
    before = _snapshot(role)
    for name, value in fields.items():
        setattr(role, name, value)
    role.full_clean(exclude=["code"])
    role.save()
    after = _snapshot(role)
    changed = {k: {"from": before[k], "to": after[k]} for k in after if before[k] != after[k]}
    if changed:
        _record(actor, "role.updated", role=role, changes=changed)
    return role


@transaction.atomic
def archive_role(*, actor, role: RoleDefinition, reason: str) -> RoleDefinition:
    role = RoleDefinition.objects.select_for_update().get(pk=role.pk)
    if role.is_system:
        raise ValidationError("System roles cannot be archived.")
    _require_role_editor(actor, role)
    if not role.is_active:
        raise ValidationError("This role is already archived.")
    if not reason.strip():
        raise ValidationError({"reason": "Say why the role is archived."})
    role.status = RoleStatus.ARCHIVED
    role.archived_at = timezone.now()
    role.archived_by = actor
    role.archive_reason = reason.strip()
    role.save()
    _record(actor, "role.archived", role=role, reason=role.archive_reason)
    return role


@transaction.atomic
def restore_role(*, actor, role: RoleDefinition, reason: str = "") -> RoleDefinition:
    role = RoleDefinition.objects.select_for_update().get(pk=role.pk)
    _require_role_editor(actor, role)
    if role.is_active:
        raise ValidationError("This role is already active.")
    role.status = RoleStatus.ACTIVE
    role.archived_at = None
    role.archived_by = None
    role.archive_reason = ""
    role.save()
    _record(actor, "role.restored", role=role, reason=reason.strip())
    return role


# --- appointments ----------------------------------------------------------------


def _build_areas(role, areas):
    built = []
    seen = set()
    for attrs in areas:
        area = RoleAssignmentArea(**{k: attrs.get(k) for k in ("scope_level", *SCOPE_FIELDS)})
        area.normalize_scope()
        if area.scope_level not in role.permitted_levels:
            label = GeographyLevel(area.scope_level).label.lower()
            raise ValidationError({"areas": f"{role.name} cannot be assigned at {label} level."})
        key = (
            area.scope_level,
            getattr(area, f"{area.scope_unit_field}_id", None) if area.scope_unit_field else None,
        )
        if key in seen:
            raise ValidationError({"areas": "The same area is listed twice."})
        seen.add(key)
        built.append(area)
    if not built:
        raise ValidationError({"areas": "Choose at least one area."})
    return built


@transaction.atomic
def appoint(
    *,
    actor,
    member,
    role: RoleDefinition,
    areas: list[dict],
    restricted_permissions=None,
    starts_at=None,
    ends_at=None,
    reason: str = "",
) -> RoleAssignment:
    from apps.membership.models import Member

    member = Member.objects.select_for_update().get(pk=member.pk)
    if member.user_id and member.user_id == actor.pk:
        raise PermissionDenied("You cannot appoint yourself.")
    if not member.can_hold_roles:
        raise ValidationError(
            {
                "member": "Only members with a verified identity and an active "
                "membership can hold roles."
            }
        )
    role = RoleDefinition.objects.select_for_update().get(pk=role.pk)
    if not role.is_active:
        raise ValidationError({"role": "Archived roles cannot be assigned."})
    if role.is_protected and not engine.allows(actor, C.APPOINT_NATIONAL_ADMINISTRATOR):
        raise PermissionDenied("Only a National Administrator can appoint another.")

    built = _build_areas(role, areas)
    assignment = RoleAssignment(
        member=member,
        role=role,
        restricted_permissions=restricted_permissions,
        starts_at=starts_at or timezone.now(),
        ends_at=ends_at,
        granted_by=actor,
        status=S.ACTIVE,
        status_changed_at=timezone.now(),
        status_changed_by=actor,
    )
    assignment.full_clean(exclude=["authority", "authority_assignment"])

    authority = _authority_for(
        actor, C.ASSIGN_ROLES, permissions=assignment.permission_set(), areas=built
    )
    if authority is None:
        raise PermissionDenied("This appointment is beyond your authority.")

    duplicate = RoleAssignment.objects.filter(
        member=member, role=role, status__in=[S.ACTIVE, S.SUSPENDED]
    ).prefetch_related("areas")
    for existing in duplicate:
        if existing.effective_status() == "expired":
            continue
        for area in built:
            if any(
                engine.area_contains(a, area) and a.scope_level == area.scope_level
                for a in existing.areas.all()
            ):
                raise ValidationError(
                    {"areas": f"{member.full_name} already holds this role there."}
                )

    assignment.authority = authority.kind
    if authority.kind == AuthoritySource.ASSIGNMENT:
        assignment.authority_assignment = RoleAssignment.objects.get(uuid=authority.key)
    assignment.save()
    for area in built:
        area.assignment = assignment
        area.save()
    _record(
        actor,
        "assignment.created",
        assignment=assignment,
        reason=reason,
        changes={
            "member": member.membership_number,
            "role": role.code,
            "restricted_permissions": assignment.restricted_permissions,
            "starts_at": assignment.starts_at.isoformat(),
            "ends_at": assignment.ends_at.isoformat() if assignment.ends_at else None,
        },
        provenance={"authority": authority.kind, "via": authority.key},
    )
    engine.clear_cache()
    return assignment


def _lock(assignment) -> RoleAssignment:
    return (
        RoleAssignment.objects.select_for_update()
        .select_related("role", "member")
        .get(pk=assignment.pk)
    )


def _require_manager(actor, assignment: RoleAssignment) -> Authority:
    if assignment.member.user_id == actor.pk:
        raise PermissionDenied("You cannot change your own appointment.")
    if assignment.role.is_protected:
        raise PermissionDenied(
            "National Administrator appointments change only through the "
            "two-administrator approval."
        )
    areas = list(assignment.areas.all())
    authority = _authority_for(
        actor, C.MANAGE_ASSIGNMENTS, permissions=assignment.role.permissions, areas=areas
    )
    if authority is None:
        raise PermissionDenied("This appointment is outside your authority.")
    return authority


def _set_status(assignment, status, actor, reason):
    assignment.status = status
    assignment.status_changed_at = timezone.now()
    assignment.status_changed_by = actor
    assignment.status_reason = reason
    assignment.save()
    engine.clear_cache()


@transaction.atomic
def suspend(*, actor, assignment, reason: str) -> RoleAssignment:
    assignment = _lock(assignment)
    authority = _require_manager(actor, assignment)
    if assignment.status != S.ACTIVE:
        raise ValidationError(
            {"status": f"Only an active appointment can be suspended (it is {assignment.status})."}
        )
    if not reason.strip():
        raise ValidationError({"reason": "Say why the appointment is suspended."})
    _set_status(assignment, S.SUSPENDED, actor, reason.strip())
    _record(
        actor,
        "assignment.suspended",
        assignment=assignment,
        reason=reason.strip(),
        provenance={"authority": authority.kind, "via": authority.key},
    )
    return assignment


@transaction.atomic
def reactivate(*, actor, assignment, reason: str = "") -> RoleAssignment:
    assignment = _lock(assignment)
    authority = _require_manager(actor, assignment)
    if assignment.status != S.SUSPENDED:
        raise ValidationError({"status": "Only a suspended appointment can be reactivated."})
    _set_status(assignment, S.ACTIVE, actor, reason.strip())
    _record(
        actor,
        "assignment.reactivated",
        assignment=assignment,
        reason=reason.strip(),
        provenance={"authority": authority.kind, "via": authority.key},
    )
    return assignment


@transaction.atomic
def revoke(*, actor, assignment, reason: str) -> RoleAssignment:
    assignment = _lock(assignment)
    authority = _require_manager(actor, assignment)
    if assignment.status not in (S.ACTIVE, S.SUSPENDED):
        raise ValidationError({"status": f"This appointment is already {assignment.status}."})
    if not reason.strip():
        raise ValidationError({"reason": "Say why the appointment is revoked."})
    _set_status(assignment, S.REVOKED, actor, reason.strip())
    _record(
        actor,
        "assignment.revoked",
        assignment=assignment,
        reason=reason.strip(),
        provenance={"authority": authority.kind, "via": authority.key},
    )
    return assignment


@transaction.atomic
def restrict(*, actor, assignment, permissions) -> RoleAssignment:
    """Limit an appointment to some of its role's permissions (None lifts the limit)."""
    assignment = _lock(assignment)
    authority = _require_manager(actor, assignment)
    before = assignment.restricted_permissions
    assignment.restricted_permissions = None if permissions is None else list(permissions)
    assignment.full_clean(exclude=["authority", "authority_assignment"])
    assignment.save()
    engine.clear_cache()
    _record(
        actor,
        "assignment.restricted",
        assignment=assignment,
        changes={"from": before, "to": assignment.restricted_permissions},
        provenance={"authority": authority.kind, "via": authority.key},
    )
    return assignment


# --- the appointee's own choices -------------------------------------------------


def _require_own(user, assignment):
    if assignment.member.user_id != user.pk:
        raise PermissionDenied("This is not your appointment.")


def _guard_last_administrator(assignment, user):
    if assignment.role.is_protected and active_national_administrators() <= {user.pk}:
        raise ValidationError("The last National Administrator cannot step down.")


@transaction.atomic
def accept(*, user, assignment) -> RoleAssignment:
    assignment = _lock(assignment)
    _require_own(user, assignment)
    if assignment.status != S.ACTIVE or assignment.accepted_at:
        raise ValidationError({"status": "There is nothing to accept."})
    assignment.accepted_at = timezone.now()
    assignment.save(update_fields=["accepted_at", "updated_at"])
    _record(user, "assignment.accepted", assignment=assignment)
    return assignment


@transaction.atomic
def decline(*, user, assignment, reason: str = "") -> RoleAssignment:
    assignment = _lock(assignment)
    _require_own(user, assignment)
    if assignment.status != S.ACTIVE or assignment.accepted_at:
        raise ValidationError(
            {"status": "Only an appointment you have not accepted can be declined. Resign instead."}
        )
    _guard_last_administrator(assignment, user)
    _set_status(assignment, S.DECLINED, user, reason.strip())
    _record(user, "assignment.declined", assignment=assignment, reason=reason.strip())
    return assignment


@transaction.atomic
def resign(*, user, assignment, reason: str = "") -> RoleAssignment:
    assignment = _lock(assignment)
    _require_own(user, assignment)
    if assignment.status not in (S.ACTIVE, S.SUSPENDED):
        raise ValidationError({"status": f"This appointment is already {assignment.status}."})
    _guard_last_administrator(assignment, user)
    _set_status(assignment, S.RESIGNED, user, reason.strip())
    _record(user, "assignment.resigned", assignment=assignment, reason=reason.strip())
    return assignment

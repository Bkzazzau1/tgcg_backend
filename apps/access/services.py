"""Role, appointment, delegation and administrator rules. Views stay thin;
every write goes through here.

Authority to create roles, appoint, manage or delegate comes from one
*source* at a time (see docs/access-control.md §5): a National
Administrator, an appointment holding the needed power over its own areas and
up to its own permissions, or a delegation grant within its permissions, role
categories, levels, areas and depth. Sources are never combined. Nobody acts
on themselves, and authority never acts on someone it came from.
"""

from dataclasses import dataclass

from django.core.exceptions import ObjectDoesNotExist, PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.text import slugify

from apps.accounts.phone import normalize_ng_phone
from apps.core.choices import GeographyLevel, MembershipState, Role, VerificationStatus
from apps.core.models import Sequence
from apps.core.permissions import Capability
from apps.geography.scope import SCOPE_FIELDS, any_of, scope_payload

from . import audit, engine
from .models import (
    ADMIN_POWERS,
    EXPIRED,
    AdminActionApproval,
    AdminActionKind,
    AdminActionRequest,
    AdminActionStatus,
    AssignmentStatus,
    AuthoritySource,
    DelegationGrant,
    DelegationGrantArea,
    GrantStatus,
    RoleAssignment,
    RoleAssignmentArea,
    RoleDefinition,
    RoleStatus,
)
from .registry import PROTECTED

C = Capability
S = AssignmentStatus
A = AuthoritySource

# Administrative powers: passing them on is delegation and needs delegateAuthority.
ADMIN_PERMISSIONS = ADMIN_POWERS
SUPERVISOR = "supervisor"
UPSTREAM = "upstream"
# Two administrators must approve action on a third, so the server recovery
# procedure is available only while fewer than this many are active.
RECOVERY_THRESHOLD = 3


# --- authority -------------------------------------------------------------------


@dataclass(frozen=True)
class Authority:
    """One source of authority, never combined with another."""

    kind: str
    permissions: frozenset | None  # None: unlimited (National Administrator)
    areas: tuple | None  # None: nationwide
    key: str = ""
    categories: frozenset | None = None  # None: any role category
    levels: frozenset | None = None  # None: any geographic level
    depth: int | None = None  # further delegation a grant allows; None: not counted
    can_delegate: bool = True
    ends_at: object = None  # when the source ends; None: open-ended
    role_code: str = ""

    def covers(self, area) -> bool:
        return self.areas is None or any(engine.area_contains(a, area) for a in self.areas)

    def allows_permissions(self, permissions, *, in_role: bool = False) -> bool:
        permissions = set(permissions)
        if self.permissions is None:
            return True
        if permissions & PROTECTED or not permissions <= self.permissions:
            return False
        if permissions & ADMIN_PERMISSIONS and not self.can_delegate:
            return False  # passing on administrative powers is delegation
        # Below a National Administrator, further delegation passes on only
        # through a grant, where its depth is counted; never through a role.
        return not (in_role and C.DELEGATE_AUTHORITY in permissions)

    def allows(self, *, permissions, categories=(), levels=(), areas=(), in_role=False) -> bool:
        return (
            self.allows_permissions(permissions, in_role=in_role)
            and (self.categories is None or set(categories) <= self.categories)
            and (self.levels is None or set(levels) <= self.levels)
            and all(self.covers(area) for area in areas)
        )

    def outlasted_by(self, ends_at) -> bool:
        """True when something ending at `ends_at` (None: never) would outlast this source."""
        return self.ends_at is not None and (ends_at is None or ends_at > self.ends_at)


NATIONAL = Authority(kind=A.NATIONAL_ADMINISTRATOR, permissions=None, areas=None)


def authorities(actor, needed: str) -> list[Authority]:
    """Every source from which `actor` may exercise `needed`."""
    if engine.is_national_administrator(actor):
        return [NATIONAL]
    assigned, delegated = {}, set()
    for grant in engine.active_grants(actor):
        if needed not in grant.permissions:
            continue
        if grant.source == engine.ASSIGNMENT:
            assigned[grant.key] = grant
        elif grant.source == engine.DELEGATION:
            delegated.add(grant.key)

    found = []
    if assigned:
        ends = {
            str(uuid): ends_at
            for uuid, ends_at in RoleAssignment.objects.filter(uuid__in=assigned).values_list(
                "uuid", "ends_at"
            )
        }
        for key, grant in assigned.items():
            found.append(
                Authority(
                    kind=A.ASSIGNMENT,
                    permissions=grant.permissions,
                    areas=grant.areas,
                    key=key,
                    can_delegate=C.DELEGATE_AUTHORITY in grant.permissions,
                    ends_at=ends.get(key),
                    role_code=grant.role_code,
                )
            )
    if delegated:
        grants = DelegationGrant.objects.filter(uuid__in=delegated).prefetch_related("areas")
        for delegation in grants:
            found.append(
                Authority(
                    kind=A.DELEGATION,
                    permissions=frozenset(delegation.permissions),
                    areas=tuple(delegation.areas.all()),
                    key=str(delegation.uuid),
                    categories=frozenset(delegation.categories),
                    levels=frozenset(delegation.levels),
                    depth=delegation.depth,
                    can_delegate=delegation.can_redelegate,
                    ends_at=delegation.ends_at,
                )
            )
    return found


def _authority_for(
    actor,
    needed,
    *,
    permissions,
    categories=(),
    levels=(),
    areas=(),
    in_role=False,
    ends_at=None,
    bounded=False,
):
    """The first single source covering all of the request at once.

    `bounded`: the request passes on authority, so it may not outlast its source.
    """
    for authority in authorities(actor, needed):
        if authority.allows(
            permissions=permissions,
            categories=categories,
            levels=levels,
            areas=areas,
            in_role=in_role,
        ) and not (bounded and authority.outlasted_by(ends_at)):
            return authority
    return None


def _provenance(authority, **extra) -> dict:
    provenance = {"authority": authority.kind, "via": authority.key}
    if authority.role_code:
        provenance["role"] = authority.role_code
    return {**provenance, **extra}


def _source_record(authority):
    """The grant or appointment an authority comes from (None for an
    administrator or a legacy role)."""
    if authority.kind == A.DELEGATION:
        return DelegationGrant.objects.get(uuid=authority.key)
    if authority.kind in (A.ASSIGNMENT, SUPERVISOR) and authority.key != engine.LEGACY:
        return RoleAssignment.objects.filter(uuid=authority.key).first()
    return None


def _upstream_accounts(record) -> set[int]:
    """Accounts that passed this authority down, through grants and appointments."""
    accounts: set[int] = set()
    seen = set()
    while record is not None and (type(record), record.pk) not in seen:
        seen.add((type(record), record.pk))
        if isinstance(record, DelegationGrant):
            accounts.add(record.grantor_id)
            record = record.source_grant or record.source_assignment
        else:
            if record.granted_by_id:
                accounts.add(record.granted_by_id)
            record = record.authority_grant or record.authority_assignment
    return accounts


def _require_not_upstream(authority, member):
    if member.user_id and member.user_id in _upstream_accounts(_source_record(authority)):
        raise PermissionDenied("Authority cannot be used on someone it came from.")


# --- who is who ------------------------------------------------------------------


def _phone(value) -> str:
    return normalize_ng_phone(value) or ""


def _phone_of(user) -> str:
    phone = _phone(getattr(user, "phone_number", ""))
    if phone:
        return phone
    try:
        return _phone(user.member.phone_number)
    except ObjectDoesNotExist:
        return ""


def _same_person(user, other) -> bool:
    """The same account, or two accounts with the same phone number."""
    if user.pk == other.pk:
        return True
    phone = _phone_of(user)
    return bool(phone) and phone == _phone_of(other)


def _is_self(actor, member) -> bool:
    """`member` is the actor: their own member record, or one with their phone."""
    if member.user_id and member.user_id == actor.pk:
        return True
    phone = _phone_of(actor)
    return bool(phone) and phone == _phone(member.phone_number)


def in_good_standing(user) -> bool:
    """An active account whose membership, if any, is active."""
    if not user.is_active:
        return False
    try:
        return user.member.membership_state == MembershipState.ACTIVE
    except ObjectDoesNotExist:
        return True


def identity_verified(user) -> bool:
    """The account belongs to a member whose identity a coordinator verified.

    Membership never needs it; agents, observers and appointed roles do.
    """
    try:
        return user.member.verification_status == VerificationStatus.VERIFIED
    except ObjectDoesNotExist:
        return False


def authority_over_member(actor, permission, member, *, area=None, what="member"):
    """The grant under which `actor` may use `permission` on `member`.

    One grant must hold the permission and cover `area` (the member's own
    registration by default), and must not have come from the member: nobody
    acts on themselves, and authority never acts on someone it came from.
    """
    if _is_self(actor, member):
        raise PermissionDenied("You cannot do this to yourself.")
    area = member if area is None else area
    covering = [
        grant
        for grant in engine.active_grants(actor)
        if permission in grant.permissions and grant.covers(area)
    ]
    if not covering:
        raise PermissionDenied(f"The {what} is outside your area for this.")
    for grant in covering:
        source = None
        if grant.source == engine.ASSIGNMENT:
            source = RoleAssignment.objects.filter(uuid=grant.key).first()
        elif grant.source == engine.DELEGATION:
            source = DelegationGrant.objects.filter(uuid=grant.key).first()
        if not (member.user_id and member.user_id in _upstream_accounts(source)):
            return grant
    raise PermissionDenied("Authority cannot be used on someone it came from.")


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


def _lock_administrators():
    """Serialise every change to who is a National Administrator."""
    Sequence.objects.select_for_update().get_or_create(name="national-administrators")
    engine.clear_cache()  # whoever waited for the lock re-reads who is who


def end_sessions(user):
    """Blacklist the account's refresh tokens so no session outlives the change.

    Access tokens already in use carry no permissions of their own: every
    request is checked against the database.
    """
    from rest_framework_simplejwt.token_blacklist.models import BlacklistedToken, OutstandingToken

    for token in OutstandingToken.objects.filter(user=user):
        BlacklistedToken.objects.get_or_create(token=token)


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


def _current_holdings(role) -> list[RoleAssignment]:
    """Appointments to `role` that hold, or may again hold, its permissions."""
    holdings = (
        RoleAssignment.objects.filter(role=role, status__in=[S.ACTIVE, S.SUSPENDED])
        .select_related("member")
        .prefetch_related("areas")
    )
    return [holding for holding in holdings if holding.effective_status() != EXPIRED]


def _role_change_authority(actor, role, holdings=(), added=frozenset()):
    """The single source under which `actor` may make `role` what it now is.

    Its creator, or a supervisor whose supervising appointment itself holds
    manageRoles, within a source covering the role's permissions, category and
    levels, and the areas of every holding the change reaches. `added`:
    permissions the holdings would gain, which may not outlast the source.
    """
    if engine.is_national_administrator(actor):
        return NATIONAL
    if role.is_system:
        raise PermissionDenied("Only National Administrators can change system roles.")
    creator = role.created_by_id == actor.pk
    codes = supervising_codes(role)
    areas = [area for holding in holdings for area in holding.areas.all()]
    levels = set(role.permitted_levels) | {area.scope_level for area in areas}
    bounded = bool(set(added) & ADMIN_PERMISSIONS) and bool(holdings)
    if any(holding.ends_at is None for holding in holdings):
        latest = None
    else:
        latest = max((holding.ends_at for holding in holdings), default=None)
    for authority in authorities(actor, C.MANAGE_ROLES):
        if not (creator or (authority.kind == A.ASSIGNMENT and authority.role_code in codes)):
            continue
        if authority.allows(
            permissions=role.permissions,
            categories=[role.category],
            levels=levels,
            areas=areas,
            in_role=True,
        ) and not (bounded and authority.outlasted_by(latest)):
            return authority
    return None


def _require_role_change(actor, role, holdings=(), added=frozenset()) -> Authority:
    authority = _role_change_authority(actor, role, holdings, added)
    if authority is None:
        raise PermissionDenied(
            "Only the role's creator, a supervisor holding manageRoles or a National "
            "Administrator can change it, within their authority over everyone it reaches."
        )
    if added and authority.kind != A.NATIONAL_ADMINISTRATOR:
        if any(_is_self(actor, holding.member) for holding in holdings):
            raise PermissionDenied("You hold this role; someone else must widen it.")
        upstream = _upstream_accounts(_source_record(authority))
        if any(holding.member.user_id in upstream for holding in holdings):
            raise PermissionDenied(
                "Someone your authority came from holds this role; it cannot be widened by you."
            )
    return authority


@transaction.atomic
def create_role(*, actor, **fields) -> RoleDefinition:
    role = RoleDefinition(created_by=actor, **{k: v for k, v in fields.items() if k in ROLE_FIELDS})
    role.code = _unique_code(role.name or "")
    role.full_clean(exclude=["code"])
    authority = _role_change_authority(actor, role)
    if authority is None:
        raise PermissionDenied("This role is beyond your authority.")
    role.save()
    _record(
        actor,
        "role.created",
        role=role,
        changes={"after": _snapshot(role)},
        provenance=_provenance(authority),
    )
    return role


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
    # Who may change it is judged on the role as it is; what it may become, after.
    _require_role_change(actor, role)
    before = _snapshot(role)
    line = role.supervising_role_id
    for name, value in fields.items():
        setattr(role, name, value)
    role.full_clean(exclude=["code"])
    added = frozenset(set(role.permissions) - set(before["permissions"]))
    # New permissions reach every holder, and a new reporting line gives its
    # holders supervision over them: both need authority over all of them.
    reaches = bool(added) or role.supervising_role_id != line
    holdings = _current_holdings(role) if reaches else ()
    authority = _require_role_change(actor, role, holdings, added)
    role.save()
    after = _snapshot(role)
    changed = {k: {"from": before[k], "to": after[k]} for k in after if before[k] != after[k]}
    if changed:
        _record(
            actor, "role.updated", role=role, changes=changed, provenance=_provenance(authority)
        )
    return role


@transaction.atomic
def archive_role(*, actor, role: RoleDefinition, reason: str) -> RoleDefinition:
    """Stop the role granting anything, for all its holders at once."""
    role = RoleDefinition.objects.select_for_update().get(pk=role.pk)
    if role.is_system:
        raise ValidationError("System roles cannot be archived.")
    authority = _require_role_change(actor, role, _current_holdings(role))
    if not role.is_active:
        raise ValidationError("This role is already archived.")
    if not reason.strip():
        raise ValidationError({"reason": "Say why the role is archived."})
    role.status = RoleStatus.ARCHIVED
    role.archived_at = timezone.now()
    role.archived_by = actor
    role.archive_reason = reason.strip()
    role.save()
    _record(
        actor,
        "role.archived",
        role=role,
        reason=role.archive_reason,
        provenance=_provenance(authority),
    )
    return role


@transaction.atomic
def restore_role(*, actor, role: RoleDefinition, reason: str = "") -> RoleDefinition:
    """Bring an archived role back: like widening it to all its permissions."""
    role = RoleDefinition.objects.select_for_update().get(pk=role.pk)
    if role.is_active:
        raise ValidationError("This role is already active.")
    if role.archived_by_id != actor.pk and not engine.is_national_administrator(actor):
        raise PermissionDenied(
            "Only whoever archived it or a National Administrator can restore it."
        )
    authority = _require_role_change(
        actor, role, _current_holdings(role), added=frozenset(role.permissions)
    )
    role.status = RoleStatus.ACTIVE
    role.archived_at = None
    role.archived_by = None
    role.archive_reason = ""
    role.save()
    _record(
        actor,
        "role.restored",
        role=role,
        reason=reason.strip(),
        provenance=_provenance(authority),
    )
    return role


# --- supervision -----------------------------------------------------------------


def supervising_codes(role) -> set[str]:
    """Codes of every role above `role` in its reporting line."""
    codes: set[str] = set()
    parent = role.supervising_role
    while parent is not None and parent.code not in codes:
        codes.add(parent.code)
        parent = parent.supervising_role
    return codes


def supervised_role_ids(codes) -> set[int]:
    """Ids of every role reporting, directly or not, to one of `codes`."""
    found: set[int] = set()
    frontier = set(
        RoleDefinition.objects.filter(supervising_role__code__in=codes).values_list("pk", flat=True)
    )
    while frontier:
        found |= frontier
        frontier = (
            set(
                RoleDefinition.objects.filter(supervising_role__in=frontier).values_list(
                    "pk", flat=True
                )
            )
            - found
        )
    return found


def supervises_any(user) -> bool:
    codes = {grant.role_code for grant in engine.active_grants(user) if grant.role_code}
    return bool(codes) and RoleDefinition.objects.filter(supervising_role__code__in=codes).exists()


def supervised_q(user) -> Q:
    """Appointments `user` supervises: roles below one they hold, inside that holding's areas."""
    conditions = []
    for grant in engine.active_grants(user):
        if not grant.role_code:
            continue
        roles = supervised_role_ids({grant.role_code})
        if roles:
            inside = RoleAssignmentArea.objects.filter(engine.areas_q(grant.areas))
            conditions.append(Q(role__in=roles, areas__in=inside))
    return any_of(*conditions) if conditions else Q(pk__in=[])


def _supervisor_authority(actor, assignment, areas) -> Authority | None:
    """A holding of a role above the appointment's that covers all of its areas."""
    codes = supervising_codes(assignment.role)
    for grant in engine.active_grants(actor):
        if grant.role_code in codes and all(grant.covers(area) for area in areas):
            return Authority(
                kind=SUPERVISOR,
                permissions=frozenset(),
                areas=grant.areas,
                key=grant.key,
                can_delegate=False,
                role_code=grant.role_code,
            )
    return None


# --- appointments ----------------------------------------------------------------


def _build_areas(model, areas, role=None):
    built = []
    seen = set()
    for attrs in areas:
        area = model(**{k: attrs.get(k) for k in ("scope_level", *SCOPE_FIELDS)})
        area.normalize_scope()
        if role is not None and area.scope_level not in role.permitted_levels:
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
    if _is_self(actor, member):
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
    if role.is_protected:
        _lock_administrators()
        if not engine.allows(actor, C.APPOINT_NATIONAL_ADMINISTRATOR):
            raise PermissionDenied("Only a National Administrator can appoint another.")

    built = _build_areas(RoleAssignmentArea, areas, role)
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
    assignment.full_clean(exclude=["authority", "authority_assignment", "authority_grant"])

    permissions = assignment.permission_set()
    authority = _authority_for(
        actor,
        C.ASSIGN_ROLES,
        permissions=permissions,
        categories=[role.category],
        levels=[area.scope_level for area in built],
        areas=built,
        in_role=True,
        ends_at=assignment.ends_at,
        bounded=bool(permissions & ADMIN_PERMISSIONS),
    )
    if authority is None:
        raise PermissionDenied("This appointment is beyond your authority.")
    _require_not_upstream(authority, member)
    source = _source_record(authority)

    duplicate = RoleAssignment.objects.filter(
        member=member, role=role, status__in=[S.ACTIVE, S.SUSPENDED]
    ).prefetch_related("areas")
    for existing in duplicate:
        if existing.effective_status() == EXPIRED:
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
    if isinstance(source, RoleAssignment):
        assignment.authority_assignment = source
    elif isinstance(source, DelegationGrant):
        assignment.authority_grant = source
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
        provenance=_provenance(authority),
    )
    engine.clear_cache()
    return assignment


def _lock(assignment) -> RoleAssignment:
    return (
        RoleAssignment.objects.select_for_update()
        .select_related("role", "member")
        .get(pk=assignment.pk)
    )


def _require_manager(
    actor, assignment: RoleAssignment, *, supervisor_may=False, whole_role=False
) -> Authority:
    """A source allowing manageAssignments over the whole appointment, or (when
    `supervisor_may`) a holder of a role above it whose areas cover it.

    The source must cover what the appointment carries, or (`whole_role`, for
    changing its restriction) everything its role could carry, and cannot act
    on anyone it came from.
    """
    if _is_self(actor, assignment.member):
        raise PermissionDenied("You cannot change your own appointment.")
    if assignment.role.is_protected:
        raise PermissionDenied(
            "National Administrator appointments change only through the "
            "two-administrator approval."
        )
    areas = list(assignment.areas.all())
    authority = _authority_for(
        actor,
        C.MANAGE_ASSIGNMENTS,
        permissions=assignment.role.permissions if whole_role else assignment.permission_set(),
        categories=[assignment.role.category],
        levels=[area.scope_level for area in areas],
        areas=areas,
        in_role=True,
    )
    if authority is None and supervisor_may:
        authority = _supervisor_authority(actor, assignment, areas)
    if authority is None:
        raise PermissionDenied("This appointment is outside your authority.")
    _require_not_upstream(authority, assignment.member)
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
    authority = _require_manager(actor, assignment, supervisor_may=True)
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
        provenance=_provenance(authority),
    )
    return assignment


@transaction.atomic
def reactivate(*, actor, assignment, reason: str = "") -> RoleAssignment:
    assignment = _lock(assignment)
    authority = _require_manager(actor, assignment, supervisor_may=True)
    if assignment.status != S.SUSPENDED:
        raise ValidationError({"status": "Only a suspended appointment can be reactivated."})
    if authority.kind == SUPERVISOR and assignment.status_changed_by_id != actor.pk:
        raise PermissionDenied("A supervisor can lift only a suspension they made.")
    _set_status(assignment, S.ACTIVE, actor, reason.strip())
    _record(
        actor,
        "assignment.reactivated",
        assignment=assignment,
        reason=reason.strip(),
        provenance=_provenance(authority),
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
        provenance=_provenance(authority),
    )
    return assignment


@transaction.atomic
def restrict(*, actor, assignment, permissions) -> RoleAssignment:
    """Limit an appointment to some of its role's permissions (None lifts the limit).

    Narrowing needs manageAssignments; giving permissions back is granting
    them, so it also needs a source that could make the appointment as it
    would become.
    """
    assignment = _lock(assignment)
    authority = _require_manager(actor, assignment, whole_role=True)
    before = assignment.restricted_permissions
    carried = assignment.permission_set()
    assignment.restricted_permissions = None if permissions is None else list(permissions)
    assignment.full_clean(exclude=["authority", "authority_assignment", "authority_grant"])
    added = assignment.permission_set() - carried
    if added:
        areas = list(assignment.areas.all())
        authority = _authority_for(
            actor,
            C.ASSIGN_ROLES,
            permissions=assignment.permission_set(),
            categories=[assignment.role.category],
            levels=[area.scope_level for area in areas],
            areas=areas,
            in_role=True,
            ends_at=assignment.ends_at,
            bounded=bool(added & ADMIN_PERMISSIONS),
        )
        if authority is None:
            raise PermissionDenied("Giving these permissions back is beyond your authority.")
        _require_not_upstream(authority, assignment.member)
    assignment.save()
    engine.clear_cache()
    _record(
        actor,
        "assignment.restricted",
        assignment=assignment,
        changes={"from": before, "to": assignment.restricted_permissions},
        provenance=_provenance(authority),
    )
    return assignment


# --- the appointee's own choices -------------------------------------------------


def _require_own(user, assignment):
    if assignment.member.user_id != user.pk:
        raise PermissionDenied("This is not your appointment.")


def _guard_last_administrator(assignment, user):
    if not assignment.role.is_protected:
        return
    _lock_administrators()
    if active_national_administrators() <= {user.pk}:
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


# --- delegation ------------------------------------------------------------------


def _record_grant(actor, action, grant, reason="", changes=None, provenance=None):
    audit.record(
        actor=actor,
        action=action,
        target_type="delegationGrant",
        target_id=grant.uuid,
        target_label=f"Delegation to {grant.recipient.full_name}",
        scope=[scope_payload(area) for area in grant.areas.all()],
        changes=changes,
        reason=reason,
        provenance=provenance,
    )


def _covers_grant(authority, grant, areas) -> bool:
    """`authority` could itself make `grant`: everything it passes on, one level up."""
    return authority.allows(
        permissions=grant.permissions,
        categories=grant.categories,
        levels=grant.levels,
        areas=areas,
    ) and (authority.depth is None or grant.depth < authority.depth)


@transaction.atomic
def delegate(
    *,
    actor,
    recipient,
    areas: list[dict],
    permissions,
    categories,
    levels,
    can_redelegate: bool = False,
    depth: int = 0,
    starts_at=None,
    ends_at=None,
    reason: str = "",
) -> DelegationGrant:
    """Pass on administrative powers within the actor's own authority."""
    from apps.membership.models import Member

    recipient = Member.objects.select_for_update().get(pk=recipient.pk)
    if _is_self(actor, recipient):
        raise PermissionDenied("You cannot delegate authority to yourself.")
    if not recipient.can_hold_roles:
        raise ValidationError(
            {
                "recipient": "Only members with a verified identity and an active "
                "membership can receive authority."
            }
        )
    if recipient.user_id is None:
        raise ValidationError({"recipient": "This member has no account to sign in with."})

    built = _build_areas(DelegationGrantArea, areas)
    grant = DelegationGrant(
        grantor=actor,
        recipient=recipient,
        permissions=list(permissions),
        categories=list(categories),
        levels=list(levels),
        can_redelegate=can_redelegate,
        depth=depth,
        starts_at=starts_at or timezone.now(),
        ends_at=ends_at,
        reason=reason.strip(),
    )
    grant.full_clean(exclude=["source", "source_grant", "source_assignment"])

    authority = next(
        (
            candidate
            for candidate in authorities(actor, C.DELEGATE_AUTHORITY)
            if _covers_grant(candidate, grant, built) and not candidate.outlasted_by(grant.ends_at)
        ),
        None,
    )
    if authority is None:
        raise PermissionDenied("This delegation is beyond your authority.")
    _require_not_upstream(authority, recipient)
    source = _source_record(authority)

    grant.source = authority.kind
    if isinstance(source, DelegationGrant):
        grant.source_grant = source
    elif isinstance(source, RoleAssignment):
        grant.source_assignment = source
    grant.save()
    for area in built:
        area.grant = grant
        area.save()
    _record_grant(
        actor,
        "delegation.created",
        grant,
        reason=grant.reason,
        changes={
            "recipient": recipient.membership_number,
            "permissions": grant.permissions,
            "categories": grant.categories,
            "levels": grant.levels,
            "can_redelegate": grant.can_redelegate,
            "depth": grant.depth,
            "starts_at": grant.starts_at.isoformat(),
            "ends_at": grant.ends_at.isoformat() if grant.ends_at else None,
        },
        provenance=_provenance(authority),
    )
    engine.clear_cache()
    return grant


def _revoking_authority(actor, grant) -> Authority | None:
    """A National Administrator, or anyone above the grant in its chain (its
    grantor included) whose own standing is still good."""
    if engine.is_national_administrator(actor):
        return NATIONAL
    if in_good_standing(actor) and actor.pk in _upstream_accounts(grant):
        return Authority(kind=UPSTREAM, permissions=frozenset(), areas=())
    return None


@transaction.atomic
def revoke_delegation(*, actor, grant, reason: str) -> DelegationGrant:
    """Revoke one grant. Grants made from it stay valid until revoked or expired."""
    grant = DelegationGrant.objects.select_for_update().select_related("recipient").get(pk=grant.pk)
    authority = _revoking_authority(actor, grant)
    if authority is None:
        raise PermissionDenied(
            "Only its grantor, someone above it or a National Administrator can revoke it."
        )
    if grant.status != GrantStatus.ACTIVE:
        raise ValidationError({"status": "This delegation is already revoked."})
    if not reason.strip():
        raise ValidationError({"reason": "Say why the delegation is revoked."})
    grant.status = GrantStatus.REVOKED
    grant.revoked_at = timezone.now()
    grant.revoked_by = actor
    grant.revoke_reason = reason.strip()
    grant.save()
    _record_grant(
        actor,
        "delegation.revoked",
        grant,
        reason=grant.revoke_reason,
        provenance=_provenance(authority),
    )
    engine.clear_cache()
    return grant


def delegations_visible_to(user):
    """Grants the user made or received, or that lie inside their delegating reach."""
    grants = DelegationGrant.objects.select_related(
        "grantor", "recipient", "revoked_by", "source_grant", "source_assignment"
    ).prefetch_related("areas")
    if engine.is_national_administrator(user):
        return grants
    inside = DelegationGrantArea.objects.filter(engine.scope_q(user, C.DELEGATE_AUTHORITY))
    return grants.filter(Q(grantor=user) | Q(recipient__user=user) | Q(areas__in=inside)).distinct()


# --- National Administrators -----------------------------------------------------

STATE_ACTIVE = "active"
STATE_SUSPENDED = "suspended"
STATE_INACTIVE = "inactive"


def _require_administrator(actor):
    """Take the administrators' lock, then confirm the actor still is one."""
    _lock_administrators()
    if not engine.is_national_administrator(actor):
        raise PermissionDenied("Only a National Administrator can do this.")


def _administrator_appointments(user, statuses):
    return RoleAssignment.objects.select_related("role", "member").filter(
        member__user=user, role__is_protected=True, status__in=statuses
    )


def is_administrator_account(user) -> bool:
    """A legacy administrator account, or one holding a current administrator appointment."""
    return (
        user.role == Role.NATIONAL_ADMINISTRATOR
        or _administrator_appointments(user, [S.ACTIVE, S.SUSPENDED]).exists()
    )


def administrator_state(user, active=None) -> str:
    """active, suspended, or inactive (e.g. membership suspended or not verified)."""
    active = active_national_administrators() if active is None else active
    if user.pk in active:
        return STATE_ACTIVE
    legacy_suspended = user.role == Role.NATIONAL_ADMINISTRATOR and not user.is_active
    if legacy_suspended or _administrator_appointments(user, [S.SUSPENDED]).exists():
        return STATE_SUSPENDED
    return STATE_INACTIVE


def administrator_accounts():
    from apps.accounts.models import User

    appointed = RoleAssignment.objects.filter(
        role__is_protected=True, status__in=[S.ACTIVE, S.SUSPENDED]
    ).values("member__user")
    return User.objects.filter(Q(role=Role.NATIONAL_ADMINISTRATOR) | Q(pk__in=appointed))


def _distinct_people(users) -> int:
    """How many people, counting accounts that share a phone number once."""
    people: list = []
    for user in users:
        if not any(_same_person(user, other) for other in people):
            people.append(user)
    return len(people)


def _require_applicable(kind, target, active=None):
    """Suspend needs an active administrator; remove, any administrator account."""
    active = active_national_administrators() if active is None else active
    if kind == AdminActionKind.SUSPEND and target.pk not in active:
        raise ValidationError({"target": "Only an active National Administrator can be suspended."})
    if kind == AdminActionKind.REMOVE and not is_administrator_account(target):
        raise ValidationError({"target": "This account is not a National Administrator."})


def _record_admin(actor, action, request, changes=None):
    audit.record(
        actor=actor,
        action=action,
        target_type="administrator",
        target_id=request.target.access_id,
        target_label=request.target.full_name,
        changes={"request": str(request.uuid), "kind": request.kind, **(changes or {})},
        reason=request.reason,
        provenance={"authority": A.NATIONAL_ADMINISTRATOR},
    )


@transaction.atomic
def request_admin_action(*, actor, target, kind: str, reason: str) -> AdminActionRequest:
    """Ask to suspend or remove a National Administrator. The request is its
    requester's approval; a second administrator must approve before it runs."""
    _require_administrator(actor)
    if kind not in AdminActionKind.values:
        raise ValidationError({"kind": "Choose suspend or remove."})
    if _same_person(actor, target):
        raise PermissionDenied("You cannot request action against yourself.")
    if not reason.strip():
        raise ValidationError({"reason": "Say why."})
    active = active_national_administrators()
    _require_applicable(kind, target, active)
    if len(active - {target.pk}) < 2:
        raise ValidationError(
            "Two National Administrators other than the one concerned must approve, and there "
            "are not enough. Appoint another administrator first (see the recovery procedure "
            "if none can act)."
        )
    if AdminActionRequest.objects.filter(target=target, status=AdminActionStatus.PENDING).exists():
        raise ValidationError({"target": "A request about this administrator is already pending."})
    request = AdminActionRequest.objects.create(
        target=target, kind=kind, reason=reason.strip(), requested_by=actor
    )
    AdminActionApproval.objects.create(request=request, approver=actor)
    _record_admin(actor, "administrator.action_requested", request)
    return request


@transaction.atomic
def approve_admin_action(*, actor, request) -> AdminActionRequest:
    """Approve a pending request; it runs once two current administrators,
    two different people and neither the one concerned, have approved it."""
    _require_administrator(actor)
    request = (
        AdminActionRequest.objects.select_for_update().select_related("target").get(pk=request.pk)
    )
    if request.status != AdminActionStatus.PENDING:
        raise ValidationError({"status": f"This request is already {request.status}."})
    if _same_person(actor, request.target):
        raise PermissionDenied("The administrator concerned cannot approve.")
    approvals = list(request.approvals.select_related("approver"))
    if any(_same_person(actor, approval.approver) for approval in approvals):
        raise ValidationError("You have already approved this request.")
    try:
        _require_applicable(request.kind, request.target)
    except ValidationError as error:
        raise ValidationError(
            "This administrator's status has changed since the request; cancel it instead."
        ) from error
    AdminActionApproval.objects.create(request=request, approver=actor)
    _record_admin(actor, "administrator.action_approved", request)

    active = active_national_administrators()
    approvers = [
        approval.approver
        for approval in request.approvals.select_related("approver")
        if approval.approver_id in active and not _same_person(approval.approver, request.target)
    ]
    if _distinct_people(approvers) >= 2:
        _execute_admin_action(request, actor, approvers)
    return request


def _execute_admin_action(request, actor, approvers):
    from apps.accounts.models import User

    target = User.objects.select_for_update().get(pk=request.target_id)
    if not active_national_administrators() - {target.pk}:
        raise ValidationError("The last National Administrator cannot be suspended or removed.")
    suspending = request.kind == AdminActionKind.SUSPEND
    changes = {"approvers": sorted(approver.access_id for approver in approvers)}
    if target.role == Role.NATIONAL_ADMINISTRATOR:
        if suspending and target.is_active:
            target.is_active = False
            target.save(update_fields=["is_active"])
            changes["account"] = "deactivated"
        elif not suspending:
            target.role = Role.MEMBER
            target.save(update_fields=["role"])
            changes["legacy_role"] = "removed"
    statuses = [S.ACTIVE] if suspending else [S.ACTIVE, S.SUSPENDED]
    status = S.SUSPENDED if suspending else S.REVOKED
    affected = []
    for assignment in _administrator_appointments(target, statuses):
        _set_status(assignment, status, actor, request.reason)
        _record(
            actor,
            f"assignment.{status}",
            assignment=assignment,
            reason=request.reason,
            provenance={"authority": A.NATIONAL_ADMINISTRATOR, "request": str(request.uuid)},
        )
        affected.append(str(assignment.uuid))
    changes["appointments"] = affected
    end_sessions(target)
    request.status = AdminActionStatus.EXECUTED
    request.executed_at = timezone.now()
    request.save(update_fields=["status", "executed_at", "updated_at"])
    action = "administrator.suspended" if suspending else "administrator.removed"
    _record_admin(actor, action, request, changes=changes)
    engine.clear_cache()


@transaction.atomic
def cancel_admin_action(*, actor, request, reason: str = "") -> AdminActionRequest:
    """The requester withdraws a pending request (any administrator may, once
    the requester is no longer one)."""
    _require_administrator(actor)
    request = (
        AdminActionRequest.objects.select_for_update().select_related("target").get(pk=request.pk)
    )
    if request.status != AdminActionStatus.PENDING:
        raise ValidationError({"status": f"This request is already {request.status}."})
    requester_gone = request.requested_by_id not in active_national_administrators()
    if request.requested_by_id != actor.pk and not (
        requester_gone and not _same_person(actor, request.target)
    ):
        raise PermissionDenied("Only the administrator who made the request can cancel it.")
    request.status = AdminActionStatus.CANCELLED
    request.cancelled_at = timezone.now()
    request.cancelled_by = actor
    request.cancel_reason = reason.strip()
    request.save()
    _record_admin(
        actor, "administrator.action_cancelled", request, changes={"why": request.cancel_reason}
    )
    return request


@transaction.atomic
def reinstate_administrator(*, actor, target, reason: str = ""):
    """Lift a National Administrator's suspension (like appointing one, a single
    administrator may do this)."""
    from apps.accounts.models import User

    _require_administrator(actor)
    if _same_person(actor, target):
        raise PermissionDenied("You cannot reinstate yourself.")
    target = User.objects.select_for_update().get(pk=target.pk)
    if administrator_state(target) != STATE_SUSPENDED:
        raise ValidationError("This administrator is not suspended.")
    if AdminActionRequest.objects.filter(target=target, status=AdminActionStatus.PENDING).exists():
        raise ValidationError("A request about this administrator is pending; resolve it first.")
    changes = {}
    if target.role == Role.NATIONAL_ADMINISTRATOR and not target.is_active:
        target.is_active = True
        target.save(update_fields=["is_active"])
        changes["account"] = "reactivated"
    affected = []
    for assignment in _administrator_appointments(target, [S.SUSPENDED]):
        _set_status(assignment, S.ACTIVE, actor, reason.strip())
        _record(
            actor,
            "assignment.reactivated",
            assignment=assignment,
            reason=reason.strip(),
            provenance={"authority": A.NATIONAL_ADMINISTRATOR},
        )
        affected.append(str(assignment.uuid))
    changes["appointments"] = affected
    audit.record(
        actor=actor,
        action="administrator.reinstated",
        target_type="administrator",
        target_id=target.access_id,
        target_label=target.full_name,
        changes=changes,
        reason=reason.strip(),
        provenance={"authority": A.NATIONAL_ADMINISTRATOR},
    )
    engine.clear_cache()
    return target


@transaction.atomic
def recover_administrator(*, member, reason: str, operator: str = "") -> RoleAssignment:
    """Server recovery procedure (docs/access-control.md §7): appoint a National
    Administrator while too few are active for two to approve action on a third.

    Run only from the server (manage.py recover_administrator); never exposed
    through the API. It adds an administrator and never suspends or removes one.
    """
    from apps.membership.models import Member

    _lock_administrators()
    active = active_national_administrators()
    if len(active) >= RECOVERY_THRESHOLD:
        raise ValidationError(
            f"{len(active)} National Administrators are active, enough for the normal "
            "two-administrator workflow. Recovery is not available."
        )
    if not reason.strip():
        raise ValidationError({"reason": "Record why recovery is needed."})
    member = Member.objects.select_for_update().select_related("user").get(pk=member.pk)
    if member.user is None or not member.user.is_active:
        raise ValidationError({"member": "The member needs an active account."})
    if not member.can_hold_roles:
        raise ValidationError(
            {"member": "Only a member with a verified identity and an active membership."}
        )
    if member.user_id in active:
        raise ValidationError({"member": "This member is already a National Administrator."})

    now = timezone.now()
    assignment = RoleAssignment.objects.create(
        member=member,
        role=RoleDefinition.objects.get(code=Role.NATIONAL_ADMINISTRATOR),
        starts_at=now,
        status=S.ACTIVE,
        status_changed_at=now,
        status_reason=reason.strip(),
        authority=A.RECOVERY,
    )
    area = RoleAssignmentArea(assignment=assignment, scope_level=GeographyLevel.COUNTRY)
    area.normalize_scope()
    area.save()
    _record(
        None,
        "administrator.recovered",
        assignment=assignment,
        reason=reason.strip(),
        changes={"member": member.membership_number, "active_before": len(active)},
        provenance={"authority": A.RECOVERY, "operator": operator},
    )
    engine.clear_cache()
    return assignment

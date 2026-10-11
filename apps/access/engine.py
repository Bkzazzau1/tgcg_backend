"""The authorization engine: who may do what, where.

An action P on a resource in area X is allowed only when one single grant
holds P *and* covers X. A grant is an active, in-date assignment of an active
role to a verified member with an active membership, or an account's legacy
fixed role (with its single scope) during the transition. Grants are never
pooled: permissions from one never combine with areas from another.

Everything is read from the database, cached only for the current request,
so suspensions, revocations, expiry and archiving take effect immediately,
including for existing access tokens.
"""

import contextvars
from dataclasses import dataclass

from django.db.models import Q
from django.utils import timezone

from apps.core.choices import GeographyLevel, MembershipState, Role, VerificationStatus
from apps.core.permissions import ROLE_CAPABILITIES
from apps.geography.scope import LEVEL_FIELD

from .models import AssignmentStatus, RoleAssignment, RoleStatus
from .registry import PERMISSIONS, PROTECTED

LEGACY = "legacy"
ASSIGNMENT = "assignment"

# Per-request cache of computed grants, reset by AccessCacheMiddleware.
_request_cache: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "tgcg_access_cache", default=None
)


@dataclass(frozen=True)
class Grant:
    """What one assignment (or a legacy role) allows, and where."""

    source: str
    key: str
    role_code: str
    role_name: str
    permissions: frozenset
    areas: tuple
    protected: bool

    def covers(self, target) -> bool:
        return any(area_contains(area, target) for area in self.areas)

    @property
    def is_national(self) -> bool:
        return any(area.scope_level == GeographyLevel.COUNTRY for area in self.areas)


def area_contains(area, target) -> bool:
    """True when `target` (anything with scope fields) lies inside `area`."""
    field = area.scope_unit_field
    if field is None:
        return True
    return getattr(target, f"{field}_id", None) == getattr(area, f"{field}_id")


def _member_of(user):
    try:
        return user.member
    except Exception:  # noqa: BLE001 - no linked member
        return None


def _compute_grants(user, now) -> tuple[Grant, ...]:
    if not getattr(user, "is_authenticated", False) or not user.is_active:
        return ()
    member = _member_of(user)
    if member is not None and member.membership_state != MembershipState.ACTIVE:
        return ()  # a suspended or deactivated membership blocks everything

    grants: list[Grant] = []
    if user.role != Role.MEMBER:
        # Legacy fixed role: unknown roles grant nothing.
        legacy = frozenset(
            code for code in ROLE_CAPABILITIES.get(user.role, ()) if code in PERMISSIONS
        )
        if legacy:
            grants.append(
                Grant(
                    source=LEGACY,
                    key=LEGACY,
                    role_code=user.role,
                    role_name=Role(user.role).label if user.role in Role.values else user.role,
                    permissions=legacy,
                    areas=(user,),
                    protected=user.role == Role.NATIONAL_ADMINISTRATOR,
                )
            )

    if member is not None and member.verification_status == VerificationStatus.VERIFIED:
        assignments = (
            RoleAssignment.objects.filter(
                member=member,
                status=AssignmentStatus.ACTIVE,
                role__status=RoleStatus.ACTIVE,
                starts_at__lte=now,
            )
            .filter(Q(ends_at__isnull=True) | Q(ends_at__gt=now))
            .select_related("role")
            .prefetch_related("areas")
        )
        for assignment in assignments:
            permissions = assignment.permission_set()
            if not assignment.role.is_protected:
                permissions -= PROTECTED
            areas = tuple(assignment.areas.all())
            if permissions and areas:
                grants.append(
                    Grant(
                        source=ASSIGNMENT,
                        key=str(assignment.uuid),
                        role_code=assignment.role.code,
                        role_name=assignment.role.name,
                        permissions=frozenset(permissions),
                        areas=areas,
                        protected=assignment.role.is_protected,
                    )
                )
    return tuple(grants)


def active_grants(user, now=None) -> tuple[Grant, ...]:
    """The user's grants right now (cached for the current request)."""
    if now is not None:
        return _compute_grants(user, now)
    cache = _request_cache.get()
    key = getattr(user, "pk", None)
    if cache is not None and key in cache:
        return cache[key]
    grants = _compute_grants(user, timezone.now())
    if cache is not None:
        cache[key] = grants
    return grants


def clear_cache():
    cache = _request_cache.get()
    if cache is not None:
        cache.clear()


def permissions_for(user) -> frozenset[str]:
    """Every permission the user holds somewhere (for navigation and coarse checks)."""
    held: set[str] = set()
    for grant in active_grants(user):
        held |= grant.permissions
    return frozenset(held)


def allows(user, permission: str, target=None) -> bool:
    """True when one grant holds `permission` and (if given) covers `target`."""
    for grant in active_grants(user):
        if permission in grant.permissions and (target is None or grant.covers(target)):
            return True
    return False


def areas_for(user, permission: str) -> list:
    """The areas where the user holds `permission`."""
    return [
        area
        for grant in active_grants(user)
        if permission in grant.permissions
        for area in grant.areas
    ]


def scope_q(user, permission: str, prefix: str = "") -> Q:
    """Filter for rows (scope fields under `prefix`) where the user holds `permission`.

    Matches nothing when no grant holds it.
    """
    query = None
    for area in areas_for(user, permission):
        field = area.scope_unit_field
        if field is None:
            return Q()
        condition = Q(**{f"{prefix}{field}_id": getattr(area, f"{field}_id")})
        query = condition if query is None else query | condition
    return query if query is not None else Q(pk__in=[])


def is_national_administrator(user) -> bool:
    return any(grant.protected for grant in active_grants(user))


def level_index(level: str) -> int:
    order = [GeographyLevel.COUNTRY, *LEVEL_FIELD]
    return order.index(level)

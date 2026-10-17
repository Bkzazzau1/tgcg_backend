"""Who a message reaches: groups joined together, kept inside the sender's area.

A group is one of:
- `filter`: members in some areas (all of Nigeria when none), narrowed to
  people with a role, without one, or holding chosen roles, and optionally by
  gender or a checked identity;
- `project`: the people taking part in a project;
- `list`: a saved list.

A message reaches everyone in any of its groups, once. For each channel only
members in the areas where the sender holds that channel's power remain, and
only active memberships.
"""

from django.db.models import Q
from django.utils import timezone

from apps.access import engine
from apps.access.models import AssignmentStatus, RoleAssignment
from apps.core.choices import MembershipState, Role, VerificationStatus
from apps.core.permissions import Capability
from apps.geography.scope import SCOPE_FIELDS, ScopeRef, any_of, within_scope_q
from apps.membership.models import Member
from apps.projects.models import Participation, ParticipationStatus

from .models import Channel, Device

C = Capability
POWER = {
    Channel.SMS: C.SEND_SMS,
    Channel.WHATSAPP: C.SEND_WHATSAPP,
    Channel.PUSH: C.SEND_PUSH_NOTIFICATIONS,
}


def area(attrs: dict) -> ScopeRef:
    return ScopeRef(attrs["scope_level"], **{field: attrs.get(field) for field in SCOPE_FIELDS})


def holders(roles=None):
    """Members holding a role now (any role, or one of `roles`): an appointment
    that has started and not ended, or a staff or agent account."""
    now = timezone.now()
    appointed = RoleAssignment.objects.filter(
        Q(ends_at__isnull=True) | Q(ends_at__gt=now),
        status=AssignmentStatus.ACTIVE,
        starts_at__lte=now,
    )
    accounts = Member.objects.filter(user__isnull=False).exclude(user__role=Role.MEMBER)
    if roles:
        appointed = appointed.filter(role__code__in=roles)
        accounts = accounts.filter(user__role__in=roles)
    return Member.objects.filter(
        Q(pk__in=appointed.values("member")) | Q(pk__in=accounts.values("pk"))
    ).values("pk")


def group_q(group: dict) -> Q:
    kind = group["type"]
    if kind == "project":
        taking_part = Participation.objects.filter(
            project=group["project"], status=ParticipationStatus.ACCEPTED
        )
        return Q(pk__in=taking_part.values("member"))
    if kind == "list":
        return Q(pk__in=group["list"].members.values("pk"))
    areas = group.get("areas") or []
    query = engine.areas_q([area(attrs) for attrs in areas]) if areas else Q()
    people = group.get("people") or "everyone"
    if people == "withRole":
        query &= Q(pk__in=holders())
    elif people == "noRole":
        query &= ~Q(pk__in=holders())
    elif people == "roles":
        query &= Q(pk__in=holders(group.get("roles") or []))
    if group.get("gender"):
        query &= Q(gender=group["gender"])
    if group.get("verified_only"):
        query &= Q(verification_status=VerificationStatus.VERIFIED)
    return query


def audience_q(groups) -> Q:
    """Everyone in any group. An empty filter (all of Nigeria) means everyone."""
    return any_of(*(group_q(group) for group in groups)) if groups else Q(pk__in=[])


def reached(sender, groups, channel):
    """The active members a message on `channel` reaches."""
    return (
        Member.objects.filter(membership_state=MembershipState.ACTIVE)
        .filter(audience_q(groups))
        .filter(within_scope_q(sender, permission=POWER[channel]))
    )


def addressable(members, channel):
    """Those with somewhere to receive it: a phone number, or the app."""
    if channel == Channel.PUSH:
        return members.filter(user__in=Device.objects.values("user"))
    return members.exclude(phone_number="")


def sms_pages(text: str) -> int:
    """How many SMS one message takes: 160 plain characters fit in one, 153
    per page in a longer message; any other character halves that."""
    plain = all(ord(char) < 128 for char in text)
    single, part = (160, 153) if plain else (70, 67)
    if len(text) <= single:
        return 1
    return -(-len(text) // part)

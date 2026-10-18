"""Members and exco in an area, counted for the Members & Exco page and the dashboard.

Exco are the members holding an appointment (a role) over somewhere in the
area: state, district, LGA, ward or polling unit executives, coordinators
and any other role a National Administrator created. An appointment counts
while it is active or suspended and has not ended; the protected National
Administrator role is not exco.

A member is fully verified when their identity is verified and their
profile rests on the card: VIN, a photo of the card, a selfie, gender and
date of birth. Executives a chapter sent in are verified on the chapter's
word, so many are verified without being fully verified.
"""

import operator
from datetime import date, timedelta
from functools import reduce

from django.db.models import Case, Count, IntegerField, Q, Value, When
from django.db.models.functions import TruncWeek
from django.utils import timezone

from apps.access.models import AssignmentStatus, RoleAssignmentArea
from apps.core.choices import GeographyLevel, RegistrationChannel, VerificationStatus
from apps.geography.models import Lga, PollingUnit, State, Ward, Zone
from apps.geography.scope import LEVEL_FIELD, within_scope_q

from .models import Member

L = GeographyLevel
V = VerificationStatus

# What a profile resting on the card has; each key names what is missing.
GAPS = {
    "vin": Q(vin__isnull=True),
    "cardPhoto": Q(pvc_image=""),
    "selfie": Q(photo=""),
    "gender": Q(gender=""),
    "dateOfBirth": Q(date_of_birth__isnull=True),
}
COMPLETE = ~reduce(operator.or_, GAPS.values())
FULLY_VERIFIED = Q(verification_status=V.VERIFIED) & COMPLETE
SIGNED_IN = Q(user__last_login__isnull=False)

# Ages in years: (label, youngest, oldest); None is open-ended.
AGE_BANDS = [
    ("under18", None, 17),
    ("18-24", 18, 24),
    ("25-34", 25, 34),
    ("35-44", 35, 44),
    ("45-54", 45, 54),
    ("55-64", 55, 64),
    ("65+", 65, None),
]

# The smaller areas each area is broken down into.
CHILD_LEVEL = {
    L.COUNTRY: L.STATE,
    L.GEOPOLITICAL_ZONE: L.STATE,
    L.STATE: L.LGA,
    L.SENATORIAL_DISTRICT: L.LGA,
    L.LGA: L.WARD,
    L.WARD: L.POLLING_UNIT,
}
# Nigeria can also be broken down by geopolitical zone.
OTHER_BREAKDOWNS = {L.COUNTRY: (L.GEOPOLITICAL_ZONE,)}
CHILD_MODEL = {
    L.GEOPOLITICAL_ZONE: Zone,
    L.STATE: State,
    L.LGA: Lga,
    L.WARD: Ward,
    L.POLLING_UNIT: PollingUnit,
}
# How deep each level lies below Nigeria, for ordering exco from the top.
DEPTH = {L.COUNTRY: 0, **{level: depth for depth, level in enumerate(LEVEL_FIELD, start=1)}}
WEEKS = 12


def gaps(member) -> list[str]:
    """What the member's profile still lacks to rest on the card."""
    missing = []
    if not member.vin:
        missing.append("vin")
    if not member.pvc_image:
        missing.append("cardPhoto")
    if not member.photo:
        missing.append("selfie")
    if not member.gender:
        missing.append("gender")
    if not member.date_of_birth:
        missing.append("dateOfBirth")
    return missing


def verified_how(member) -> str | None:
    """automatic (the system's policy), chapter (a chapter's list) or person."""
    if member.verification_status != V.VERIFIED:
        return None
    if member.verified_by_id is None:
        return "automatic"
    if member.registration_channel == RegistrationChannel.IMPORT:
        return "chapter"
    return "person"


def exco_areas(scope=None, now=None):
    """Appointment areas that count as exco, inside `scope` (all when None)."""
    now = now or timezone.now()
    areas = RoleAssignmentArea.objects.filter(
        assignment__status__in=[AssignmentStatus.ACTIVE, AssignmentStatus.SUSPENDED],
        assignment__role__is_protected=False,
    ).filter(Q(assignment__ends_at__isnull=True) | Q(assignment__ends_at__gt=now))
    return areas.filter(within_scope_q(scope)) if scope is not None else areas


def role_holders_q() -> Q:
    """Members who are exco anywhere."""
    return Q(pk__in=exco_areas().values("assignment__member_id"))


PLACE_NAMES = ("zone__name", "state__name", "lga__name", "ward__name", "polling_unit__name")


def search_q(text: str) -> Q:
    """Members matching every word of `text`: in their name or membership
    number, the places where they registered, or a post they hold now (its
    position or the place it covers). "women leader zaria" finds the women
    leaders posted in Zaria."""
    condition = Q()
    for word in text.split():
        places = Q()
        for name in PLACE_NAMES:
            places |= Q(**{f"{name}__icontains": word})
        posts = exco_areas().filter(Q(assignment__role__name__icontains=word) | places)
        condition &= (
            Q(full_name__icontains=word)
            | Q(membership_number__iexact=word)
            | places
            | Q(pk__in=posts.values("assignment__member_id"))
        )
    return condition


def depth_order():
    """Exco from the top: Nigeria, then zone, state, district, LGA, ward, unit."""
    return Case(
        *(When(scope_level=level, then=Value(depth)) for level, depth in DEPTH.items()),
        output_field=IntegerField(),
    )


def _years_ago(today: date, years: int) -> date:
    try:
        return today.replace(year=today.year - years)
    except ValueError:  # 29 February
        return today.replace(year=today.year - years, day=28)


def _age_counts(today) -> dict:
    counts = {}
    for label, youngest, oldest in AGE_BANDS:
        born = Q(date_of_birth__isnull=False)
        if youngest is not None:
            born &= Q(date_of_birth__lte=_years_ago(today, youngest))
        if oldest is not None:
            born &= Q(date_of_birth__gt=_years_ago(today, oldest + 1))
        counts[f"age:{label}"] = Count("pk", filter=born)
    counts["age:unknown"] = Count("pk", filter=Q(date_of_birth__isnull=True))
    return counts


def _by(queryset, field) -> dict[str, int]:
    rows = queryset.values_list(field).annotate(n=Count("pk")).order_by()
    return {value or "": n for value, n in rows}


def _profile(totals: dict) -> dict:
    return {key: totals[f"has:{key}"] for key in GAPS} | {
        "phoneConfirmed": totals["phoneConfirmed"],
        "appAccount": totals["appAccount"],
        "signedIn": totals["signedIn"],
    }


def _members_summary(members, now) -> dict:
    today = timezone.localdate()
    totals = members.aggregate(
        total=Count("pk"),
        new7=Count("pk", filter=Q(created_at__gte=now - timedelta(days=7))),
        new30=Count("pk", filter=Q(created_at__gte=now - timedelta(days=30))),
        fullyVerified=Count("pk", filter=FULLY_VERIFIED),
        withRole=Count("pk", filter=role_holders_q()),
        phoneConfirmed=Count("pk", filter=Q(phone_verified_at__isnull=False)),
        appAccount=Count("pk", filter=Q(user__isnull=False)),
        signedIn=Count("pk", filter=SIGNED_IN),
        disability=Count("pk", filter=~Q(disabilities=[])),
        **{f"has:{key}": Count("pk", filter=~gap) for key, gap in GAPS.items()},
        **_age_counts(today),
    )
    monday = today - timedelta(days=today.weekday())
    first = monday - timedelta(weeks=WEEKS - 1)
    weekly = {
        row["week"].date() if hasattr(row["week"], "date") else row["week"]: row["n"]
        for row in members.filter(created_at__date__gte=first)
        .annotate(week=TruncWeek("created_at"))
        .values("week")
        .annotate(n=Count("pk"))
        .order_by("week")
    }
    return {
        "total": totals["total"],
        "new_7_days": totals["new7"],
        "new_30_days": totals["new30"],
        "fully_verified": totals["fullyVerified"],
        "with_role": totals["withRole"],
        "with_disability": totals["disability"],
        "verification": _by(members, "verification_status"),
        "standing": _by(members, "membership_state"),
        "gender": _by(members, "gender"),
        "channel": _by(members, "registration_channel"),
        "age": {label: totals[f"age:{label}"] for label, *_ in AGE_BANDS}
        | {"unknown": totals["age:unknown"]},
        "profile": _profile(totals),
        "weekly": [
            {"week": week.isoformat(), "count": weekly.get(week, 0)}
            for week in (first + timedelta(weeks=n) for n in range(WEEKS))
        ],
    }


def _exco_summary(areas, scope) -> dict:
    people = Member.objects.filter(pk__in=areas.values("assignment__member_id"))
    totals = people.aggregate(
        total=Count("pk"),
        verified=Count("pk", filter=Q(verification_status=V.VERIFIED)),
        fullyVerified=Count("pk", filter=FULLY_VERIFIED),
        women=Count("pk", filter=Q(gender="female")),
        phoneConfirmed=Count("pk", filter=Q(phone_verified_at__isnull=False)),
        appAccount=Count("pk", filter=Q(user__isnull=False)),
        signedIn=Count("pk", filter=SIGNED_IN),
        **{f"has:{key}": Count("pk", filter=~gap) for key, gap in GAPS.items()},
    )
    by_level = {
        level: n
        for level, n in areas.values_list("scope_level")
        .annotate(n=Count("assignment", distinct=True))
        .order_by()
    }
    roles = (
        areas.values("assignment__role__code", "assignment__role__name")
        .annotate(n=Count("assignment", distinct=True))
        .order_by("-n", "assignment__role__name")
    )
    return {
        "people": totals["total"],
        "appointments": areas.values("assignment_id").distinct().count(),
        "here": areas.filter(scope_level=scope.scope_level)
        .values("assignment_id")
        .distinct()
        .count(),
        "suspended": areas.filter(assignment__status=AssignmentStatus.SUSPENDED)
        .values("assignment_id")
        .distinct()
        .count(),
        "verified": totals["verified"],
        "fully_verified": totals["fullyVerified"],
        "women": totals["women"],
        "profile": _profile(totals),
        "by_level": by_level,
        "roles": [
            {
                "role": row["assignment__role__code"],
                "name": row["assignment__role__name"],
                "count": row["n"],
            }
            for row in roles
        ],
    }


def _children(scope, level):
    model = CHILD_MODEL[level]
    units = model.objects.all()
    field = scope.scope_unit_field
    if field is not None:
        parent = {
            ("state", "zone"): "zone_id",
            ("lga", "state"): "state_id",
            ("lga", "senatorial_district"): "senatorial_district_id",
            ("ward", "lga"): "lga_id",
            ("polling_unit", "ward"): "ward_id",
        }[(LEVEL_FIELD[level], field)]
        units = units.filter(**{parent: getattr(scope, f"{field}_id")})
    return units.order_by("name").values_list("code", "name")


def breakdowns(scope) -> list[str]:
    """The levels an area can be broken down into, the usual one first."""
    usual = CHILD_LEVEL.get(scope.scope_level)
    return [] if usual is None else [usual, *OTHER_BREAKDOWNS.get(scope.scope_level, ())]


def _parts(members, areas, scope, now, level) -> tuple[str | None, list[dict]]:
    if level is None:
        return None, []
    field = LEVEL_FIELD[level]
    recent = now - timedelta(days=30)
    member_rows = {
        row[f"{field}__code"]: row
        for row in members.exclude(**{f"{field}__isnull": True})
        .values(f"{field}__code")
        .annotate(
            members=Count("pk"),
            verified=Count("pk", filter=Q(verification_status=V.VERIFIED)),
            fully_verified=Count("pk", filter=FULLY_VERIFIED),
            women=Count("pk", filter=Q(gender="female")),
            new_30_days=Count("pk", filter=Q(created_at__gte=recent)),
        )
        .order_by()
    }
    exco_rows = {
        row[f"{field}__code"]: row
        for row in areas.exclude(**{f"{field}__isnull": True})
        .values(f"{field}__code")
        .annotate(
            appointments=Count("assignment", distinct=True),
            people=Count("assignment__member", distinct=True),
        )
        .order_by()
    }
    parts = []
    for code, name in _children(scope, level):
        counted = member_rows.get(code, {})
        exco = exco_rows.get(code, {})
        parts.append(
            {
                "code": code,
                "name": name,
                "members": counted.get("members", 0),
                "verified": counted.get("verified", 0),
                "fully_verified": counted.get("fully_verified", 0),
                "women": counted.get("women", 0),
                "new_30_days": counted.get("new_30_days", 0),
                "exco": exco.get("appointments", 0),
                "exco_people": exco.get("people", 0),
            }
        )
    return level, parts


def summary(scope, by=None) -> dict:
    """Everything the Members & Exco page shows about one area, with its
    smaller areas at level `by` (one of `breakdowns`; the usual one by default)."""
    now = timezone.now()
    members = Member.objects.filter(within_scope_q(scope))
    areas = exco_areas(scope, now)
    by, parts = _parts(members, areas, scope, now, by or next(iter(breakdowns(scope)), None))
    return {
        "members": _members_summary(members, now),
        "exco": _exco_summary(areas, scope),
        "by": by,
        "parts": parts,
        "coverage": {
            "areas": len(parts),
            "with_members": sum(1 for part in parts if part["members"]),
            "with_exco": sum(1 for part in parts if part["exco"]),
        },
    }

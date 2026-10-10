"""Live, unofficial collation computed from verified submissions only.

Mirrors the client's CollationEngine: expected polling units come from
master data, only verified results are summed, and each level can be broken
down into its direct children (country → zones → states → senatorial
districts → LGAs → wards → polling units).
"""

from django.db.models import Count, Q, Sum

from apps.core.choices import GeographyLevel, RecordStatus
from apps.geography.models import Lga, PollingUnit, SenatorialDistrict, State, Ward, Zone
from apps.geography.scope import LEVEL_FIELD, scope_payload, within_scope_q

from .models import PartyVote, ResultSubmission

L = GeographyLevel

# Path from a PollingUnit to each scope unit.
PU_PATH = {
    "zone": "ward__lga__state__zone",
    "state": "ward__lga__state",
    "senatorial_district": "ward__lga__senatorial_district",
    "lga": "ward__lga",
    "ward": "ward",
    "polling_unit": "pk",
}

CHILD_LEVEL = {
    L.COUNTRY: L.GEOPOLITICAL_ZONE,
    L.GEOPOLITICAL_ZONE: L.STATE,
    L.STATE: L.SENATORIAL_DISTRICT,
    L.SENATORIAL_DISTRICT: L.LGA,
    L.LGA: L.WARD,
    L.WARD: L.POLLING_UNIT,
}

CHILD_MODEL = {
    L.GEOPOLITICAL_ZONE: Zone,
    L.STATE: State,
    L.SENATORIAL_DISTRICT: SenatorialDistrict,
    L.LGA: Lga,
    L.WARD: Ward,
    L.POLLING_UNIT: PollingUnit,
}

# Path from a unit at each level to its broader scope units.
UNIT_PATH = {
    L.GEOPOLITICAL_ZONE: {},
    L.STATE: {"zone": "zone"},
    L.SENATORIAL_DISTRICT: {"zone": "state__zone", "state": "state"},
    L.LGA: {
        "zone": "state__zone",
        "state": "state",
        "senatorial_district": "senatorial_district",
    },
    L.WARD: {
        "zone": "lga__state__zone",
        "state": "lga__state",
        "senatorial_district": "lga__senatorial_district",
        "lga": "lga",
    },
    L.POLLING_UNIT: {field: path for field, path in PU_PATH.items() if path != "pk"},
}
LEVEL_DEPTH = {level: depth for depth, level in enumerate(LEVEL_FIELD, start=1)}


def _pu_filter(scope) -> Q:
    field = scope.scope_unit_field
    if field is None:
        return Q()
    return Q(**{PU_PATH[field]: getattr(scope, f"{field}_id")})


def _expected_pus(election, scope):
    return PollingUnit.objects.filter(_pu_filter(election), _pu_filter(scope))


def _blank_votes(election):
    return {party.acronym: 0 for party in election.parties.all()}


def summarize(election, scope, *, include_children=True) -> dict:
    submissions = ResultSubmission.objects.filter(election=election).filter(within_scope_q(scope))
    verified = submissions.filter(status=RecordStatus.VERIFIED)

    counts = submissions.aggregate(
        reported=Count("polling_unit", distinct=True),
        verified=Count("pk", filter=Q(status=RecordStatus.VERIFIED)),
        under_review=Count("pk", filter=Q(status=RecordStatus.UNDER_REVIEW)),
        disputed=Count("pk", filter=Q(status=RecordStatus.DISPUTED)),
    )
    totals = verified.aggregate(
        accredited_voters=Sum("accredited_voters"),
        total_valid_votes=Sum("total_valid_votes"),
        rejected_votes=Sum("rejected_votes"),
    )
    party_votes = _blank_votes(election)
    for row in (
        PartyVote.objects.filter(submission__in=verified)
        .values("party__acronym")
        .annotate(votes=Sum("votes"))
    ):
        party_votes[row["party__acronym"]] = row["votes"]

    expected = _expected_pus(election, scope).count()
    summary = {
        "election": election.code,
        "official": False,
        "scope": scope_payload(scope),
        "expected_polling_units": expected,
        "reported_polling_units": counts["reported"],
        "verified_polling_units": counts["verified"],
        "under_review": counts["under_review"],
        "disputed": counts["disputed"],
        "missing_polling_units": max(expected - counts["verified"], 0),
        "completion_percent": round(counts["verified"] * 100 / expected, 2) if expected else 0.0,
        "accredited_voters": totals["accredited_voters"] or 0,
        "total_valid_votes": totals["total_valid_votes"] or 0,
        "rejected_votes": totals["rejected_votes"] or 0,
        "party_votes": dict(sorted(party_votes.items(), key=lambda kv: (-kv[1], kv[0]))),
        "leading_party": _leader(party_votes),
    }
    if include_children and scope.scope_level in CHILD_LEVEL:
        summary["children"] = _children(election, scope, submissions, verified)
    return summary


def _leader(party_votes):
    if not party_votes or max(party_votes.values()) == 0:
        return None
    best = max(party_votes.values())
    leaders = [acronym for acronym, votes in party_votes.items() if votes == best]
    return leaders[0] if len(leaders) == 1 else None  # None on a tie


def _units_within(child_level, scope):
    """Units at `child_level` inside (or, if broader, containing) `scope`."""
    units = CHILD_MODEL[child_level].objects.all()
    field = scope.scope_unit_field
    if field is None:
        return units
    unit_id = getattr(scope, f"{field}_id")
    if LEVEL_DEPTH[child_level] <= LEVEL_DEPTH[scope.scope_level]:
        return units.filter(pk=getattr(scope, f"{LEVEL_FIELD[child_level]}_id"))
    return units.filter(**{UNIT_PATH[child_level][field]: unit_id})


def _children(election, scope, submissions, verified):
    child_level = CHILD_LEVEL[scope.scope_level]
    child_field = LEVEL_FIELD[child_level]
    # Children of the requested area, limited to what the election covers.
    units = _units_within(child_level, scope) & _units_within(child_level, election)

    expected = dict(
        _expected_pus(election, scope).values_list(PU_PATH[child_field]).annotate(n=Count("pk"))
    )
    verified_counts = dict(verified.values_list(f"{child_field}_id").annotate(n=Count("pk")))
    reported_counts = dict(
        submissions.values_list(f"{child_field}_id").annotate(
            n=Count("polling_unit", distinct=True)
        )
    )
    votes = {}
    for child_id, acronym, total in (
        PartyVote.objects.filter(submission__in=verified)
        .values_list(f"submission__{child_field}_id", "party__acronym")
        .annotate(total=Sum("votes"))
    ):
        votes.setdefault(child_id, _blank_votes(election))[acronym] = total

    rows = []
    for unit in units.order_by("name"):
        exp = expected.get(unit.pk, 0)
        ver = verified_counts.get(unit.pk, 0)
        party_votes = votes.get(unit.pk, _blank_votes(election))
        rows.append(
            {
                "level": child_level,
                "code": unit.code,
                "name": unit.name,
                "expected_polling_units": exp,
                "reported_polling_units": reported_counts.get(unit.pk, 0),
                "verified_polling_units": ver,
                "completion_percent": round(ver * 100 / exp, 2) if exp else 0.0,
                "party_votes": party_votes,
                "leading_party": _leader(party_votes),
            }
        )
    return rows

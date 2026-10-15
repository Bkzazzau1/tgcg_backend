"""How many members are registered in each part of an area (GET /members/counts/)."""

import pytest

from apps.core.choices import GeographyLevel, Role
from apps.core.models import Sequence
from apps.geography.models import Lga, PollingUnit, Ward
from apps.membership.models import Member
from apps.membership.tests.test_membership import client_for
from conftest import make_member

pytestmark = pytest.mark.django_db

COUNTS = "/api/v1/members/counts/"


@pytest.fixture
def units(db):
    lga = Lga.objects.get(code="KD-KADUNA-NORTH")
    first = Ward.objects.create(code="KD-KN-W01", name="Ward 01", lga=lga)
    second = Ward.objects.create(code="KD-KN-W02", name="Ward 02", lga=lga)
    return [
        PollingUnit.objects.create(code="KD-KN-W01-PU001", name="Unit 1", ward=first),
        PollingUnit.objects.create(code="KD-KN-W01-PU002", name="Unit 2", ward=first),
        PollingUnit.objects.create(code="KD-KN-W02-PU001", name="Unit 3", ward=second),
    ]


def member_at(unit, phone):
    member = Member(
        full_name="Ada Bello",
        phone_number=phone,
        scope_level=GeographyLevel.POLLING_UNIT,
        polling_unit=unit,
        membership_number=f"TGCG-{Sequence.next_value('member'):06d}",
    )
    member.full_clean()
    member.save()
    return member


@pytest.fixture
def members(units):
    member_at(units[0], "08030000001")
    member_at(units[0], "08030000002")
    member_at(units[2], "08030000003")
    make_member("08030000004")  # registered at the LGA only, without a ward
    make_member("08030000005", lga="LA-IKEJA")  # another state


def coordinator(lga="KD-KADUNA-NORTH"):
    return client_for(
        access_id="LGA-1",
        full_name="Coordinator",
        role=Role.LGA_COORDINATOR,
        scope_level=GeographyLevel.LGA,
        lga=Lga.objects.get(code=lga),
    )


def test_the_administrator_counts_each_ward_and_polling_unit(members):
    admin = client_for(access_id="NA-1", full_name="Admin", role=Role.NATIONAL_ADMINISTRATOR)

    wards = admin.get(COUNTS, {"level": "lga", "code": "KD-KADUNA-NORTH", "by": "ward"})
    assert wards.status_code == 200, wards.content
    # The member registered at the LGA only counts in the total, in no ward.
    assert wards.json()["total"] == 4
    assert wards.json()["counts"] == {"KD-KN-W01": 2, "KD-KN-W02": 1}
    assert wards.json()["scope"]["lgaId"] == "KD-KADUNA-NORTH"

    units = admin.get(COUNTS, {"level": "ward", "code": "KD-KN-W01", "by": "pollingUnit"})
    assert units.json() == {
        "scope": units.json()["scope"],
        "by": "pollingUnit",
        "total": 2,
        "counts": {"KD-KN-W01-PU001": 2},
    }

    states = admin.get(COUNTS, {"by": "state"})
    assert states.json()["counts"] == {"KD": 4, "LA": 1}


def test_a_coordinator_counts_only_areas_wholly_inside_their_own(members):
    client = coordinator()
    assert client.get(COUNTS, {"level": "lga", "code": "KD-KADUNA-NORTH", "by": "ward"}).json()[
        "counts"
    ] == {"KD-KN-W01": 2, "KD-KN-W02": 1}
    # Never a partial count of a bigger or another area.
    for area in ({"level": "state", "code": "KD"}, {"level": "lga", "code": "LA-IKEJA"}, {}):
        assert client.get(COUNTS, {**area, "by": "ward"}).status_code == 403, area


def test_counting_needs_member_access_and_a_smaller_level(members):
    director = client_for(
        access_id="SRD-1", full_name="Director", role=Role.SITUATION_ROOM_DIRECTOR
    )
    assert director.get(COUNTS, {"by": "state"}).status_code == 403

    admin = client_for(access_id="NA-1", full_name="Admin", role=Role.NATIONAL_ADMINISTRATOR)
    for query in (
        {"level": "ward", "code": "KD-KN-W01", "by": "lga"},
        {"level": "ward", "code": "KD-KN-W01"},
        {"level": "pollingUnit", "code": "KD-KN-W01-PU001", "by": "pollingUnit"},
    ):
        assert admin.get(COUNTS, query).status_code == 400, query
    assert (
        admin.get(COUNTS, {"level": "ward", "code": "NO-SUCH", "by": "pollingUnit"}).status_code
        == 400
    )

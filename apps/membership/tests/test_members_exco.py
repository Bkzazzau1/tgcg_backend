"""Members and exco in an area: the analysis, the exco lists and the member filters."""

from datetime import timedelta

import pytest
from django.utils import timezone

from apps.access.tests.test_access import appoint, client_for, custom_role, member_account
from apps.accounts.models import User
from apps.core.choices import RegistrationChannel, Role
from apps.core.permissions import Capability
from apps.geography.models import Lga, PollingUnit, Ward
from conftest import PASSWORD

pytestmark = pytest.mark.django_db
C = Capability
ANALYSIS = "/api/v1/members/analysis/"
EXCO = "/api/v1/members/exco/"


@pytest.fixture
def admin():
    return User.objects.create_user(
        "NA-1", PASSWORD, full_name="National Admin", role=Role.NATIONAL_ADMINISTRATOR
    )


@pytest.fixture
def unit(db):
    lga = Lga.objects.get(code="KD-KADUNA-NORTH")
    ward = Ward.objects.create(code="KD-KADUNA-NORTH-W01", name="Ward 01", lga=lga)
    return PollingUnit.objects.create(code="KD-KN-W01-PU001", name="Unit 1", ward=ward)


def complete(member, years=30, gender="female"):
    """A profile resting on the card: VIN, card photo, selfie, gender and birth date."""
    member.vin = f"90F5B0{member.pk:013d}"[:19]
    member.pvc_image = "members/pvc/card.jpg"
    member.photo = "members/photos/selfie.jpg"
    member.gender = gender
    member.date_of_birth = timezone.localdate() - timedelta(days=365 * years + 30)
    member.save()
    return member


@pytest.fixture
def exco(admin, unit):
    """Kaduna: a state secretary (fully verified), an LGA women leader from a
    chapter's list, and a polling unit coordinator. Kano: one state officer."""
    secretary = complete(member_account("08033330001", name="Aisha Bello"))
    leader = member_account("08033330002", name="Hauwa Musa")
    leader.registration_channel = RegistrationChannel.IMPORT
    leader.verified_by = admin
    leader.gender = "female"
    leader.save()
    coordinator = member_account("08033330003", name="Sani Garba")
    kano = member_account("08033330004", name="Kano Officer", lga="KN-AJINGI")

    state_role = custom_role(admin, "State Secretary", [C.VIEW_GEOGRAPHY])
    lga_role = custom_role(admin, "LGA Women Leader", [C.VIEW_GEOGRAPHY], levels=("lga",))
    unit_role = custom_role(admin, "PU Coordinator", [C.VIEW_GEOGRAPHY], levels=("pollingUnit",))
    for member, role, area in (
        (secretary, state_role, {"level": "state", "stateId": "KD"}),
        (leader, lga_role, {"level": "lga", "lgaId": "KD-KADUNA-NORTH"}),
        (coordinator, unit_role, {"level": "pollingUnit", "pollingUnitId": unit.code}),
        (kano, state_role, {"level": "state", "stateId": "KN"}),
    ):
        assert appoint(admin, member, role, [area]).status_code == 201
    member_account("08033330005", name="Plain Member", verified=False)  # no post
    return {"secretary": secretary, "leader": leader, "coordinator": coordinator, "kano": kano}


def test_the_national_view_counts_members_and_exco_state_by_state(admin, exco):
    exco["coordinator"].disabilities = ["physical"]
    exco["coordinator"].save()
    body = client_for(admin).get(ANALYSIS).json()
    members = body["members"]
    assert members["total"] == 5
    assert members["with_role"] == 4
    assert members["fully_verified"] == 1
    assert members["new_7_days"] == 5
    assert members["verification"] == {"verified": 4, "unverified": 1}
    assert members["gender"]["female"] == 2
    assert members["age"]["25-34"] == 1 and members["age"]["unknown"] == 4
    assert members["profile"]["selfie"] == 1 and members["profile"]["vin"] == 1
    assert members["channel"]["import"] == 1
    assert members["with_disability"] == 1
    assert len(members["weekly"]) == 12 and members["weekly"][-1]["count"] == 5

    posts = body["exco"]
    assert posts["people"] == 4 and posts["appointments"] == 4
    assert posts["by_level"] == {"state": 2, "lga": 1, "pollingUnit": 1}
    assert posts["roles"][0] == {
        "role": posts["roles"][0]["role"],
        "name": "State Secretary",
        "count": 2,
    }
    assert posts["verified"] == 4 and posts["fully_verified"] == 1 and posts["women"] == 2

    assert body["by"] == "state"
    states = {part["code"]: part for part in body["parts"]}
    assert len(states) == 37  # every state, with or without members
    assert states["KD"]["members"] == 4 and states["KD"]["exco"] == 3
    assert states["KD"]["women"] == 2 and states["KD"]["fully_verified"] == 1
    assert states["KN"]["exco"] == 1 and states["LA"]["members"] == 0
    assert body["coverage"] == {"areas": 37, "with_members": 2, "with_exco": 2}


def test_a_state_shows_its_own_exco_and_each_lga(admin, exco):
    body = client_for(admin).get(ANALYSIS, {"level": "state", "code": "KD"}).json()
    assert body["scope"]["stateId"] == "KD"
    assert body["exco"]["appointments"] == 3 and body["exco"]["here"] == 1
    assert body["by"] == "lga"
    lgas = {part["code"]: part for part in body["parts"]}
    assert lgas["KD-KADUNA-NORTH"]["exco"] == 2  # the LGA leader and the unit coordinator
    assert lgas["KD-KADUNA-NORTH"]["exco_people"] == 2

    national = client_for(admin)
    state_exco = national.get(EXCO, {"level": "state", "code": "KD", "at": "state"}).json()
    [post] = state_exco["results"]
    assert post["role_name"] == "State Secretary" and post["area_name"] == "Kaduna"
    assert post["appointment_state"] == "active"
    assert post["member"]["full_name"] == "Aisha Bello"
    assert post["member"]["phone_number"] == "+2348033330001"
    assert post["member"]["gaps"] == [] and post["member"]["verified_how"] == "automatic"

    everyone = national.get(EXCO, {"level": "state", "code": "KD"}).json()["results"]
    # From the top down: state, then LGA, then polling unit.
    assert [p["role_name"] for p in everyone] == [
        "State Secretary",
        "LGA Women Leader",
        "PU Coordinator",
    ]
    assert everyone[1]["member"]["verified_how"] == "chapter"
    assert everyone[1]["member"]["gaps"] == ["vin", "cardPhoto", "selfie", "dateOfBirth"]
    assert everyone[2]["area"]["pollingUnitId"] == "KD-KN-W01-PU001"

    # Not fully verified: verified by a chapter or the system, but the card is missing.
    behind = national.get(EXCO, {"level": "state", "code": "KD", "show": "notFullyVerified"})
    assert [p["member"]["full_name"] for p in behind.json()["results"]] == [
        "Hauwa Musa",
        "Sani Garba",
    ]
    found = national.get(EXCO, {"level": "state", "code": "KD", "search": "women"}).json()
    assert [p["member"]["full_name"] for p in found["results"]] == ["Hauwa Musa"]
    lga = national.get(EXCO, {"level": "lga", "code": "KD-KADUNA-NORTH", "at": "lga"}).json()
    assert [p["role_name"] for p in lga["results"]] == ["LGA Women Leader"]


def test_member_lists_narrow_to_exco_and_what_is_missing(admin, exco):
    national = client_for(admin)

    def names(show):
        response = national.get("/api/v1/members/", {"show": show, "ordering": "full_name"})
        assert response.status_code == 200, response.content
        return [member["full_name"] for member in response.json()["results"]]

    assert names("withRole") == ["Aisha Bello", "Hauwa Musa", "Kano Officer", "Sani Garba"]
    assert names("withoutRole") == ["Plain Member"]
    assert names("fullyVerified") == ["Aisha Bello"]
    assert "Aisha Bello" not in names("notFullyVerified")
    assert len(names("neverSignedIn")) == 5
    row = national.get("/api/v1/members/", {"search": "Hauwa"}).json()["results"][0]
    assert row["gaps"] == ["vin", "cardPhoto", "selfie", "dateOfBirth"]
    assert row["signed_in"] is False and row["verified_how"] == "chapter"


def test_each_sees_only_areas_wholly_theirs(exco, lga_coordinator):
    coordinator = client_for(lga_coordinator)
    own = coordinator.get(ANALYSIS, {"level": "lga", "code": "KD-KADUNA-NORTH"})
    assert own.status_code == 200, own.content
    assert own.json()["exco"]["appointments"] == 2
    assert own.json()["by"] == "ward"
    assert coordinator.get(ANALYSIS, {"level": "state", "code": "KD"}).status_code == 403
    assert coordinator.get(ANALYSIS).status_code == 403
    assert coordinator.get(EXCO, {"level": "state", "code": "KD"}).status_code == 403
    plain = client_for(member_account("08033330009", name="Bala Musa").user)
    assert plain.get(ANALYSIS).status_code == 403
    assert plain.get(EXCO).status_code == 403


def test_each_level_has_its_own_exco_zones_included(admin, exco):
    zonal = member_account("08033330006", name="Zainab Zonal")
    role = custom_role(admin, "Zonal Secretary", [C.VIEW_GEOGRAPHY], levels=("geopoliticalZone",))
    area = {"level": "geopoliticalZone", "zoneId": "NW"}
    assert appoint(admin, zonal, role, [area]).status_code == 201
    national = client_for(admin)

    by_zone = national.get(ANALYSIS, {"by": "geopoliticalZone"}).json()
    assert by_zone["by"] == "geopoliticalZone"
    assert by_zone["breakdowns"] == ["state", "geopoliticalZone"]
    zones = {part["code"]: part for part in by_zone["parts"]}
    assert len(zones) == 6
    assert zones["NW"]["exco"] == 5 and zones["NW"]["members"] == 6
    assert national.get(ANALYSIS, {"by": "ward"}).status_code == 400

    zone = national.get(ANALYSIS, {"level": "geopoliticalZone", "code": "NW"}).json()
    assert zone["by"] == "state" and zone["exco"]["here"] == 1
    assert {part["code"] for part in zone["parts"]} >= {"KD", "KN"}
    own = national.get(EXCO, {"level": "geopoliticalZone", "code": "NW", "at": "geopoliticalZone"})
    assert [p["role_name"] for p in own.json()["results"]] == ["Zonal Secretary"]
    # From the top: the zone's own exco before the states'.
    everyone = national.get(EXCO, {"level": "geopoliticalZone", "code": "NW"}).json()["results"]
    assert [p["area"]["level"] for p in everyone][:3] == ["geopoliticalZone", "state", "state"]
    states = national.get(EXCO, {"level": "geopoliticalZone", "code": "NW", "at": "state"}).json()
    assert [p["area_name"] for p in states["results"]] == ["Kaduna", "Kano"]

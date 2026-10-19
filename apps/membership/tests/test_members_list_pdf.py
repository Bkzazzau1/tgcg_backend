"""Members and exco of an area as one printable list (GET /members/list-pdf/)."""

import dataclasses

import pytest
from django.utils import timezone

from apps.access.models import AuditEvent
from apps.access.tests.test_access import appoint, client_for, custom_role, member_account
from apps.accounts.models import User
from apps.core.choices import GeographyLevel, Role
from apps.core.permissions import Capability
from apps.geography.models import Lga, PollingUnit, Ward
from apps.geography.scope import ScopeRef
from apps.membership import members_list_pdf
from conftest import PASSWORD

pytestmark = pytest.mark.django_db
C = Capability
LIST_PDF = "/api/v1/members/list-pdf/"


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


def at_unit(member, unit):
    member.scope_level = GeographyLevel.POLLING_UNIT
    member.polling_unit = unit
    member.full_clean()
    member.save()
    return member


@pytest.fixture
def kaduna(admin, unit):
    """Kaduna: a state secretary and an LGA women leader registered in Kaduna
    North, a unit coordinator and a plain member at Unit 1, a plain member at
    the LGA; and a Kano state officer outside it."""
    secretary = member_account("08033330001", name="Aisha Bello")
    leader = member_account("08033330002", name="Hauwa Musa")
    coordinator = at_unit(member_account("08033330003", name="Sani Garba"), unit)
    at_unit(member_account("08033330004", name="Zainab Umar", verified=False), unit)
    member_account("08033330005", name="Bala Yusuf", verified=False)
    kano = member_account("08033330006", name="Kano Officer", lga="KN-AJINGI")

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


def rows(listing):
    return [(place.title, [(row.name, row.exco) for row in place.rows]) for place in listing.places]


def test_everyone_appears_once_by_place_with_exco_first(kaduna):
    listing = members_list_pdf.collect(ScopeRef.resolve("state", "KD"))
    assert listing.title == "Kaduna State"
    assert (listing.people, listing.exco) == (5, 3)
    assert rows(listing) == [
        ("Kaduna State", [("Aisha Bello", True)]),
        ("Kaduna North LGA", [("Hauwa Musa", True), ("Bala Yusuf", False)]),
        ("Ward 01 ward", [("Sani Garba", True), ("Zainab Umar", False)]),
    ]
    # Each table repeats its place on every page it runs onto.
    assert listing.places[2].path == "Ward 01 ward, Kaduna North LGA, Kaduna State"
    by_name = {row.name: row for place in listing.places for row in place.rows}
    assert by_name["Aisha Bello"].posts == "State Secretary, Kaduna"
    assert by_name["Sani Garba"].posts == "PU Coordinator, Unit 1"
    assert by_name["Zainab Umar"].unit == "KD-KN-W01-PU001 Unit 1"
    assert by_name["Bala Yusuf"].unit == "" and by_name["Bala Yusuf"].posts == ""
    assert listing.parts_label == "LGA"
    assert listing.parts == [("Kaduna North", 4, 2)]
    # Full name, membership number, polling unit and posts: never a phone number.
    assert {f.name for f in dataclasses.fields(members_list_pdf.Row)} == {
        "name",
        "number",
        "unit",
        "posts",
        "exco",
    }


def test_a_post_elsewhere_is_shown_but_not_counted_as_exco_here(kaduna):
    listing = members_list_pdf.collect(ScopeRef.resolve("lga", "KD-KADUNA-NORTH"))
    lga = listing.places[1]
    assert lga.title == "Kaduna North LGA"
    secretary = next(row for row in lga.rows if row.name == "Aisha Bello")
    assert secretary.exco is False and secretary.posts == "State Secretary, Kaduna"
    assert [row.name for row in lga.rows] == ["Hauwa Musa", "Aisha Bello", "Bala Yusuf"]
    assert (listing.people, listing.exco) == (5, 2)


def test_the_pdf_downloads_for_an_area_and_each_download_is_recorded(admin, kaduna):
    national = client_for(admin)
    response = national.get(LIST_PDF, {"level": "state", "code": "KD"})
    assert response.status_code == 200, response.content
    assert response["Content-Type"] == "application/pdf"
    assert response.content.startswith(b"%PDF")
    day = timezone.localdate().isoformat()
    assert response["Content-Disposition"] == (
        f'attachment; filename="tgcg-members-and-exco-kaduna-state-{day}.pdf"'
    )
    assert response["Cache-Control"] == "no-store"
    event = AuditEvent.objects.get(action="members.list_downloaded")
    assert (event.target_id, event.target_label) == ("KD", "Kaduna State")
    assert event.changes == {"people": 5, "exco": 3}
    assert event.actor == admin

    everyone = national.get(LIST_PDF)
    assert everyone.status_code == 200 and everyone.content.startswith(b"%PDF")
    nigeria = AuditEvent.objects.filter(action="members.list_downloaded").latest("id")
    assert nigeria.target_id == "NG" and nigeria.changes == {"people": 6, "exco": 4}


def test_coordinators_download_only_their_own_area(kaduna, lga_coordinator):
    coordinator = client_for(lga_coordinator)
    own = coordinator.get(LIST_PDF, {"level": "lga", "code": "KD-KADUNA-NORTH"})
    assert own.status_code == 200 and own.content.startswith(b"%PDF")
    assert coordinator.get(LIST_PDF, {"level": "state", "code": "KD"}).status_code == 403
    assert coordinator.get(LIST_PDF).status_code == 403
    plain = client_for(member_account("08033330009", name="Musa Plain").user)
    assert plain.get(LIST_PDF).status_code == 403
    assert not AuditEvent.objects.filter(
        action="members.list_downloaded", actor=plain.handler._force_user
    ).exists()


def test_an_empty_area_still_prints(admin, unit):
    response = client_for(admin).get(LIST_PDF, {"level": "ward", "code": "KD-KADUNA-NORTH-W01"})
    assert response.status_code == 200 and response.content.startswith(b"%PDF")


def test_nigeria_opens_with_national_and_zonal_posts(admin, kaduna):
    zonal = member_account("08033330007", name="Zainab Zonal")
    role = custom_role(admin, "Zonal Secretary", [C.VIEW_GEOGRAPHY], levels=("geopoliticalZone",))
    assert (
        appoint(admin, zonal, role, [{"level": "geopoliticalZone", "zoneId": "NW"}]).status_code
        == 201
    )
    listing = members_list_pdf.collect(ScopeRef.national())
    first = listing.places[0]
    assert first.title == "Nigeria"
    assert [(row.name, row.posts) for row in first.rows] == [
        ("Zainab Zonal", "Zonal Secretary, North West")
    ]
    assert [place.title for place in listing.places[1:3]] == ["Kaduna State", "Kaduna North LGA"]
    assert listing.parts_label == "State"
    assert ("Kano", 1, 1) in listing.parts

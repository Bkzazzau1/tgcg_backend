"""A group's analytics and finding its people (4 November 2026), for those
who vet groups. Every name and number here is made up."""

import pytest

from apps.access.tests.test_access import client_for, member_account
from apps.support_groups import group_list
from apps.support_groups.group_list import Person
from apps.support_groups.places import Places
from apps.support_groups.tests.conftest import GROUPS

pytestmark = pytest.mark.django_db

VIN_A = "90F5B0E8D0295847263"
VIN_B = "90F5B0E8D0295847264"


@pytest.fixture
def listed(admin, group):
    """The group's leader, two people from its list (one with no phone), and
    a member already registered, placed from the list too."""
    member_account("08035550002", name="Grace Obi")
    for person in (
        Person("Musa Bello", vin=VIN_A, phone="+2348031234567", state="Kaduna"),
        Person("Aisha Sani", vin=VIN_B, state="Kaduna"),
        Person("Grace Obi", phone="+2348035550002"),
    ):
        group_list.place_person(
            actor=admin, group=group, person=person, places=Places(), source="a list"
        )
    return group


def test_those_who_vet_groups_see_a_groups_analytics(admin, listed):
    figures = client_for(admin).get(f"{GROUPS}{listed.uuid}/analysis/")
    assert figures.status_code == 200, figures.content
    body = figures.json()
    assert body["members"]["total"] == 4
    assert body["members"]["channel"]["import"] == 2
    assert body["via"] == {"leader": 1, "list": 3}
    assert body["waiting"] == 0
    assert body["sign_in"] == {"phone": 3, "vin_only": 1, "pin_to_choose": 1}
    kaduna = next(row for row in body["states"] if row["code"] == "KD")
    assert kaduna["name"] and kaduna["count"] >= 2
    assert sum(row["count"] for row in body["states"]) == 4


def test_a_groups_analytics_are_for_those_who_vet_groups_only(listed):
    leader = client_for(listed.leader.user)
    assert leader.get(f"{GROUPS}{listed.uuid}/analysis/").status_code == 403


@pytest.mark.parametrize(
    ("query", "found"),
    [
        ("musa", ["Musa Bello"]),
        ("bello musa", ["Musa Bello"]),
        ("0803 123 4567", ["Musa Bello"]),
        ("90f5 b0e8 d029 5847 264", ["Aisha Sani"]),
        ("nobody here", []),
    ],
)
def test_those_who_vet_groups_find_its_people(admin, listed, query, found):
    response = client_for(admin).get(f"{GROUPS}{listed.uuid}/members/", {"q": query})
    assert response.status_code == 200, response.content
    assert [row["full_name"] for row in response.json()["results"]] == found


def test_a_membership_number_finds_its_member(admin, listed):
    rows = client_for(admin).get(f"{GROUPS}{listed.uuid}/members/").json()["results"]
    number = next(row["membership_number"] for row in rows if row["full_name"] == "Grace Obi")
    found = client_for(admin).get(f"{GROUPS}{listed.uuid}/members/", {"q": number.lower()})
    assert [row["full_name"] for row in found.json()["results"]] == ["Grace Obi"]

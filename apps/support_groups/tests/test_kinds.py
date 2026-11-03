"""Types of support group (3 November 2026): Support Groups, Women Groups,
Youth Groups and People With Disability (PWD) Groups to start with; those
who vet groups add more, rename them, stop offering one, and move a group."""

import pytest
from rest_framework.test import APIClient

from apps.access.models import AuditEvent
from apps.access.tests.test_access import client_for, member_account
from apps.support_groups.models import GroupKind, SupportGroup
from apps.support_groups.tests.conftest import GROUPS, register

pytestmark = pytest.mark.django_db
KINDS = f"{GROUPS}kinds/"
OFFERED = "/api/v1/register/support-group-kinds/"


def test_the_four_types_are_offered_in_order():
    assert APIClient().get(OFFERED).json() == [
        {"code": "support", "name": "Support Groups"},
        {"code": "women", "name": "Women Groups"},
        {"code": "youth", "name": "Youth Groups"},
        {"code": "pwd", "name": "People With Disability (PWD) Groups"},
    ]


def test_a_group_registers_under_the_type_it_chooses(leader):
    response = register(leader.user, organization_kind="women")
    assert response.status_code == 201, response.content
    assert (response.json()["kind"], response.json()["kind_name"]) == ("women", "Women Groups")
    missing = register(leader.user, organization_kind="")
    assert missing.status_code == 400 and "organization_kind" in missing.json()
    unknown = register(leader.user, organization_kind="traders")
    assert unknown.status_code == 400 and "organization_kind" in unknown.json()
    assert SupportGroup.objects.count() == 1


def test_the_administrator_adds_renames_and_stops_offering_types(admin, leader):
    client = client_for(admin)
    added = client.post(KINDS, {"name": "  Traders   Groups "}, format="json")
    assert added.status_code == 201, added.content
    assert added.json() == {
        "code": "traders-groups",
        "name": "Traders Groups",
        "position": 5,
        "offered": True,
    }
    assert client.post(KINDS, {"name": "traders groups"}, format="json").status_code == 400
    assert "traders-groups" in [kind["code"] for kind in APIClient().get(OFFERED).json()]

    assert register(leader.user, organization_kind="traders-groups").status_code == 201
    renamed = client.patch(
        f"{KINDS}traders-groups/", {"name": "Market Women and Traders"}, format="json"
    )
    assert renamed.json()["name"] == "Market Women and Traders"
    closed = client.patch(f"{KINDS}traders-groups/", {"offered": False}, format="json")
    assert closed.status_code == 200 and closed.json()["offered"] is False
    # Groups of it keep their type; nobody new chooses it.
    assert "traders-groups" not in [kind["code"] for kind in APIClient().get(OFFERED).json()]
    assert SupportGroup.objects.get().kind.name == "Market Women and Traders"
    assert register(leader.user, organization_kind="traders-groups").status_code == 400
    assert [kind["code"] for kind in client.get(KINDS).json()][-1] == "traders-groups"
    assert AuditEvent.objects.filter(action__startswith="supportGroupKind.").count() == 3


def test_only_those_who_vet_groups_manage_types(leader):
    client = client_for(leader.user)
    assert client.get(KINDS).status_code == 403
    assert client.post(KINDS, {"name": "Traders Groups"}, format="json").status_code == 403
    assert client.patch(f"{KINDS}women/", {"offered": False}, format="json").status_code == 403
    assert GroupKind.objects.count() == 4


def test_one_type_stays_offered(admin):
    client = client_for(admin)
    for code in ("support", "women", "youth"):
        assert client.patch(f"{KINDS}{code}/", {"offered": False}, format="json").status_code == 200
    last = client.patch(f"{KINDS}pwd/", {"offered": False}, format="json")
    assert last.status_code == 400 and "offered" in last.json()


def test_a_group_moves_to_another_type_and_lists_by_type(admin, group):
    client = client_for(admin)
    other = member_account("08035550002", name="Aisha Bello")
    women = register(other.user, organization_kind="women", organization_name="Women for Tomorrow")
    assert women.status_code == 201

    youth = client.get(GROUPS, {"kind": "youth"}).json()["results"]
    assert [row["name"] for row in youth] == ["Arewa Youth for Progress"]
    totals = {row["code"]: row for row in client.get(f"{GROUPS}totals/").json()["kinds"]}
    assert totals["youth"]["groups"]["approved"] == 1 and totals["youth"]["people"] == 1
    assert totals["women"]["groups"]["pending"] == 1 and totals["women"]["people"] == 0
    assert totals["pwd"]["groups"] == {"pending": 0, "approved": 0, "rejected": 0, "suspended": 0}

    moved = client.post(f"{GROUPS}{group.uuid}/kind/", {"kind": "pwd"}, format="json")
    assert moved.status_code == 200, moved.content
    assert moved.json()["kind_name"] == "People With Disability (PWD) Groups"
    assert client.get(GROUPS, {"kind": "youth"}).json()["results"] == []
    event = AuditEvent.objects.get(action="supportGroup.kindChanged")
    assert event.changes == {"from": "Youth Groups", "to": "People With Disability (PWD) Groups"}
    wrong = client.post(f"{GROUPS}{group.uuid}/kind/", {"kind": "nothing"}, format="json")
    assert wrong.status_code == 400
    assert (
        client_for(group.leader.user)
        .post(f"{GROUPS}{group.uuid}/kind/", {"kind": "women"}, format="json")
        .status_code
        == 403
    )

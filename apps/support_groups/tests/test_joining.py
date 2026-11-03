"""Registering an organization on the joining page, and joining TGCG with a
support group's link."""

import pytest
from rest_framework.test import APIClient

from apps.access.tests.test_access import client_for, member_account
from apps.accounts.models import User
from apps.core.choices import Role
from apps.geography.polling_units import load_polling_units
from apps.membership.models import Member
from apps.support_groups.models import GroupMember, SupportGroup
from conftest import PASSWORD

# Joining runs outside the request transaction, as in the registration tests.
pytestmark = pytest.mark.django_db(transaction=True, serialized_rollback=True)

VIN = "90F5B0E8D0295847263"


@pytest.fixture(autouse=True)
def setup(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    settings.REGISTRATION_PHONE_CODE_REQUIRED = False
    load_polling_units(lga_codes=["KD-ZARIA"])


def join(**overrides):
    data = {
        "phone_number": "08031234567",
        "vin": VIN,
        "delim": "18/23/01/001",
        "full_name": "Halima Sani",
        "pin": "482915",
        "consent": True,
        **overrides,
    }
    return APIClient().post("/api/v1/register/", data, format="multipart")


def test_an_organization_registers_with_its_leader():
    response = join(
        organization_name="Arewa Youth for Progress",
        organization_kind="youth",
        organization_coverage="regional",
        organization_zone="NW",
        organization_officials='[{"name": "Bala Musa", "position": "Secretary", '
        '"phone": "08035550009"}]',
    )
    assert response.status_code == 201, response.content
    leader = Member.objects.get()
    group = SupportGroup.objects.get()
    assert group.leader == leader and group.status == SupportGroup.Status.PENDING
    assert group.kind.name == "Youth Groups"
    assert group.zone.code == "NW" and group.registered_by == leader.user
    assert group.officials[0]["name"] == "Bala Musa"
    assert GroupMember.objects.get().via == GroupMember.Via.LEADER


def test_nobody_joins_when_the_organization_is_incomplete():
    response = join(organization_name="Arewa Youth", organization_coverage="state")
    assert response.status_code == 400
    assert {"organization_state", "organization_kind"} <= set(response.json())
    assert not Member.objects.exists() and not SupportGroup.objects.exists()


def test_joining_with_a_groups_link():
    admin = User.objects.create_user(
        "NA-1", PASSWORD, full_name="National Admin", role=Role.NATIONAL_ADMINISTRATOR
    )
    leader = member_account("08035550001", name="Halima Sani")
    created = client_for(leader.user).post(
        "/api/v1/me/support-groups/register/",
        {
            "organization_name": "Arewa Youth for Progress",
            "organization_kind": "youth",
            "organization_coverage": "national",
        },
        format="json",
    )
    group = SupportGroup.objects.get(uuid=created.json()["id"])
    client_for(admin).post(f"/api/v1/support-groups/{group.uuid}/approve/")
    group.refresh_from_db()

    named = APIClient().get("/api/v1/register/support-group/", {"code": group.join_code})
    assert named.json() == {"name": "Arewa Youth for Progress"}
    assert APIClient().get("/api/v1/register/support-group/", {"code": "1"}).status_code == 400

    assert join(group_code="9999999").status_code == 400
    response = join(group_code=group.join_code)
    assert response.status_code == 201, response.content
    joined = Member.objects.get(phone_number="+2348031234567")
    place = GroupMember.objects.get(member=joined)
    assert place.group == group and place.via == GroupMember.Via.LINK
    assert place.status == GroupMember.Status.ACTIVE

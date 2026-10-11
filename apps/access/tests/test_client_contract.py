"""The exact requests the Flutter client builds are accepted, and sessions end
when access does. (Shapes copied from lib/tgcg/api/*_repository.dart.)"""

import pytest
from rest_framework.test import APIClient

from apps.access.models import DelegationGrant, RoleAssignment
from apps.access.tests.test_access import client_for, member_account
from apps.accounts.models import User
from apps.core.choices import Role
from conftest import PASSWORD

pytestmark = pytest.mark.django_db

# What GeographicScope.toJson sends (geographicScopeToJson, nulls removed).
KADUNA_FROM_CLIENT = {
    "level": "state",
    "country": "Nigeria",
    "zoneId": "NW",
    "zoneName": "North West",
    "stateId": "KD",
    "stateName": "Kaduna",
}
LGA_FROM_CLIENT = {
    "level": "lga",
    "country": "Nigeria",
    "zoneId": "NW",
    "zoneName": "North West",
    "stateId": "KD",
    "stateName": "Kaduna",
    "senatorialDistrictId": "SD/033/KD",
    "senatorialDistrictName": "Kaduna Central",
    "lgaId": "KD-KADUNA-NORTH",
    "lgaName": "Kaduna North",
}


def test_the_clients_role_appointment_and_delegation_requests_are_accepted(admin):
    client = client_for(admin)
    # RoleDraft.toJson
    role = client.post(
        "/api/v1/access/roles/",
        {
            "name": "Kaduna Media Officer",
            "description": "Announcements for Kaduna.",
            "category": "communications",
            "permitted_levels": ["state", "lga"],
            "permissions": ["sendBroadcast", "viewCommunications"],
            "supervising_role": None,
        },
        format="json",
    )
    assert role.status_code == 201, role.content
    code = role.json()["code"]
    # The list as RoleRepository.roles asks for it.
    listed = client.get(
        "/api/v1/access/roles/",
        {"search": "Media", "category": "communications", "status": "active", "ordering": "name"},
    )
    assert [item["code"] for item in listed.json()["results"]] == [code]

    # AccessRepository.appoint
    member = member_account("08034440001", name="Ada Obi")
    appointed = client.post(
        "/api/v1/access/assignments/",
        {
            "member": member.membership_number.lower(),
            "role": code,
            "areas": [KADUNA_FROM_CLIENT, LGA_FROM_CLIENT],
            "restricted_permissions": None,
            "starts_at": None,
            "ends_at": None,
            "reason": "",
        },
        format="json",
    )
    # Membership numbers are sent upper-cased by the client.
    assert appointed.status_code == 404
    appointed = client.post(
        "/api/v1/access/assignments/",
        {
            "member": member.membership_number,
            "role": code,
            "areas": [KADUNA_FROM_CLIENT],
            "restricted_permissions": ["sendBroadcast"],
            "starts_at": None,
            "ends_at": None,
            "reason": "Election season",
        },
        format="json",
    )
    assert appointed.status_code == 201, appointed.content
    assignment = appointed.json()["id"]
    restricted = client.post(
        f"/api/v1/access/assignments/{assignment}/restrict/", {"permissions": None}, format="json"
    )
    assert restricted.status_code == 200 and restricted.json()["restricted_permissions"] is None
    filtered = client.get(
        "/api/v1/access/assignments/",
        {
            "status": "active",
            "role__code": code,
            "member__membership_number": member.membership_number,
        },
    )
    assert filtered.json()["count"] == 1

    # DelegationDraft.toJson
    coordinator = member_account("08034440002", name="Kaduna Coordinator")
    delegated = client.post(
        "/api/v1/access/delegations/",
        {
            "recipient": coordinator.membership_number,
            "permissions": ["assignRoles", "sendBroadcast"],
            "categories": ["communications"],
            "levels": ["lga"],
            "areas": [KADUNA_FROM_CLIENT],
            "can_redelegate": False,
            "depth": 0,
            "starts_at": None,
            "ends_at": None,
            "reason": "",
        },
        format="json",
    )
    assert delegated.status_code == 201, delegated.content
    assert DelegationGrant.objects.get(uuid=delegated.json()["id"]).levels == ["lga"]
    assert client.get("/api/v1/access/delegations/", {"status": "active"}).json()["count"] == 1
    assert RoleAssignment.objects.count() == 1


def test_the_clients_administrator_requests_are_accepted(admin):
    User.objects.create_user("NA-2", PASSWORD, full_name="Second", role=Role.NATIONAL_ADMINISTRATOR)
    User.objects.create_user("NA-3", PASSWORD, full_name="Third", role=Role.NATIONAL_ADMINISTRATOR)
    client = client_for(admin)
    # AccessRepository.requestAdminAction upper-cases the access ID.
    made = client.post(
        "/api/v1/access/admin-actions/",
        {"target": "NA-3", "kind": "suspend", "reason": "Misuse"},
        format="json",
    )
    assert made.status_code == 201, made.content
    assert client.get("/api/v1/access/admin-actions/", {"status": "pending"}).json()["count"] == 1
    assert client.get("/api/v1/access/audit/", {"action__startswith": "administrator."}).json()[
        "count"
    ]


def test_access_tokens_stop_working_when_access_does(admin):
    """Real sign-in, real tokens: deactivation ends the session at once."""
    member = member_account("08034440003", name="Signed In")
    member.user.set_password(PASSWORD)
    member.user.save()
    anonymous = APIClient()
    signed = anonymous.post(
        "/api/v1/auth/login/",
        {"username": member.user.access_id, "password": PASSWORD},
        format="json",
    )
    assert signed.status_code == 200, signed.content
    tokens = signed.json()
    session = APIClient()
    session.credentials(HTTP_AUTHORIZATION=f"Bearer {tokens['access']}")
    assert session.get("/api/v1/auth/me/").status_code == 200

    deactivate_role = client_for(admin)
    ended = deactivate_role.post(
        f"/api/v1/members/{member.membership_number}/deactivate/",
        {"reason": "Left the party"},
        format="json",
    )
    assert ended.status_code == 200, ended.content
    assert session.get("/api/v1/auth/me/").status_code == 401  # the access token
    refreshed = anonymous.post(
        "/api/v1/auth/refresh/", {"refresh": tokens["refresh"]}, format="json"
    )
    assert refreshed.status_code == 401  # and the refresh token

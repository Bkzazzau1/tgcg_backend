import pytest
from django.core.exceptions import ValidationError

from apps.accounts.phone import normalize_ng_phone
from apps.core.choices import GeographyLevel, Role
from apps.geography.models import State
from conftest import PASSWORD

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize(
    "raw",
    ["08031234567", "8031234567", "2348031234567", "+234 803 123 4567", "+234-803-123-4567"],
)
def test_normalize_ng_phone(raw):
    assert normalize_ng_phone(raw) == "+2348031234567"


@pytest.mark.parametrize("raw", ["", None, "0803123", "05031234567", "+44 7700 900123"])
def test_normalize_ng_phone_rejects_invalid(raw):
    assert normalize_ng_phone(raw) is None


def test_lga_scope_derives_parent_units(lga_coordinator):
    user = lga_coordinator
    assert user.access_id == "TGCG-KD-0001"
    assert user.phone_number == "+2348031234567"
    assert user.state.code == "KD"
    assert user.zone.code == "NW"
    assert user.senatorial_district is not None
    assert user.ward is None


def test_role_requires_matching_scope(make_user):
    with pytest.raises(ValidationError) as error:
        make_user("TGCG-X-1", role=Role.STATE_COORDINATOR)
    assert "scope_level" in error.value.message_dict


def test_scope_level_requires_its_unit(make_user):
    with pytest.raises(ValidationError) as error:
        make_user("TGCG-X-2", scope_level=GeographyLevel.STATE)
    assert "state" in error.value.message_dict


def test_state_scope_clears_deeper_units(make_user, lga_coordinator):
    user = make_user(
        "TGCG-X-3",
        role=Role.STATE_COORDINATOR,
        scope_level=GeographyLevel.STATE,
        state=State.objects.get(code="LA"),
    )
    assert user.lga is None and user.senatorial_district is None
    assert user.zone.code == "SW"


@pytest.mark.parametrize("identifier", ["TGCG-KD-0001", "tgcg-kd-0001", "0803 123 4567"])
def test_login_with_access_id_or_phone(api_client, lga_coordinator, identifier):
    response = api_client.post(
        "/api/v1/auth/login/", {"username": identifier, "password": PASSWORD}, format="json"
    )
    assert response.status_code == 200, response.content
    body = response.json()
    assert body["access"] and body["refresh"]
    assert body["user"]["role"] == "lgaCoordinator"
    assert body["user"]["scope"] == {
        "level": "lga",
        "country": "Nigeria",
        "zoneId": "NW",
        "zoneName": "North West",
        "stateId": "KD",
        "stateName": "Kaduna",
        "senatorialDistrictId": lga_coordinator.senatorial_district.code,
        "senatorialDistrictName": lga_coordinator.senatorial_district.name,
        "lgaId": "KD-KADUNA-NORTH",
        "lgaName": "Kaduna North",
        "wardId": None,
        "wardName": None,
        "pollingUnitId": None,
        "pollingUnitName": None,
    }


def test_login_rejects_wrong_password(api_client, lga_coordinator):
    response = api_client.post(
        "/api/v1/auth/login/", {"username": "TGCG-KD-0001", "password": "wrong"}, format="json"
    )
    assert response.status_code == 401


def test_login_rejects_inactive_user(api_client, lga_coordinator):
    lga_coordinator.is_active = False
    lga_coordinator.save()
    response = api_client.post(
        "/api/v1/auth/login/", {"username": "TGCG-KD-0001", "password": PASSWORD}, format="json"
    )
    assert response.status_code == 401


def test_me_and_logout(api_client, lga_coordinator):
    tokens = api_client.post(
        "/api/v1/auth/login/", {"username": "TGCG-KD-0001", "password": PASSWORD}, format="json"
    ).json()
    api_client.credentials(HTTP_AUTHORIZATION=f"Bearer {tokens['access']}")

    me = api_client.get("/api/v1/auth/me/")
    assert me.status_code == 200
    assert me.json()["full_name"] == "Amina Kaduna"

    assert (
        api_client.post(
            "/api/v1/auth/logout/", {"refresh": tokens["refresh"]}, format="json"
        ).status_code
        == 205
    )
    refreshed = api_client.post(
        "/api/v1/auth/refresh/", {"refresh": tokens["refresh"]}, format="json"
    )
    assert refreshed.status_code == 401

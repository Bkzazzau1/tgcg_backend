import pytest
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.core.choices import GeographyLevel, Role
from apps.geography.models import Lga, PollingUnit, State, Ward
from apps.membership.models import AccreditationEvent
from conftest import PASSWORD, make_member

pytestmark = pytest.mark.django_db


@pytest.fixture
def kd_polling_units(db):
    ward = Ward.objects.create(
        code="KD-KADUNA-NORTH-W01",
        name="Ward 01",
        lga=Lga.objects.get(code="KD-KADUNA-NORTH"),
    )
    return [
        PollingUnit.objects.create(code=f"KD-KN-W01-PU00{i}", name=f"Unit {i}", ward=ward)
        for i in (1, 2)
    ]


@pytest.fixture
def la_polling_unit(db):
    ward = Ward.objects.create(
        code="LA-IKEJA-W03", name="Ward 03", lga=Lga.objects.get(code="LA-IKEJA")
    )
    return PollingUnit.objects.create(code="LA-IK-W03-PU012", name="Unit 12", ward=ward)


def client_for(**user_fields):
    user = User.objects.create_user(password=PASSWORD, **user_fields)
    client = APIClient()
    client.force_authenticate(user)
    return client


@pytest.fixture
def admin(db):
    return client_for(
        access_id="TGCG-NA-1", full_name="National Admin", role=Role.NATIONAL_ADMINISTRATOR
    )


@pytest.fixture
def kd_state(db):
    return client_for(
        access_id="TGCG-KD-SC",
        full_name="Kaduna Coordinator",
        role=Role.STATE_COORDINATOR,
        scope_level=GeographyLevel.STATE,
        state=State.objects.get(code="KD"),
    )


@pytest.fixture
def la_state(db):
    return client_for(
        access_id="TGCG-LA-SC",
        full_name="Lagos Coordinator",
        role=Role.STATE_COORDINATOR,
        scope_level=GeographyLevel.STATE,
        state=State.objects.get(code="LA"),
    )


@pytest.fixture
def kd_lga(db):
    return client_for(
        access_id="TGCG-KD-LC",
        full_name="Kaduna North Coordinator",
        role=Role.LGA_COORDINATOR,
        scope_level=GeographyLevel.LGA,
        lga=Lga.objects.get(code="KD-KADUNA-NORTH"),
    )


def new_member(phone="08031112222", lga="KD-KADUNA-NORTH"):
    """Create a member directly; registration itself is tested in test_registration."""
    return make_member(phone=phone, lga=lga).membership_number


def pu_scope(pu):
    return {"level": "pollingUnit", "pollingUnitId": pu.code}


def accredit(client, member_number, pu, **extra):
    return client.post(
        "/api/v1/agents/",
        {
            "member": member_number,
            "role": "pollingUnitAgent",
            "scope": pu_scope(pu),
            **extra,
        },
        format="json",
    )


# --- members --------------------------------------------------------------


def test_members_are_limited_to_scope(admin, la_state):
    number = new_member()
    assert la_state.get("/api/v1/members/").json()["count"] == 0
    assert la_state.get(f"/api/v1/members/{number}/").status_code == 404


def test_observer_cannot_see_members(db):
    observer = client_for(access_id="TGCG-OB-1", full_name="Observer", role=Role.OBSERVER)
    assert observer.get("/api/v1/members/").status_code == 403


def test_update_member_status(admin):
    number = new_member()
    response = admin.patch(f"/api/v1/members/{number}/", {"status": "verified"}, format="json")
    assert response.status_code == 200
    assert response.json()["status"] == "verified"


# --- accreditation --------------------------------------------------------


def test_state_coordinator_accredits_agent(admin, kd_state, kd_polling_units):
    number = new_member()
    response = accredit(kd_state, number, kd_polling_units[0], device_id="DEV-1")
    assert response.status_code == 201, response.content
    body = response.json()
    assert body["agent_id"] == "AG-00001"
    assert body["status"] == "pending"
    assert body["scope"]["wardId"] == "KD-KADUNA-NORTH-W01"
    assert body["scope"]["lgaId"] == "KD-KADUNA-NORTH"
    assert body["is_ready"] is False
    assert AccreditationEvent.objects.filter(to_status="pending").count() == 1

    member = kd_state.get(f"/api/v1/members/{number}/").json()
    assert member["active_accreditation"] == {
        "agent_id": "AG-00001",
        "role": "pollingUnitAgent",
        "status": "pending",
    }


def test_role_must_match_scope_level(admin, kd_state):
    number = new_member()
    response = kd_state.post(
        "/api/v1/agents/",
        {
            "member": number,
            "role": "pollingUnitAgent",
            "scope": {"level": "lga", "lgaId": "KD-KADUNA-NORTH"},
        },
        format="json",
    )
    assert response.status_code == 400
    assert "scope" in response.json()


def test_national_roles_cannot_be_accredited(admin, kd_polling_units):
    number = new_member()
    response = admin.post(
        "/api/v1/agents/",
        {"member": number, "role": "nationalAdministrator", "scope": {"level": "country"}},
        format="json",
    )
    assert response.status_code == 400
    assert "role" in response.json()


def test_cannot_accredit_member_outside_scope(admin, la_state, la_polling_unit):
    number = new_member()
    response = accredit(la_state, number, la_polling_unit)
    assert response.status_code == 400
    assert "member" in response.json()


def test_cannot_assign_outside_scope(admin, kd_state, la_polling_unit):
    number = new_member()
    assert accredit(kd_state, number, la_polling_unit).status_code == 403


def test_one_active_accreditation_per_member(admin, kd_state, kd_polling_units):
    number = new_member()
    accredit(kd_state, number, kd_polling_units[0])
    response = accredit(kd_state, number, kd_polling_units[1])
    assert response.status_code == 400
    assert "member" in response.json()


def test_lga_coordinator_updates_readiness_but_cannot_approve(
    admin, kd_state, kd_lga, kd_polling_units
):
    number = new_member()
    agent_id = accredit(kd_state, number, kd_polling_units[0]).json()["agent_id"]

    ready = kd_lga.patch(
        f"/api/v1/agents/{agent_id}/readiness/",
        {"training_completed": True, "biometric_enrolled": True},
        format="json",
    )
    assert ready.status_code == 200
    assert ready.json()["is_ready"] is True

    approve = kd_lga.post(f"/api/v1/agents/{agent_id}/status/", {"status": "approved"})
    assert approve.status_code == 403


def test_approval_requires_readiness(admin, kd_state, kd_polling_units):
    number = new_member()
    agent_id = accredit(kd_state, number, kd_polling_units[0]).json()["agent_id"]

    early = kd_state.post(f"/api/v1/agents/{agent_id}/status/", {"status": "approved"})
    assert early.status_code == 400

    kd_state.patch(
        f"/api/v1/agents/{agent_id}/readiness/",
        {"training_completed": True, "biometric_enrolled": True},
        format="json",
    )
    approved = kd_state.post(f"/api/v1/agents/{agent_id}/status/", {"status": "approved"})
    assert approved.status_code == 200
    assert approved.json()["status"] == "approved"


def ready_agent(admin, coordinator, pu, phone):
    number = new_member(phone=phone)
    agent_id = accredit(coordinator, number, pu).json()["agent_id"]
    coordinator.patch(
        f"/api/v1/agents/{agent_id}/readiness/",
        {"training_completed": True, "biometric_enrolled": True},
        format="json",
    )
    return agent_id


def test_status_lifecycle_is_audited(admin, kd_state, kd_polling_units):
    agent_id = ready_agent(admin, kd_state, kd_polling_units[0], "08031112222")
    url = f"/api/v1/agents/{agent_id}/status/"

    assert kd_state.post(url, {"status": "approved"}).status_code == 200
    assert kd_state.post(url, {"status": "suspended"}).status_code == 400  # needs reason
    assert (
        kd_state.post(url, {"status": "suspended", "reason": "Missed briefing"}).status_code == 200
    )
    assert kd_state.post(url, {"status": "revoked", "reason": "Misconduct"}).status_code == 200
    assert kd_state.post(url, {"status": "approved"}).status_code == 400  # revoked is final

    events = kd_state.get(f"/api/v1/agents/{agent_id}/events/").json()
    assert [(e["from_status"], e["to_status"]) for e in events] == [
        ("", "pending"),
        ("pending", "approved"),
        ("approved", "suspended"),
        ("suspended", "revoked"),
    ]
    assert events[2]["reason"] == "Missed briefing"
    assert events[2]["actor"] == "TGCG-KD-SC"


def test_one_approved_agent_per_polling_unit(admin, kd_state, kd_polling_units):
    pu = kd_polling_units[0]
    first = ready_agent(admin, kd_state, pu, "08031112222")
    second = ready_agent(admin, kd_state, pu, "08031112223")

    assert (
        kd_state.post(f"/api/v1/agents/{first}/status/", {"status": "approved"}).status_code == 200
    )
    response = kd_state.post(f"/api/v1/agents/{second}/status/", {"status": "approved"})
    assert response.status_code == 400


def test_agents_are_limited_to_scope(admin, kd_state, la_state, kd_polling_units):
    number = new_member()
    agent_id = accredit(kd_state, number, kd_polling_units[0]).json()["agent_id"]
    assert la_state.get("/api/v1/agents/").json()["count"] == 0
    assert la_state.get(f"/api/v1/agents/{agent_id}/").status_code == 404
    assert kd_state.get("/api/v1/agents/", {"status": "pending"}).json()["count"] == 1


def test_me_lists_capabilities(kd_state):
    capabilities = kd_state.get("/api/v1/auth/me/").json()["capabilities"]
    assert "accreditAgents" in capabilities
    assert "manageMembership" not in capabilities

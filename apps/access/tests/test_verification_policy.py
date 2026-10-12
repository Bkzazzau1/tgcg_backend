"""Who needs a verified identity (docs/access-control.md, "Who must be verified").

Membership needs none: every member has the whole member app. Agents,
observers and appointed roles act only for a member whose identity a
coordinator verified.
"""

from datetime import timedelta

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.core.choices import GeographyLevel, Role, VerificationStatus
from apps.geography.models import PollingUnit, Ward
from apps.geography.polling_units import load_polling_units
from apps.membership.models import Member
from apps.membership.registration import ussd_register
from conftest import PASSWORD

pytestmark = pytest.mark.django_db

UNIT = "18-23-01-001"


@pytest.fixture(autouse=True)
def zaria():
    load_polling_units(lga_codes=["KD-ZARIA"])


def client_for(user):
    client = APIClient()
    client.force_authenticate(user)
    return client


def new_member(phone="08031112222", name="Musa Bello"):
    """A member as USSD registers one: no selfie, so not verified."""
    return ussd_register(
        phone_number=phone,
        session_id=f"s-{phone}",
        full_name=name,
        polling_unit=PollingUnit.objects.get(code=UNIT),
        gender="male",
        pin="482915",
    )


def as_agent(member):
    """The member's own account, given the polling-unit agent role at their unit."""
    User.objects.filter(pk=member.user_id).update(role=Role.POLLING_UNIT_AGENT)
    return User.objects.get(pk=member.user_id)


def verify(member):
    Member.objects.filter(pk=member.pk).update(verification_status=VerificationStatus.VERIFIED)


def gps():
    return {
        "latitude": "11.085500",
        "longitude": "7.719900",
        "accuracy_m": 12,
        "captured_at": (timezone.now() - timedelta(seconds=5)).isoformat(),
        "is_mocked": False,
    }


def test_members_need_no_verification():
    member = new_member()
    assert member.verification_status == VerificationStatus.PENDING_REVIEW
    me = client_for(member.user)

    profile = me.get("/api/v1/auth/me/").json()
    assert profile["role"] == "member" and profile["identity_verified"] is False
    assert me.get("/api/v1/me/member/").status_code == 200
    edit = me.patch("/api/v1/me/member/", {"email": "musa@example.com"}, format="json")
    assert edit.status_code == 200
    assert me.get("/api/v1/me/polling-unit/").status_code == 200
    assert me.get("/api/v1/me/announcements/").status_code == 200
    offer = me.post("/api/v1/me/volunteer/", {"interests": ["mobilisation"]}, format="json")
    assert offer.status_code == 201

    # Their card is valid at party events.
    coordinator = User.objects.create_user(
        "SC-KD",
        PASSWORD,
        full_name="State Coordinator",
        role=Role.STATE_COORDINATOR,
        scope_level=GeographyLevel.STATE,
        state=member.state,
    )
    token = me.get("/api/v1/me/member/").json()["qr_token"]
    card = client_for(coordinator).get("/api/v1/members/verify/", {"token": token}).json()
    assert card["valid"] is True and card["verification_status"] == "pendingReview"


def test_agents_work_only_once_verified():
    member = new_member()
    agent = as_agent(member)
    client = client_for(agent)

    profile = client.get("/api/v1/auth/me/").json()
    assert profile["capabilities"] == [] and profile["identity_verified"] is False
    refused = client.post("/api/v1/tracking/check-ins/", {"gps": gps()}, format="json")
    assert refused.status_code == 403
    assert "verified" in refused.json()["detail"]
    assert (
        client.post("/api/v1/tracking/duty/", {"on_duty": True}, format="json").status_code == 403
    )

    verify(member)
    client = client_for(User.objects.get(pk=agent.pk))  # each request reads the account afresh
    profile = client.get("/api/v1/auth/me/").json()
    assert profile["identity_verified"] is True
    assert {"submitElectionResult", "createIncident"} <= set(profile["capabilities"])
    checked = client.post("/api/v1/tracking/check-ins/", {"gps": gps()}, format="json")
    assert checked.status_code == 201, checked.content


def test_agent_and_observer_accounts_without_a_member_do_nothing():
    unit = PollingUnit.objects.get(code=UNIT)
    agent = User.objects.create_user(
        "AG-1",
        PASSWORD,
        full_name="Agent",
        role=Role.POLLING_UNIT_AGENT,
        scope_level=GeographyLevel.POLLING_UNIT,
        polling_unit=unit,
    )
    observer = User.objects.create_user(
        "OB-1",
        PASSWORD,
        full_name="Observer",
        role=Role.OBSERVER,
        scope_level=GeographyLevel.WARD,
        ward=Ward.objects.get(code="18-23-01"),
    )
    for account in (agent, observer):
        assert client_for(account).get("/api/v1/auth/me/").json()["capabilities"] == []
    assert (
        client_for(agent)
        .post("/api/v1/tracking/check-ins/", {"gps": gps()}, format="json")
        .status_code
        == 403
    )


def test_coverage_counts_only_agents_who_can_work():
    verified = new_member("08031110001", "Amina Yusuf")
    as_agent(verified)
    verify(verified)
    as_agent(new_member("08031110002", "Sani Musa"))  # not verified yet
    coordinator = User.objects.create_user(
        "LC-ZARIA",
        PASSWORD,
        full_name="Zaria Coordinator",
        role=Role.LGA_COORDINATOR,
        scope_level=GeographyLevel.LGA,
        lga=verified.lga,
    )
    assert client_for(coordinator).get("/api/v1/tracking/coverage/").json()["agents"] == 1

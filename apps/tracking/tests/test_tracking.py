import datetime
from datetime import timedelta

import pytest
from django.core.management import call_command
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.core.choices import ElectionStatus, ElectionType, GeographyLevel, Role
from apps.geography.models import Lga, PollingUnit, State, Ward
from apps.geography.polling_units import load_polling_units
from apps.results.models import Election, Party
from apps.tracking.models import DutySession, LocationPing
from conftest import PASSWORD

pytestmark = pytest.mark.django_db

UNIT = (11.0855, 7.7199)  # a point in Zaria


def fix(lat=UNIT[0], lng=UNIT[1], accuracy=12, minutes_ago=0, mocked=False):
    at = timezone.now() - timedelta(minutes=minutes_ago)
    return {
        "latitude": f"{lat:.6f}",
        "longitude": f"{lng:.6f}",
        "accuracy_m": accuracy,
        "captured_at": at.isoformat(),
        "is_mocked": mocked,
    }


def login(**fields):
    user = User.objects.create_user(password=PASSWORD, full_name=fields["access_id"], **fields)
    client = APIClient()
    client.force_authenticate(user)
    return client


@pytest.fixture(autouse=True)
def zaria(db):
    load_polling_units(lga_codes=["KD-ZARIA"])


def agent_at(code, access_id):
    return login(
        access_id=access_id,
        role=Role.POLLING_UNIT_AGENT,
        scope_level=GeographyLevel.POLLING_UNIT,
        polling_unit=PollingUnit.objects.get(code=code),
    )


@pytest.fixture
def agent():
    return agent_at("18-23-01-001", "AG-1")


@pytest.fixture
def agent2():
    return agent_at("18-23-01-002", "AG-2")


@pytest.fixture
def ward_coord():
    return login(
        access_id="WC-1",
        role=Role.WARD_COORDINATOR,
        scope_level=GeographyLevel.WARD,
        ward=Ward.objects.get(code="18-23-01"),
    )


@pytest.fixture
def lga_coord():
    return login(
        access_id="LC-1",
        role=Role.LGA_COORDINATOR,
        scope_level=GeographyLevel.LGA,
        lga=Lga.objects.get(code="KD-ZARIA"),
    )


@pytest.fixture
def lagos():
    return login(
        access_id="SC-LA",
        role=Role.STATE_COORDINATOR,
        scope_level=GeographyLevel.STATE,
        state=State.objects.get(code="LA"),
    )


@pytest.fixture
def director():
    return login(access_id="SRD-1", role=Role.SITUATION_ROOM_DIRECTOR)


def check_in(client, **kw):
    return client.post("/api/v1/tracking/check-ins/", {"gps": fix(**kw)}, format="json")


# --- check-in ------------------------------------------------------------------


def test_check_in_requires_gps_and_an_agent(agent, lga_coord):
    assert agent.post("/api/v1/tracking/check-ins/", {}, format="json").status_code == 400
    observer = login(access_id="OB-1", role=Role.OBSERVER)
    assert check_in(observer).status_code == 403
    assert check_in(lga_coord).status_code == 403


def test_confirmed_check_in_teaches_the_unit_location(agent, ward_coord):
    first = check_in(agent).json()
    assert first["location"]["status"] == "unverified"  # unit location still unknown
    assert first["polling_unit"] == "18-23-01-001"

    confirmed = ward_coord.post(f"/api/v1/tracking/check-ins/{first['id']}/confirm-location/")
    assert confirmed.status_code == 200, confirmed.content
    assert confirmed.json()["location"]["status"] == "onSite"
    assert confirmed.json()["confirmed_by"] == "WC-1"

    unit = PollingUnit.objects.get(code="18-23-01-001")
    assert unit.location_source == "checkIn"
    assert float(unit.latitude) == pytest.approx(UNIT[0])

    near = check_in(agent, lat=UNIT[0] + 0.0004).json()  # about 45 m
    assert near["location"]["status"] == "onSite"
    far = check_in(agent, lat=UNIT[0] + 0.01).json()  # about 1.1 km
    assert far["location"]["status"] == "away"
    assert 1000 < far["location"]["distance_to_pu_m"] < 1200


def test_faked_or_inaccurate_check_ins(agent, ward_coord):
    mocked = check_in(agent, mocked=True).json()
    assert mocked["location"]["status"] == "suspect"
    url = f"/api/v1/tracking/check-ins/{mocked['id']}/confirm-location/"
    assert ward_coord.post(url).status_code == 400

    vague = check_in(agent, accuracy=900).json()
    url = f"/api/v1/tracking/check-ins/{vague['id']}/confirm-location/"
    response = ward_coord.post(url)
    assert response.status_code == 400
    assert "accurate" in str(response.json())
    assert PollingUnit.objects.get(code="18-23-01-001").latitude is None


def test_confirmation_rules(agent, lagos):
    check = check_in(agent).json()
    url = f"/api/v1/tracking/check-ins/{check['id']}/confirm-location/"
    assert lagos.post(url).status_code == 404  # outside scope: not visible
    assert agent.post(url).status_code == 403  # agents cannot confirm


def test_agents_see_only_their_own_check_ins(agent, agent2, lga_coord):
    check_in(agent)
    assert agent2.get("/api/v1/tracking/check-ins/").json()["count"] == 0
    assert lga_coord.get("/api/v1/tracking/check-ins/").json()["count"] == 1


# --- GPS on results and incidents -------------------------------------------------


@pytest.fixture
def election():
    election = Election(
        code="2027-kd-gov",
        name="KD",
        type=ElectionType.GOVERNORSHIP,
        election_date=datetime.date(2027, 2, 20),
        status=ElectionStatus.OPEN,
        scope_level=GeographyLevel.STATE,
        state=State.objects.get(code="KD"),
    )
    election.full_clean(exclude=["parties"])
    election.save()
    election.parties.set(Party.objects.filter(acronym__in=["APC", "PDP"]))
    return election


def submit(client, **extra):
    payload = {
        "election": "2027-kd-gov",
        "polling_unit": "18-23-01-001",
        "party_votes": {"APC": 100, "PDP": 50},
        "total_valid_votes": 150,
        "accredited_voters": 160,
        **extra,
    }
    return client.post("/api/v1/results/", payload, format="json")


def test_results_from_the_app_need_gps(agent, election):
    response = submit(agent)
    assert response.status_code == 400 and "gps" in response.json()
    sms = submit(agent, source="sms")
    assert sms.status_code == 201
    assert sms.json()["gps"]["status"] == "noGps"
    assert sms.json()["status"] == "submitted"  # no GPS is expected from SMS
    assert not any("polling unit's known location" in n for n in sms.json()["validation"]["notes"])


def test_result_far_from_the_unit_goes_to_review(agent, ward_coord, election):
    check = check_in(agent).json()
    ward_coord.post(f"/api/v1/tracking/check-ins/{check['id']}/confirm-location/")

    body = submit(agent, gps=fix(lat=UNIT[0] + 0.02)).json()
    assert body["gps"]["status"] == "away"
    assert body["status"] == "underReview"
    assert any("from the polling unit" in n for n in body["validation"]["notes"])


def test_result_at_the_unit_is_clean(agent, ward_coord, election):
    check = check_in(agent).json()
    ward_coord.post(f"/api/v1/tracking/check-ins/{check['id']}/confirm-location/")
    body = submit(agent, gps=fix()).json()
    assert body["gps"]["status"] == "onSite"
    assert body["status"] == "submitted"


def test_incident_location_is_judged(agent):
    payload = {"title": "Snatching", "category": "Security", "severity": "high"}
    with_fake = agent.post(
        "/api/v1/incidents/", {**payload, "gps": fix(mocked=True)}, format="json"
    ).json()
    assert with_fake["location"]["status"] == "suspect"
    without = agent.post("/api/v1/incidents/", payload, format="json")
    assert without.status_code == 201
    assert without.json()["location"]["status"] == "noGps"


# --- duty, live map, coverage --------------------------------------------------------


def test_pings_are_kept_only_while_on_duty(agent):
    assert (
        agent.post("/api/v1/tracking/duty/", {"on_duty": True, "gps": fix()}, format="json").json()[
            "on_duty"
        ]
        is True
    )
    stored = agent.post(
        "/api/v1/tracking/pings/",
        {"pings": [fix(minutes_ago=0), fix(minutes_ago=30)]},
        format="json",
    ).json()
    assert stored["stored"] == 1  # the 30-minute-old reading predates the duty session
    agent.post("/api/v1/tracking/duty/", {"on_duty": False}, format="json")
    after = agent.post(
        "/api/v1/tracking/pings/",
        {"pings": [{**fix(), "captured_at": (timezone.now() + timedelta(minutes=5)).isoformat()}]},
        format="json",
    ).json()
    assert after["stored"] == 0
    assert LocationPing.objects.count() == 2  # start-of-duty fix + one ping


def test_only_field_roles_share_location(lga_coord):
    response = lga_coord.post("/api/v1/tracking/duty/", {"on_duty": True}, format="json")
    assert response.status_code == 403


def test_live_map(agent, agent2, director, lagos, lga_coord):
    agent.post("/api/v1/tracking/duty/", {"on_duty": True, "gps": fix()}, format="json")
    agent2.post("/api/v1/tracking/duty/", {"on_duty": True}, format="json")

    live = director.get("/api/v1/tracking/live/").json()
    assert live["count"] == 2
    by_agent = {a["agent"]: a for a in live["agents"]}
    assert by_agent["AG-1"]["position"]["status"] == "unverified"
    assert by_agent["AG-1"]["stale"] is False
    assert by_agent["AG-2"]["position"] is None and by_agent["AG-2"]["stale"] is True

    assert lagos.get("/api/v1/tracking/live/").json()["count"] == 0
    assert lga_coord.get("/api/v1/tracking/live/").json()["count"] == 2
    assert agent.get("/api/v1/tracking/live/").status_code == 403


def test_coverage(agent, agent2, ward_coord, lga_coord):
    check = check_in(agent).json()
    ward_coord.post(f"/api/v1/tracking/check-ins/{check['id']}/confirm-location/")
    check_in(agent)  # latest check-in of the day counts
    agent2.post("/api/v1/tracking/duty/", {"on_duty": True}, format="json")

    body = lga_coord.get("/api/v1/tracking/coverage/").json()
    assert body["agents"] == 2
    assert body["checked_in"] == 1
    assert body["on_site"] == 1
    assert body["not_checked_in"] == 1
    assert body["on_duty_now"] == 1
    assert len(body["children"]) == 13
    ward = next(c for c in body["children"] if c["code"] == "18-23-01")
    assert ward["agents"] == 2 and ward["on_site"] == 1


def test_purge_old_pings(agent):
    agent.post("/api/v1/tracking/duty/", {"on_duty": True, "gps": fix()}, format="json")
    LocationPing.objects.update(received_at=timezone.now() - timedelta(days=40))
    call_command("purge_location_pings")
    assert LocationPing.objects.count() == 0
    assert DutySession.objects.count() == 1

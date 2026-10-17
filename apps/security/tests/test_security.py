"""Security officers: the Situation Room sends an incident to the nearest one,
who answers each step (apps.security)."""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.core.management import call_command
from django.utils import timezone

from apps.access.models import RoleAssignment
from apps.access.tests.test_access import appoint, client_for, custom_role, member_account
from apps.accounts.models import User
from apps.core.choices import GeographyLevel, Role
from apps.core.permissions import Capability
from apps.field.models import Incident, IncidentEvent
from apps.geography.models import Lga, PollingUnit, Ward
from apps.messaging import providers
from apps.messaging.models import Device
from apps.security.models import SecurityDispatch
from apps.tracking.models import DutySession, LocationPing
from conftest import PASSWORD, verified_member

pytestmark = pytest.mark.django_db
C = Capability
INCIDENTS = "/api/v1/incidents/"


@pytest.fixture(autouse=True)
def outbox():
    providers.outbox.clear()
    yield providers.outbox
    providers.outbox.clear()


@pytest.fixture
def admin():
    return User.objects.create_user(
        "NA-1", PASSWORD, full_name="National Admin", role=Role.NATIONAL_ADMINISTRATOR
    )


@pytest.fixture
def units(db):
    """Three located polling units in Kaduna North: two close together, one
    about 11 km away."""
    lga = Lga.objects.get(code="KD-KADUNA-NORTH")
    near = Ward.objects.create(code="KD-KADUNA-NORTH-W01", name="Ward 01", lga=lga)
    far = Ward.objects.create(code="KD-KADUNA-NORTH-W02", name="Ward 02", lga=lga)
    spots = [
        ("KD-KN-W01-PU001", near, "10.523000", "7.438000"),
        ("KD-KN-W01-PU002", near, "10.540000", "7.430000"),
        ("KD-KN-W02-PU001", far, "10.600000", "7.500000"),
    ]
    return [
        PollingUnit.objects.create(
            code=code,
            name=f"Unit {code[-3:]} {ward.name}",
            ward=ward,
            latitude=Decimal(lat),
            longitude=Decimal(lng),
        )
        for code, ward, lat, lng in spots
    ]


@pytest.fixture
def role(admin):
    return custom_role(
        admin, "Security Officer", [C.RESPOND_TO_SECURITY], levels=("pollingUnit", "state")
    )


def officer_at(admin, role, phone, name, area):
    member = member_account(phone, name=name)
    response = appoint(admin, member, role, [area])
    assert response.status_code == 201, response.content
    return member


def at_unit(unit):
    return {"level": "pollingUnit", "pollingUnitId": unit.code}


@pytest.fixture
def officers(admin, role, units):
    """Officer at the unit of the incident, one 2 km away, one 11 km away, and
    one over Kano State, where no polling unit is located."""
    return {
        "near": officer_at(admin, role, "08036660001", "Garba Tanko", at_unit(units[0])),
        "next": officer_at(admin, role, "08036660002", "Ladi Yusuf", at_unit(units[1])),
        "far": officer_at(admin, role, "08036660003", "Bala Sani", at_unit(units[2])),
        "kano": officer_at(
            admin, role, "08036660004", "Kano Officer", {"level": "state", "stateId": "KN"}
        ),
    }


@pytest.fixture
def director(db):
    user = User.objects.create_user(
        "SRD-1", PASSWORD, full_name="Situation Director", role=Role.SITUATION_ROOM_DIRECTOR
    )
    return client_for(user)


@pytest.fixture
def reporter(units):
    user = User.objects.create_user(
        "AG-1",
        PASSWORD,
        full_name="Agent One",
        phone_number="08035550001",
        role=Role.POLLING_UNIT_AGENT,
        scope_level=GeographyLevel.POLLING_UNIT,
        polling_unit=units[0],
    )
    verified_member(user)
    return client_for(user)


def report(client, latitude="10.523500", longitude="7.438200"):
    payload = {
        "title": "Thugs at the unit",
        "category": "Violence / threat",
        "severity": "critical",
        "summary": "Armed group chasing voters.",
        "gps": {
            "latitude": latitude,
            "longitude": longitude,
            "accuracy_m": 10,
            "captured_at": timezone.now().isoformat(),
            "is_mocked": False,
        },
    }
    response = client.post(INCIDENTS, payload, format="json")
    assert response.status_code == 201, response.content
    return response.json()["reference"]


def nearest(client, reference, **params):
    return client.get(f"{INCIDENTS}{reference}/security/nearest/", params)


def names(response):
    assert response.status_code == 200, response.content
    return [officer["name"] for officer in response.json()["officers"]]


def send(client, reference, member, note=""):
    return client.post(
        f"{INCIDENTS}{reference}/security/",
        {"officer": member.user.access_id, "note": note},
        format="json",
    )


def answer(member, dispatch, step, note=""):
    return client_for(member.user).post(
        f"/api/v1/me/security/{dispatch}/respond/", {"step": step, "note": note}, format="json"
    )


def sms_to(outbox, phone):
    return [text for kind, to, text in outbox if kind == "sms" and phone in to]


def test_the_situation_room_sends_to_the_nearest_officer_who_answers_each_step(
    admin, director, reporter, officers, outbox
):
    reference = report(reporter)
    listed = nearest(director, reference)
    # Nearest first; Kano's officer, whose area has no located unit, last.
    assert names(listed) == ["Garba Tanko", "Ladi Yusuf", "Bala Sani", "Kano Officer"]
    first = listed.json()["officers"][0]
    assert first["located_by"] == "area" and first["distance_m"] < 100
    assert first["roles"] == ["Security Officer"] and first["busy"] is False
    assert listed.json()["officers"][-1]["distance_m"] is None
    assert listed.json()["located"] is True
    # A National Administrator holds every power but is not an officer.
    assert "National Admin" not in names(nearest(director, reference, limit=50))
    assert names(nearest(director, reference, search="ladi")) == ["Ladi Yusuf"]

    near = officers["near"]
    Device.objects.create(user=near.user, token="phone-1", platform="android")
    sent = send(director, reference, near, note="Police are 20 minutes out.")
    assert sent.status_code == 201, sent.content
    dispatch = sent.json()
    assert dispatch["status"] == "sent" and dispatch["notified"] == ["push", "sms"]
    assert dispatch["officer_phone"] == near.phone_number == "+2348036660001"
    assert Incident.objects.get(reference=reference).status == "escalated"

    call_command("deliver_messages", seconds=5)
    [text] = sms_to(outbox, "+2348036660001")
    assert reference in text and "Violence / threat (critical)" in text
    assert "8035550001" not in text  # the reporter's number stays in the app
    inbox = client_for(near.user).get("/api/v1/me/messages/").json()
    assert inbox[0]["title"] == f"Security: {reference}"

    # The officer sees where to go and whom to call.
    mine = client_for(near.user).get("/api/v1/me/security/")
    assert mine.status_code == 200, mine.content
    [item] = mine.json()
    assert item["next"] == ["accepted", "declined"]
    assert item["note"] == "Police are 20 minutes out."
    incident = item["incident"]
    assert incident["reporter_phone"].endswith("8035550001")
    assert incident["reporter_name"] == "Agent One"
    assert incident["gps"] is True and incident["latitude"] == pytest.approx(10.5235)
    assert incident["place"].startswith("Unit 001 Ward 01, Ward 01, Kaduna North")

    for step, note in (
        ("accepted", ""),
        ("onTheWay", ""),
        ("arrived", "Two of us at the gate."),
    ):
        response = answer(near, dispatch["id"], step, note)
        assert response.status_code == 200, response.content
        assert response.json()["status"] == step
    refused = answer(near, dispatch["id"], "resolved")
    assert refused.status_code == 400 and "note" in refused.json()
    done = answer(near, dispatch["id"], "resolved", "Crowd dispersed; voting resumed.")
    assert done.status_code == 200 and done.json()["next"] == []

    incident = Incident.objects.get(reference=reference)
    assert incident.status == "resolved" and incident.resolved_at is not None
    trail = list(
        IncidentEvent.objects.filter(incident=incident, action="security").values_list(
            "note", flat=True
        )
    )
    assert trail == [
        f"Sent to security officer Garba Tanko ({near.user.access_id}). Police are 20 minutes out.",
        "Security officer Garba Tanko: accepted.",
        "Security officer Garba Tanko: on the way.",
        "Security officer Garba Tanko: arrived. Two of us at the gate.",
        "Security officer Garba Tanko: resolved. Crowd dispersed; voting resumed.",
    ]
    # The Situation Room followed every step.
    followed = director.get(f"{INCIDENTS}{reference}/security/").json()
    assert followed["can_send"] is True
    [record] = followed["dispatches"]
    assert record["status"] == "resolved"
    assert record["outcome"] == "Crowd dispersed; voting resumed."
    assert all(record[f] for f in ("accepted_at", "on_the_way_at", "arrived_at", "finished_at"))
    # A resolved incident is not sent again.
    assert send(director, reference, officers["next"]).status_code == 400


def test_an_officer_who_declines_is_not_offered_again(director, reporter, officers):
    reference = report(reporter)
    near, other = officers["near"], officers["next"]
    dispatch = send(director, reference, near).json()["id"]
    # One officer at a time.
    second = send(director, reference, other)
    assert second.status_code == 400 and "Garba Tanko has this incident" in str(second.json())

    assert answer(near, dispatch, "declined").status_code == 400  # a reason is needed
    declined = answer(near, dispatch, "declined", "My team is at another unit.")
    assert declined.status_code == 200 and declined.json()["next"] == []
    assert answer(near, dispatch, "accepted").status_code == 400

    assert "Garba Tanko" not in names(nearest(director, reference))
    again = send(director, reference, near)
    assert again.status_code == 400 and "declined" in str(again.json())
    assert send(director, reference, other).status_code == 201
    history = director.get(f"{INCIDENTS}{reference}/security/").json()["dispatches"]
    assert [d["status"] for d in history] == ["sent", "declined"]


def test_only_the_situation_room_sends_and_only_the_officer_answers(
    admin, director, reporter, officers, lga_coordinator
):
    reference = report(reporter)
    # A coordinator assigns incidents in their area but is not the Situation Room.
    coordinator = client_for(lga_coordinator)
    assert nearest(coordinator, reference).status_code == 403
    assert send(coordinator, reference, officers["near"]).status_code == 403
    followed = coordinator.get(f"{INCIDENTS}{reference}/security/")
    assert followed.status_code == 200 and followed.json()["can_send"] is False
    # The reporter sees their incident, not who was sent.
    assert reporter.get(f"{INCIDENTS}{reference}/security/").status_code == 403

    dispatch = send(director, reference, officers["near"]).json()
    assert (
        coordinator.get(f"{INCIDENTS}{reference}/security/").json()["dispatches"][0][
            "officer_phone"
        ]
        is None
    )
    # Only the officer it was sent to answers it.
    assert answer(officers["next"], dispatch["id"], "accepted").status_code == 404
    plain = member_account("08036660009", name="Plain Member")
    assert client_for(plain.user).get("/api/v1/me/security/").status_code == 403
    # An administrator holds the power too, but nothing is sent to them.
    assert client_for(admin).get("/api/v1/me/security/").json() == []

    # An officer whose appointment is suspended stops answering.
    held = RoleAssignment.objects.get(member=officers["near"])
    suspended = client_for(admin).post(
        f"/api/v1/access/assignments/{held.uuid}/suspend/", {"reason": "Review"}, format="json"
    )
    assert suspended.status_code == 200, suspended.content
    assert answer(officers["near"], dispatch["id"], "accepted").status_code == 403
    assert "Garba Tanko" not in names(nearest(director, reference))


def test_live_location_counts_before_the_area(director, reporter, officers):
    reference = report(reporter)
    far = officers["far"]
    session = DutySession.objects.create(user=far.user)
    now = timezone.now()

    def ping(latitude, longitude, captured_at, mocked=False):
        return LocationPing.objects.create(
            session=session,
            latitude=Decimal(latitude),
            longitude=Decimal(longitude),
            gps_accuracy_m=8,
            gps_captured_at=captured_at,
            gps_is_mocked=mocked,
        )

    # An old fix and a faked one do not count: the area does.
    ping("10.523400", "7.438100", now - timedelta(minutes=40))
    ping("10.523400", "7.438100", now, mocked=True)
    assert names(nearest(director, reference))[:3] == ["Garba Tanko", "Ladi Yusuf", "Bala Sani"]

    ping("10.523400", "7.438100", now)
    listed = nearest(director, reference).json()["officers"]
    assert listed[0]["name"] == "Bala Sani"
    assert listed[0]["located_by"] == "live" and listed[0]["distance_m"] < 50
    assert listed[0]["seen_at"] is not None

    sent = send(director, reference, far).json()
    assert sent["located_by"] == "live"
    # Busy with this incident, they show as busy for the next one.
    other = report(reporter, latitude="10.540100", longitude="7.430100")
    by_name = {o["name"]: o for o in nearest(director, other).json()["officers"]}
    assert by_name["Bala Sani"]["busy"] is True and by_name["Ladi Yusuf"]["busy"] is False

    # Sharing ended: the area again.
    session.ended_at = timezone.now()
    session.save()
    assert nearest(director, reference).json()["officers"][0]["name"] == "Garba Tanko"


def test_cancelling_or_finishing_the_incident_stands_the_officer_down(
    admin, director, reporter, officers, outbox
):
    reference = report(reporter)
    near, other = officers["near"], officers["next"]
    national = client_for(admin)
    dispatch = send(national, reference, near).json()["id"]
    cancel = f"/api/v1/security/dispatches/{dispatch}/cancel/"
    assert national.post(cancel, {"reason": ""}, format="json").status_code == 400
    # Only the Situation Room cancels.
    assert client_for(near.user).post(cancel, {"reason": "x"}, format="json").status_code == 404
    cancelled = national.post(cancel, {"reason": "Police arrived first."}, format="json")
    assert cancelled.status_code == 200, cancelled.content
    assert cancelled.json()["status"] == "cancelled"
    assert cancelled.json()["cancelled_by"] == "National Admin"
    assert answer(near, dispatch, "accepted").status_code == 400
    assert national.post(cancel, {"reason": "again"}, format="json").status_code == 400

    call_command("deliver_messages", seconds=5)
    told = sms_to(outbox, "+2348036660001")
    assert len(told) == 2 and "cancelled: Police arrived first." in told[1]
    # Notices are not among the sender's messages.
    assert national.get("/api/v1/broadcasts/").json()["results"] == []

    # Resolving the incident in the Situation Room stands the next officer down.
    second = send(director, reference, other).json()["id"]
    assert answer(other, second, "accepted").status_code == 200
    resolved = director.post(
        f"{INCIDENTS}{reference}/status/",
        {"status": "resolved", "note": "Calm restored."},
        format="json",
    )
    assert resolved.status_code == 200, resolved.content
    finished = SecurityDispatch.objects.get(uuid=second)
    assert finished.status == "cancelled" and finished.cancel_reason == "The incident was resolved."
    assert IncidentEvent.objects.filter(
        incident__reference=reference, note__startswith="Security officer Ladi Yusuf stood down."
    ).exists()

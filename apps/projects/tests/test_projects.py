"""Projects: scheduled periods of field work, the people taking part, and
location shared only while one runs."""

import datetime
from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from apps.access.models import AuditEvent
from apps.access.tests.test_access import KD, client_for, member_account
from apps.accounts.models import User
from apps.core.choices import ElectionStatus, ElectionType, GeographyLevel, Role
from apps.field.models import Incident
from apps.geography.models import PollingUnit, State
from apps.geography.polling_units import load_polling_units
from apps.projects.models import Project
from apps.results.models import Election, Party, ResultSubmission
from apps.tracking.models import DutySession
from apps.tracking.tests.test_tracking import UNIT, agent_at, fix, login, on_project, submit
from conftest import PASSWORD

pytestmark = pytest.mark.django_db
ZARIA = {"level": "lga", "lgaId": "KD-ZARIA"}
LAGOS = {"level": "state", "stateId": "LA"}


@pytest.fixture(autouse=True)
def zaria(db):
    load_polling_units(lga_codes=["KD-ZARIA"])


@pytest.fixture
def admin():
    user = User.objects.create_user(
        "NA-1", PASSWORD, full_name="National Admin", role=Role.NATIONAL_ADMINISTRATOR
    )
    return client_for(user)


@pytest.fixture
def ward_coord():
    """A ward coordinator over the first Zaria ward."""
    from apps.geography.models import Ward

    return login(
        access_id="WC-1",
        role=Role.WARD_COORDINATOR,
        scope_level=GeographyLevel.WARD,
        ward=Ward.objects.get(code="18-23-01"),
    )


def at(**delta):
    return (timezone.now() + timedelta(**delta)).isoformat()


def schedule(client, **fields):
    body = {
        "name": "Kaduna governorship election",
        "description": "Be at your polling unit from 7 am.",
        "starts_at": at(hours=1),
        "ends_at": at(hours=11),
        "areas": [ZARIA],
        **fields,
    }
    return client.post("/api/v1/projects/", body, format="json")


def running(project_id):
    """Move a scheduled project's start into the past."""
    Project.objects.filter(uuid=project_id).update(starts_at=timezone.now() - timedelta(hours=1))


@pytest.fixture
def election():
    election = Election(
        code="2027-kd-gov",
        name="Kaduna governorship",
        type=ElectionType.GOVERNORSHIP,
        election_date=datetime.date(2027, 2, 20),
        status=ElectionStatus.DRAFT,
        scope_level=GeographyLevel.STATE,
        state=State.objects.get(code="KD"),
    )
    election.full_clean(exclude=["parties"])
    election.save()
    election.parties.set(Party.objects.filter(acronym__in=["APC", "PDP"]))
    return election


def test_a_national_administrator_schedules_projects(admin, election):
    made = schedule(admin, election="2027-kd-gov")
    assert made.status_code == 201, made.content
    project = made.json()
    assert project["status"] == "scheduled" and project["can_manage"] is True
    assert project["areas"][0]["lgaId"] == "KD-ZARIA"
    assert project["election"]["code"] == "2027-kd-gov"

    coordinator = login(
        access_id="SC-KD",
        role=Role.STATE_COORDINATOR,
        scope_level=GeographyLevel.STATE,
        state=State.objects.get(code="KD"),
    )
    assert schedule(coordinator).status_code == 403  # only an administrator
    assert schedule(admin, ends_at=at(minutes=30)).status_code == 400  # ends before it starts
    assert schedule(admin, ends_at=at(days=40)).status_code == 400  # longer than 31 days
    assert schedule(admin, starts_at=at(hours=-2)).status_code == 400  # already past
    assert schedule(admin, areas=[LAGOS], election="2027-kd-gov").status_code == 400

    url = f"/api/v1/projects/{project['id']}/"
    moved = admin.patch(url, {"starts_at": at(minutes=30), "areas": [KD]}, format="json")
    assert moved.status_code == 200, moved.content
    assert moved.json()["areas"][0]["stateId"] == "KD"
    running(project["id"])
    # Once it runs, its start, areas and election stay; its end may move.
    assert admin.patch(url, {"areas": [ZARIA]}, format="json").status_code == 400
    assert admin.patch(url, {"ends_at": at(hours=12)}, format="json").status_code == 200
    assert admin.get(url).json()["status"] == "active"

    assert admin.post(f"{url}cancel/", {}, format="json").status_code == 400  # say why
    cancelled = admin.post(f"{url}cancel/", {"reason": "Election postponed."}, format="json")
    assert cancelled.json()["status"] == "cancelled"
    actions = list(
        AuditEvent.objects.filter(target_type="project").values_list("action", flat=True)
    )
    assert actions.count("project.updated") == 2
    assert {"project.created", "project.cancelled"} <= set(actions)


def test_everyone_with_a_role_in_its_areas_sees_it(admin, ward_coord):
    project = schedule(admin).json()
    assert [p["id"] for p in ward_coord.get("/api/v1/projects/").json()] == [project["id"]]
    lagos = login(
        access_id="SC-LA",
        role=Role.STATE_COORDINATOR,
        scope_level=GeographyLevel.STATE,
        state=State.objects.get(code="LA"),
    )
    assert lagos.get("/api/v1/projects/").json() == []
    assert lagos.get(f"/api/v1/projects/{project['id']}/").status_code == 403
    # Ordinary members' app stays as it is.
    member = member_account("08036660001", name="Ada Obi")
    assert client_for(member.user).get("/api/v1/projects/").json() == []


def test_coordinators_assign_people_who_agree_and_then_share_their_location(admin, ward_coord):
    project = schedule(admin, starts_at=at(minutes=1)).json()
    running(project["id"])
    unit = PollingUnit.objects.get(code="18-23-01-001")
    unit.latitude, unit.longitude = Decimal(str(UNIT[0])), Decimal(str(UNIT[1]))
    unit.save()
    agent = agent_at("18-23-01-001", "AG-1")
    number = User.objects.get(access_id="AG-1").member.membership_number
    # Not taking part, so nothing is shared.
    refused = agent.post("/api/v1/tracking/duty/", {"on_duty": True}, format="json")
    assert refused.status_code == 403
    assert "project" in refused.json()["detail"]

    people = f"/api/v1/projects/{project['id']}/participants/"
    assigned = ward_coord.post(people, {"member": number}, format="json")
    assert assigned.status_code == 201, assigned.content
    work = assigned.json()
    assert work["status"] == "invited" and work["place"]["pollingUnitId"] == "18-23-01-001"
    assert ward_coord.post(people, {"member": number}, format="json").status_code == 400
    agent_at("18-23-05-001", "AG-5")  # another ward: not this coordinator's
    elsewhere = User.objects.get(access_id="AG-5").member.membership_number
    assert ward_coord.post(people, {"member": elsewhere}, format="json").status_code == 403
    plain = member_account("08036660002", name="Bala Musa")
    in_ward = {"level": "pollingUnit", "pollingUnitId": "18-23-01-002"}
    no_role = ward_coord.post(
        people, {"member": plain.membership_number, "place": in_ward}, format="json"
    )
    assert no_role.status_code == 400 and "role" in str(no_role.json())

    mine = agent.get("/api/v1/me/projects/").json()
    assert mine[0]["my_participation"]["status"] == "invited"
    accepted = agent.post(f"/api/v1/me/projects/{work['id']}/accept/", {}, format="json")
    assert accepted.json()["status"] == "accepted"
    on = agent.post("/api/v1/tracking/duty/", {"on_duty": True, "gps": fix()}, format="json")
    assert on.status_code == 200, on.content
    session = DutySession.objects.get()
    assert session.participation.project.uuid.hex == project["id"].replace("-", "")

    monitor = f"/api/v1/projects/{project['id']}/monitor/"
    assert ward_coord.get(monitor).json()["people"][0]["state"] == "atPlace"
    far = fix(lat=UNIT[0] + 0.05)  # about 5.5 km away
    agent.post("/api/v1/tracking/pings/", {"pings": [far]}, format="json")
    watched = ward_coord.get(monitor).json()
    assert watched["people"][0]["state"] == "away"
    assert watched["counts"] == {"away": 1}
    assert agent.get(monitor).status_code == 403  # agents do not watch

    withdrew = agent.post(f"/api/v1/me/projects/{work['id']}/withdraw/", {}, format="json")
    assert withdrew.json()["status"] == "withdrawn"
    session.refresh_from_db()
    assert session.ended_at is not None
    assert agent.post("/api/v1/tracking/duty/", {"on_duty": True}, format="json").status_code == 403
    assert {
        "project.participantAssigned",
        "project.participantAccepted",
        "project.participantWithdrew",
    } <= set(AuditEvent.objects.values_list("action", flat=True))


def test_sharing_stops_when_the_project_ends(admin):
    agent = agent_at("18-23-01-001", "AG-1")
    project = on_project("AG-1")
    sharing_since = {"on_duty": True, "at": at(minutes=-30)}
    agent.post("/api/v1/tracking/duty/", sharing_since, format="json")
    ended = timezone.now() - timedelta(minutes=5)
    Project.objects.filter(pk=project.pk).update(ends_at=ended)
    stored = agent.post("/api/v1/tracking/pings/", {"pings": [fix()]}, format="json").json()
    assert stored["stored"] == 0
    assert DutySession.objects.get().ended_at == ended
    assert agent.post("/api/v1/tracking/duty/", {"on_duty": True}, format="json").status_code == 403


def test_work_during_a_project_counts_for_it(admin, election):
    agent = agent_at("18-23-01-001", "AG-1")
    project = on_project("AG-1")
    # With no project naming it, an election takes results only while open.
    assert submit(agent, gps=fix()).status_code == 400
    Project.objects.filter(pk=project.pk).update(election=election)
    result = submit(agent, gps=fix())
    assert result.status_code == 201, result.content
    assert ResultSubmission.objects.get().project == project
    listed = agent.get("/api/v1/elections/?accepting=true").json()
    assert [e["code"] for e in listed] == ["2027-kd-gov"]
    assert listed[0]["accepting_results"] is True

    incident = agent.post(
        "/api/v1/incidents/",
        {"title": "Ballot box snatched", "category": "Security", "severity": "high", "gps": fix()},
        format="json",
    )
    assert incident.status_code == 201, incident.content
    assert Incident.objects.get().project == project

    Project.objects.filter(pk=project.pk).update(cancelled_at=timezone.now())
    later = submit(agent, gps=fix(), polling_unit="18-23-01-002")
    assert later.status_code == 400  # the project naming the election was cancelled
    assert agent.get("/api/v1/elections/?accepting=true").json() == []


def test_one_person_is_in_one_project_at_a_time(admin, ward_coord):
    agent_at("18-23-01-001", "AG-1")
    number = User.objects.get(access_id="AG-1").member.membership_number
    first = schedule(admin).json()
    second = schedule(admin, name="Voter registration drive", starts_at=at(hours=5)).json()
    url = "/api/v1/projects/{}/participants/"
    assert (
        ward_coord.post(url.format(first["id"]), {"member": number}, format="json").status_code
        == 201
    )
    clash = ward_coord.post(url.format(second["id"]), {"member": number}, format="json")
    assert clash.status_code == 400 and "same time" in str(clash.json())

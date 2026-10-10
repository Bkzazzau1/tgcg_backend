import datetime

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.core.choices import ElectionStatus, ElectionType, GeographyLevel, Role
from apps.geography.models import Lga, PollingUnit, SenatorialDistrict, State, Ward
from apps.results.models import Election, Party
from conftest import PASSWORD

pytestmark = pytest.mark.django_db

RESULTS = "/api/v1/results/"
COLLATION = "/api/v1/collation/"
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64


@pytest.fixture(autouse=True)
def media_root(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path


@pytest.fixture
def pus(db):
    lga = Lga.objects.get(code="KD-KADUNA-NORTH")
    ward = Ward.objects.create(code="KD-KN-W01", name="Ward 01", lga=lga)
    units = [
        PollingUnit.objects.create(
            code=f"KD-KN-W01-PU00{i}", name=f"Unit {i}", ward=ward, registered_voters=500
        )
        for i in (1, 2, 3)
    ]
    la_ward = Ward.objects.create(
        code="LA-IK-W03", name="Ward 03", lga=Lga.objects.get(code="LA-IKEJA")
    )
    units.append(PollingUnit.objects.create(code="LA-IK-W03-PU012", name="Unit 12", ward=la_ward))
    return units


def make_election(code, type_, parties=("APC", "PDP", "LP"), **scope):
    election = Election(
        code=code,
        name=code,
        type=type_,
        election_date=datetime.date(2027, 2, 20),
        status=ElectionStatus.OPEN,
        **scope,
    )
    election.full_clean(exclude=["parties"])
    election.save()
    election.parties.set(Party.objects.filter(acronym__in=parties))
    return election


@pytest.fixture
def kd_election(db):
    return make_election(
        "2027-kd-governorship",
        ElectionType.GOVERNORSHIP,
        scope_level=GeographyLevel.STATE,
        state=State.objects.get(code="KD"),
    )


@pytest.fixture
def presidential(db):
    return make_election("2027-presidential", ElectionType.PRESIDENTIAL)


def login(**fields):
    user = User.objects.create_user(password=PASSWORD, **fields)
    client = APIClient()
    client.force_authenticate(user)
    return client


def pu_user(access_id, pu):
    return login(
        access_id=access_id,
        full_name=access_id,
        role=Role.POLLING_UNIT_AGENT,
        scope_level=GeographyLevel.POLLING_UNIT,
        polling_unit=pu,
    )


@pytest.fixture
def agent1(pus):
    return pu_user("AG-1", pus[0])


@pytest.fixture
def agent2(pus):
    return pu_user("AG-2", pus[1])


@pytest.fixture
def lga_coord(db):
    return login(
        access_id="LC-KN",
        full_name="LGA",
        role=Role.LGA_COORDINATOR,
        scope_level=GeographyLevel.LGA,
        lga=Lga.objects.get(code="KD-KADUNA-NORTH"),
    )


@pytest.fixture
def kd_coord(db):
    return login(
        access_id="SC-KD",
        full_name="KD",
        role=Role.STATE_COORDINATOR,
        scope_level=GeographyLevel.STATE,
        state=State.objects.get(code="KD"),
    )


@pytest.fixture
def la_coord(db):
    return login(
        access_id="SC-LA",
        full_name="LA",
        role=Role.STATE_COORDINATOR,
        scope_level=GeographyLevel.STATE,
        state=State.objects.get(code="LA"),
    )


@pytest.fixture
def legal(db):
    return login(access_id="LEGAL-1", full_name="Legal", role=Role.LEGAL_OFFICER)


@pytest.fixture
def director(db):
    return login(access_id="SRD-1", full_name="Director", role=Role.SITUATION_ROOM_DIRECTOR)


def submit(client, pu="KD-KN-W01-PU001", election="2027-kd-governorship", **extra):
    payload = {
        "election": election,
        "polling_unit": pu,
        "party_votes": {"APC": 120, "PDP": 98},
        "total_valid_votes": 218,
        "accredited_voters": 230,
        "rejected_votes": 7,
        **extra,
    }
    return client.post(RESULTS, payload, format="json")


# --- submission and integrity ---------------------------------------------


def test_agent_submits_clean_result(agent1, kd_election):
    response = submit(agent1, client_id="dev-1:RES-1")
    assert response.status_code == 201, response.content
    body = response.json()
    assert body["reference"] == "RES-000001"
    assert body["status"] == "submitted"
    assert body["official"] is False
    assert body["party_votes"] == {"APC": 120, "PDP": 98, "LP": 0}
    assert body["calculated_party_votes"] == 218
    assert body["registered_voters"] == 500  # from master data
    assert body["scope"]["pollingUnitId"] == "KD-KN-W01-PU001"
    assert body["scope"]["lgaId"] == "KD-KADUNA-NORTH"
    assert body["validation"] == {
        "arithmetic_valid": True,
        "duplicate_suspected": False,
        "polling_unit_matched": True,
        "agent_scope_matched": True,
        "ocr_confidence": None,
        "ocr_matched_manual_entry": None,
        "notes": [],
        "requires_human_review": False,
    }
    retry = submit(agent1, client_id="dev-1:RES-1")
    assert retry.json()["reference"] == "RES-000001"


@pytest.mark.parametrize(
    "extra, note",
    [
        ({"total_valid_votes": 300}, "does not match the recorded total"),
        ({"accredited_voters": 200}, "exceed accredited voters"),
        (
            {
                "party_votes": {"APC": 400, "PDP": 120},
                "total_valid_votes": 520,
                "accredited_voters": 530,
            },
            "exceed registered voters",
        ),
    ],
)
def test_arithmetic_failures_go_to_review(agent1, kd_election, extra, note):
    body = submit(agent1, **extra).json()
    assert body["status"] == "underReview"
    assert body["validation"]["arithmetic_valid"] is False
    assert any(note in n for n in body["validation"]["notes"])


def test_unknown_party_rejected(agent1, kd_election):
    response = submit(agent1, party_votes={"APC": 100, "XYZ": 118})
    assert response.status_code == 400
    assert "XYZ" in str(response.json())


def test_negative_votes_rejected(agent1, kd_election):
    assert submit(agent1, party_votes={"APC": -1}).status_code == 400


def test_polling_unit_must_be_in_election_and_scope(agent1, kd_election, presidential, la_coord):
    assert submit(agent1, pu="LA-IK-W03-PU012").status_code == 400  # not in KD election
    assert submit(agent1, pu="KD-KN-W01-PU002").status_code == 403  # not agent's unit
    assert submit(la_coord, pu="KD-KN-W01-PU001", election="2027-presidential").status_code == 403


def test_closed_election_rejects_results(agent1, kd_election):
    kd_election.status = ElectionStatus.CLOSED
    kd_election.save()
    assert submit(agent1).status_code == 400


def test_duplicate_and_on_behalf_submissions_need_review(agent1, lga_coord, kd_election):
    submit(agent1)
    second = submit(lga_coord).json()
    assert second["status"] == "underReview"
    assert second["validation"]["duplicate_suspected"] is True
    assert second["validation"]["agent_scope_matched"] is False


def test_ocr_comparison(agent1, agent2, kd_election):
    mismatch = submit(agent1, ocr_party_votes={"APC": 112, "PDP": 98}, ocr_confidence=0.81)
    assert mismatch.json()["status"] == "underReview"
    assert mismatch.json()["validation"]["ocr_matched_manual_entry"] is False

    match = submit(agent2, pu="KD-KN-W01-PU002", ocr_party_votes={"APC": 120, "PDP": 98, "LP": 0})
    assert match.json()["status"] == "submitted"
    assert match.json()["validation"]["ocr_matched_manual_entry"] is True


# --- review -----------------------------------------------------------------


def test_verify_dispute_reject_and_audit(agent1, lga_coord, legal, kd_election):
    ref = submit(agent1).json()["reference"]
    url = f"{RESULTS}{ref}/"

    assert agent1.post(f"{url}verify/").status_code == 403
    assert legal.post(f"{url}verify/").status_code == 403  # legal can dispute only
    verified = lga_coord.post(f"{url}verify/", {"note": "Matches sheet"})
    assert verified.status_code == 200
    assert verified.json()["status"] == "verified"
    assert verified.json()["verified_by"] == "LC-KN"

    assert legal.post(f"{url}dispute/", {"reason": " "}).status_code == 400
    disputed = legal.post(f"{url}dispute/", {"reason": "Sheet shows overwriting"})
    assert disputed.json()["status"] == "disputed"

    rejected = lga_coord.post(f"{url}reject/", {"reason": "Altered sheet confirmed"})
    assert rejected.json()["status"] == "rejected"
    assert lga_coord.post(f"{url}verify/").status_code == 400  # rejected is final

    events = lga_coord.get(f"{url}events/").json()
    assert [(e["from_status"], e["to_status"]) for e in events] == [
        ("", "submitted"),
        ("submitted", "verified"),
        ("verified", "disputed"),
        ("disputed", "rejected"),
    ]


def test_cannot_verify_own_or_invalid_or_second_result(agent1, lga_coord, kd_coord, kd_election):
    on_behalf = submit(lga_coord, pu="KD-KN-W01-PU001").json()["reference"]
    assert lga_coord.post(f"{RESULTS}{on_behalf}/verify/").status_code == 403

    bad = submit(agent1, total_valid_votes=999).json()["reference"]
    assert kd_coord.post(f"{RESULTS}{bad}/verify/").status_code == 400

    assert kd_coord.post(f"{RESULTS}{on_behalf}/verify/").status_code == 200
    good = submit(agent1).json()["reference"]
    response = kd_coord.post(f"{RESULTS}{good}/verify/")
    assert response.status_code == 400
    assert "already verified" in str(response.json())


def test_visibility(agent1, agent2, lga_coord, la_coord, kd_election):
    submit(agent1)
    assert agent1.get(RESULTS).json()["count"] == 1
    assert agent2.get(RESULTS).json()["count"] == 0
    assert lga_coord.get(RESULTS).json()["count"] == 1
    assert la_coord.get(RESULTS).json()["count"] == 0


def test_result_form_upload(agent1, lga_coord, kd_election):
    ref = submit(agent1).json()["reference"]
    upload = agent1.post(
        f"{RESULTS}{ref}/evidence/",
        {"file": SimpleUploadedFile("ec8a.jpg", JPEG), "type": "resultForm"},
        format="multipart",
    )
    assert upload.status_code == 201, upload.content
    result = lga_coord.get(f"{RESULTS}{ref}/").json()
    assert result["result_form"]["file_name"] == "ec8a.jpg"
    download = lga_coord.get(result["result_form"]["download_url"])
    assert download.status_code == 200


# --- collation --------------------------------------------------------------


def verified_results(agent1, agent2, reviewer):
    a = submit(agent1).json()["reference"]
    b = submit(
        agent2,
        pu="KD-KN-W01-PU002",
        party_votes={"APC": 80, "PDP": 150, "LP": 20},
        total_valid_votes=250,
        accredited_voters=260,
        rejected_votes=3,
    ).json()["reference"]
    for ref in (a, b):
        assert reviewer.post(f"{RESULTS}{ref}/verify/").status_code == 200
    return a, b


def test_lga_collation_counts_verified_only(agent1, agent2, lga_coord, kd_election):
    verified_results(agent1, agent2, lga_coord)
    pu3 = PollingUnit.objects.get(code="KD-KN-W01-PU003")
    pu_user("AG-3", pu3).post(
        RESULTS,
        {
            "election": "2027-kd-governorship",
            "polling_unit": "KD-KN-W01-PU003",
            "party_votes": {"APC": 999},
            "total_valid_votes": 999,
            "accredited_voters": 999,
        },
        format="json",
    )  # unverified: must not count

    body = lga_coord.get(COLLATION, {"election": "2027-kd-governorship"}).json()
    assert body["official"] is False
    assert body["scope"]["lgaId"] == "KD-KADUNA-NORTH"
    assert body["expected_polling_units"] == 3
    assert body["reported_polling_units"] == 3
    assert body["verified_polling_units"] == 2
    assert body["under_review"] == 1
    assert body["missing_polling_units"] == 1
    assert body["completion_percent"] == 66.67
    assert body["party_votes"] == {"PDP": 248, "APC": 200, "LP": 20}
    assert body["leading_party"] == "PDP"
    assert body["total_valid_votes"] == 468
    assert body["rejected_votes"] == 10
    assert [c["code"] for c in body["children"]] == ["KD-KN-W01"]
    assert body["children"][0]["verified_polling_units"] == 2


def test_state_and_national_breakdowns(agent1, agent2, kd_coord, director, kd_election):
    verified_results(agent1, agent2, kd_coord)

    state = kd_coord.get(COLLATION, {"election": "2027-kd-governorship"}).json()
    districts = SenatorialDistrict.objects.filter(state__code="KD").count()
    assert len(state["children"]) == districts
    assert sum(c["verified_polling_units"] for c in state["children"]) == 2

    national = director.get(
        COLLATION, {"election": "2027-kd-governorship", "level": "country"}
    ).json()
    assert [c["code"] for c in national["children"]] == ["NW"]  # election covers KD only
    assert national["party_votes"]["PDP"] == 248

    lga = director.get(
        COLLATION,
        {"election": "2027-kd-governorship", "level": "lga", "code": "KD-KADUNA-NORTH"},
    ).json()
    assert lga["verified_polling_units"] == 2


def test_presidential_national_children_cover_all_zones(director, presidential):
    body = director.get(COLLATION, {"election": "2027-presidential"}).json()
    assert len(body["children"]) == 6


def test_collation_scope_rules(kd_coord, la_coord, agent1, kd_election):
    assert (
        la_coord.get(
            COLLATION, {"election": "2027-kd-governorship", "level": "state", "code": "KD"}
        ).status_code
        == 403
    )
    assert la_coord.get(COLLATION, {"election": "2027-kd-governorship"}).status_code == 400
    assert agent1.get(COLLATION, {"election": "2027-kd-governorship"}).status_code == 403
    assert (
        kd_coord.get(
            COLLATION, {"election": "2027-kd-governorship", "level": "lga", "code": "NOPE"}
        ).status_code
        == 400
    )


def test_elections_and_parties_listed(agent1, kd_election):
    elections = agent1.get("/api/v1/elections/").json()
    assert elections[0]["code"] == "2027-kd-governorship"
    assert [p["acronym"] for p in elections[0]["parties"]] == ["APC", "LP", "PDP"]
    assert len(agent1.get("/api/v1/parties/").json()) == 18

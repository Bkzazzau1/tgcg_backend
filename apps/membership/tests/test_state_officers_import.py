"""Importing a state chapter's officers (manage.py import_state_officers).

Names and numbers here are made up; real lists never go into tests.
"""

import io

import pytest
from django.core.cache import cache
from django.core.management import call_command
from django.core.management.base import CommandError
from rest_framework.test import APIClient

from apps.access.models import DelegationGrant, RoleAssignment, RoleDefinition
from apps.accounts.models import User
from apps.core.choices import GeographyLevel, Role, VerificationStatus
from apps.geography.models import State
from apps.geography.polling_units import load_polling_units
from apps.membership.management.commands.import_executives import PU_PERMISSIONS
from apps.membership.management.commands.import_state_officers import (
    STATE_OFFICER_PERMISSIONS,
)
from apps.membership.models import Member
from conftest import PASSWORD

pytestmark = pytest.mark.django_db

ROWS = [
    # sn, name, role, phone, place, hold
    ("1", "HON. BALA MUSA", "State Coordinator", "08030000001", "21-03-03-018", ""),
    ("2", "ADA BELLO", "State Secretary General", "08030000002", "21-20-05", ""),
    ("3", "HAUWA SANI", "Assistant State Women Leader", "08030000003", "KE-ARGUNGU", ""),
    ("4", "LADI YARO", "Assistant State Women Leader", "07060000004", "21-20-05", ""),
    ("5", "AUDU KANO", "State Financial Secretary", "070600000055", "21-20-05", ""),  # 12 digits
    ("6", "IDI GARBA", "State Programme Manager", "", "21-20-05", ""),  # no phone
    ("7", "MUSA YARO", "State Organising Secretary", "08030000007", "KE-FAKAI", "to confirm"),
    ("8", "SANI DOGO", "State Treasurer", "08030000008", "18-23-05", ""),  # Kaduna ward
]


@pytest.fixture
def admin():
    load_polling_units(
        lga_codes=["KE-ARGUNGU", "KE-YAURI", "KD-ZARIA", "KD-SOBA", "JI-BUJI", "JI-HADEJIA"]
    )
    return User.objects.create_user(
        "NA-1", PASSWORD, full_name="National Admin", role=Role.NATIONAL_ADMINISTRATOR
    )


def csv_file(tmp_path, rows=ROWS, header="sn,name,role,phone,place,hold"):
    path = tmp_path / "officers.csv"
    lines = [header]
    lines += [",".join(f'"{value}"' for value in row) for row in rows]
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def run(path, *flags, by="NA-1", state="KE"):
    out = io.StringIO()
    call_command(
        "import_state_officers",
        str(path),
        "--state",
        state,
        "--by",
        by,
        "--source",
        "Test chapter: state officers list",
        *flags,
        stdout=out,
    )
    return out.getvalue()


def test_a_dry_run_reports_held_rows_by_number_and_saves_nothing(admin, tmp_path):
    report = run(csv_file(tmp_path))
    assert (
        "4 to import, 4 held back. Placed at 1 polling units, 2 wards and 1 LGAs; "
        "0 with a VIN." in report
    )
    assert "3 roles, 2 of them new." in report
    assert "line 6 (S/N 5): not a valid Nigerian mobile number" in report
    assert "line 7 (S/N 6): not a valid Nigerian mobile number" in report
    assert "line 8 (S/N 7): held by the list: to confirm" in report
    assert "line 9 (S/N 8): place not in this state's INEC register" in report
    # Never a name or a phone number in the report.
    assert "SANI" not in report.upper() and "0803" not in report
    assert not Member.objects.exists()


def test_officers_become_verified_members_with_state_roles(admin, tmp_path):
    report = run(csv_file(tmp_path), "--commit")
    assert (
        "Imported 4 members with their roles (3 roles); 1 coordinators may appoint in "
        "their areas." in report
    )

    places = {
        m.full_name: (m.scope_level, m.polling_unit_id and m.polling_unit.code)
        for m in Member.objects.select_related("polling_unit")
    }
    assert places["Hon. Bala Musa"] == (GeographyLevel.POLLING_UNIT, "21-03-03-018")
    assert places["Ada Bello"][0] == GeographyLevel.WARD
    assert places["Hauwa Sani"][0] == GeographyLevel.LGA
    for member in Member.objects.all():
        assert member.verification_status == VerificationStatus.VERIFIED
        assert member.verified_by == admin
        assert member.state.code == "KE"

    # The State Coordinator holds the system role; the others, new officer roles.
    coordinator = RoleAssignment.objects.get(member__full_name="Hon. Bala Musa")
    assert coordinator.role.code == Role.STATE_COORDINATOR
    officer = RoleDefinition.objects.get(name="Assistant State Women Leader")
    assert sorted(officer.permissions) == STATE_OFFICER_PERMISSIONS
    assert officer.permitted_levels == [
        GeographyLevel.STATE,
        GeographyLevel.SENATORIAL_DISTRICT,
    ]
    assert officer.supervising_role.code == Role.STATE_COORDINATOR
    assert officer.assignments.count() == 2
    for assignment in RoleAssignment.objects.prefetch_related("areas"):
        (area,) = assignment.areas.all()
        assert (area.scope_level, area.state.code) == (GeographyLevel.STATE, "KE")

    # The phone number as written is the first password, and the role works.
    login = APIClient().post(
        "/api/v1/auth/login/",
        {"username": "08030000002", "password": "08030000002"},
        format="json",
    )
    assert login.status_code == 200, login.content
    capabilities = set(login.json()["user"]["capabilities"])
    assert {"viewIncidents", "viewCollation", "joinMeeting", "viewDiscussionRoom"} <= capabilities
    assert not capabilities & {"registerMembers", "verifyMemberIdentity", "viewSituationRoom"}

    # Run again: those imported are held as registered; nothing is added twice.
    again = run(csv_file(tmp_path), "--commit")
    assert "0 to import, 8 held back." in again
    assert "line 3 (S/N 2): phone number registered already" in again
    assert Member.objects.count() == 4


def test_only_a_national_administrator_imports(admin, tmp_path):
    User.objects.create_user(
        "SC-1",
        PASSWORD,
        full_name="Coordinator",
        role=Role.STATE_COORDINATOR,
        scope_level=GeographyLevel.STATE,
        state=State.objects.get(code="KE"),
    )
    with pytest.raises(CommandError):
        run(csv_file(tmp_path), by="SC-1")
    assert not Member.objects.exists()


JIGAWA = [
    # sn, name, role, phone, place, area, vin, hold
    (
        "1",
        "IDI KANO",
        "Deputy State Coordinator",
        "08030000011",
        "17-05-04",
        "SD/049/JG",
        "90F5AFAE76296269300",
        "",
    ),
    (
        "2",
        "ADA SULE",
        "State Director of ICT",
        "08030000012",
        "JI-HADEJIA",
        "",
        "90F5AFAE76296269300",
        "",
    ),  # the first row's VIN
    (
        "3",
        "BALA GARBA",
        "State Director of ICT",
        "08030000013",
        "JI-HADEJIA",
        "SD/999/XX",
        "",
        "",
    ),  # no such district here
    ("4", "LADI MUSA", "State Women Leader", "08030000014", "JI-HADEJIA", "", "90F5AF", ""),
]


def test_a_deputy_coordinates_their_senatorial_district(admin, tmp_path):
    header = "sn,name,role,phone,place,area,vin,hold"
    report = run(csv_file(tmp_path, JIGAWA, header=header), state="JI")
    assert "line 2 (S/N 1): VIN shared with another row" in report
    assert "line 3 (S/N 2): VIN shared with another row" in report
    assert "line 4 (S/N 3): area not in this state" in report
    assert "line 5 (S/N 4): VIN not valid" in report
    assert "90F5" not in report

    # The chapter corrects row 2's VIN: rows 1 and 2 go in, 3 and 4 wait.
    rows = [*JIGAWA]
    rows[1] = (*rows[1][:6], "", rows[1][7])
    report = run(csv_file(tmp_path, rows, header=header), "--commit", state="JI")
    assert "2 to import, 2 held back." in report
    assert "1 coordinators may appoint in their areas." in report
    deputy = Member.objects.get(full_name="Idi Kano")
    assert deputy.vin == "90F5AFAE76296269300"
    assignment = RoleAssignment.objects.select_related("role").get(member=deputy)
    coordinator = RoleDefinition.objects.get(code=Role.STATE_COORDINATOR)
    assert assignment.role.name == "Deputy State Coordinator"
    assert sorted(assignment.role.permissions) == sorted(coordinator.permissions)
    (area,) = assignment.areas.all()
    assert (area.scope_level, area.senatorial_district.code) == (
        GeographyLevel.SENATORIAL_DISTRICT,
        "SD/049/JG",
    )
    grant = DelegationGrant.objects.get(recipient=deputy)
    assert {"assignRoles", "manageAssignments"} <= set(grant.permissions)

    # The deputy appoints an LGA Coordinator inside their district, not outside.
    candidate = Member.objects.get(full_name="Ada Sule")
    login = APIClient().post(
        "/api/v1/auth/login/",
        {"username": "08030000011", "password": "08030000011"},
        format="json",
    )
    assert login.status_code == 200, login.content
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {login.json()['access']}")
    appoint = lambda lga: client.post(  # noqa: E731
        "/api/v1/access/assignments/",
        {
            "member": candidate.membership_number,
            "role": Role.LGA_COORDINATOR.value,
            "areas": [{"level": "lga", "lgaId": lga}],
        },
        format="json",
    )
    assert appoint("JI-HADEJIA").status_code == 403  # Jigawa North East
    assert appoint("JI-BUJI").status_code == 201, appoint("JI-BUJI").content


KADUNA = [
    # sn, name, role, phone, place, area, vin, hold
    ("State 1", "MUSA BELLO", "State Coordinator", "08030000021", "KD", "", "", ""),
    ("State 2", "AMB. SANI ADO", "State Secretary", "08030000022", "KD", "", "", ""),
    ("State 9", "AMB. SANI ADO", "State Director of Programmes", "08030000022", "KD", "", "", ""),
    ("Zaria 1", "TANKO GARBA", "LGA Coordinator", "08030000023", "18-23-01", "KD-ZARIA", "", ""),
    ("Zaria 3", "ANAS ZUBAIRU", "LGA Secretary", "08030000024", "KD-ZARIA", "KD-ZARIA", "", ""),
    ("Zaria 9", "KABIRU TANKO", "State Secretary", "08030000025", "KD-ZARIA", "KD-ZARIA", "", ""),
    ("Kufena 1", "MUSA BINA", "Ward Coordinator", "08030000026", "18-23-13", "18-23-13", "", ""),
    ("Kufena 3", "SULE ADAMU", "Ward Secretary", "08030000027", "18-23-13", "18-23-13", "", ""),
    (
        "Kufena 001 #2",
        "SANI MUSA",
        "Assistant PU Organising Officer",
        "08030000028",
        "18-23-13-001",
        "18-23-13-001",
        "",
        "",
    ),
    (
        "Soba 5",
        "ALIYU IBRAHIM",
        "LGA Logistics Officer",
        "08030000029",
        "KD-SOBA",
        "KD-SOBA",
        "",
        "",
    ),
    ("Soba 90", "ALIYU SAIBU", "Ward Coordinator", "08030000029", "18-21-01", "18-21-01", "", ""),
    # Held with no area (the whole state): it must not make the new unit role a state one.
    (
        "Soba 26",
        "ISA MUSA",
        "Assistant PU Organising Officer",
        "08030000030",
        "18-21-09",
        "",
        "",
        "no polling unit on the list",
    ),
]


def test_lga_ward_and_unit_officers_with_their_coordinators(admin, tmp_path):
    path = csv_file(tmp_path, KADUNA, header="sn,name,role,phone,place,area,vin,hold")
    report = run(path, "--commit", state="KD")
    # State Secretary serves the state, not an LGA; one phone, two names: held.
    assert "line 7 (S/N Zaria 9): role not allowed for this area" in report
    assert "line 11 (S/N Soba 5): phone number shared with another row" in report
    assert "line 12 (S/N Soba 90): phone number shared with another row" in report
    assert "line 13 (S/N Soba 26): held by the list: no polling unit on the list" in report
    assert (
        "8 to import, 4 held back. Placed at 1 polling units, 3 wards and 1 LGAs; "
        "0 with a VIN." in report
    )
    assert "2 placed at state level." in report
    assert "7 people; 1 of them hold more than one role." in report
    assert "8 roles, 5 of them new." in report
    assert (
        "Imported 7 members with their roles (8 roles), 8 appointments; 3 coordinators "
        "may appoint in their areas." in report
    )

    # State officers without a place on the list belong to the state alone;
    # one person with two posts is one member with both roles.
    secretary = Member.objects.get(phone_number="+2348030000022")
    assert (secretary.scope_level, secretary.state.code, secretary.lga_id) == (
        GeographyLevel.STATE,
        "KD",
        None,
    )
    assert sorted(a.role.name for a in secretary.role_assignments.all()) == [
        "State Director of Programmes",
        "State Secretary",
    ]

    # New roles serve the level of their areas.
    lga_officer = RoleDefinition.objects.get(name="LGA Secretary")
    assert lga_officer.permitted_levels == [GeographyLevel.LGA]
    assert sorted(lga_officer.permissions) == STATE_OFFICER_PERMISSIONS
    assert lga_officer.supervising_role.code == Role.LGA_COORDINATOR
    ward_officer = RoleDefinition.objects.get(name="Ward Secretary")
    assert ward_officer.permitted_levels == [GeographyLevel.WARD]
    assert ward_officer.supervising_role.code == Role.WARD_COORDINATOR
    unit_role = RoleDefinition.objects.get(name="Assistant PU Organising Officer")
    assert unit_role.permitted_levels == [GeographyLevel.POLLING_UNIT]
    assert sorted(unit_role.permissions) == PU_PERMISSIONS

    # Each coordinator may appoint below their own level.
    levels = {grant.recipient.full_name: grant.levels for grant in DelegationGrant.objects.all()}
    assert levels == {
        "Musa Bello": ["lga", "ward", "pollingUnit"],
        "Tanko Garba": ["ward", "pollingUnit"],
        "Musa Bina": ["pollingUnit"],
    }

    def client_for(phone):
        cache.clear()  # the sign-in rate limit
        login = APIClient().post(
            "/api/v1/auth/login/", {"username": phone, "password": phone}, format="json"
        )
        assert login.status_code == 200, login.content
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {login.json()['access']}")
        return client

    def appoint(client, member, role, area):
        return client.post(
            "/api/v1/access/assignments/",
            {"member": member.membership_number, "role": role, "areas": [area]},
            format="json",
        )

    # The LGA Coordinator appoints a Ward Coordinator in Zaria, not in Soba.
    lga_coordinator = client_for("08030000023")
    candidate = Member.objects.get(full_name="Anas Zubairu")
    ward_role = Role.WARD_COORDINATOR.value
    outside = appoint(
        lga_coordinator, candidate, ward_role, {"level": "ward", "wardId": "18-21-01"}
    )
    assert outside.status_code == 403, outside.content
    inside = appoint(lga_coordinator, candidate, ward_role, {"level": "ward", "wardId": "18-23-04"})
    assert inside.status_code == 201, inside.content

    # The Ward Coordinator appoints at Kufena's units, not at another ward's.
    ward_coordinator = client_for("08030000026")
    officer = Member.objects.get(full_name="Sule Adamu")
    outside = appoint(
        ward_coordinator,
        officer,
        unit_role.code,
        {"level": "pollingUnit", "pollingUnitId": "18-23-01-001"},
    )
    assert outside.status_code == 403, outside.content
    inside = appoint(
        ward_coordinator,
        officer,
        unit_role.code,
        {"level": "pollingUnit", "pollingUnitId": "18-23-13-002"},
    )
    assert inside.status_code == 201, inside.content


FEDERAL = [
    # sn, name, role, phone, place, area, vin, hold
    (
        "FC 1",
        "MUSA FEDERAL",
        "Federal Constituency Coordinator",
        "08030000041",
        "KD-ZARIA",
        "KD-ZARIA; KD-SOBA",
        "",
        "",
    ),
    (
        "FC 2",
        "ADA MIXED",
        "Federal Constituency Coordinator",
        "08030000042",
        "KD-ZARIA",
        "KD-ZARIA;18-21-01",
        "",
        "",
    ),
    (
        "FC 3",
        "SANI ELSEWHERE",
        "Federal Constituency Coordinator",
        "08030000043",
        "KD",
        "JI-BUJI",
        "",
        "",
    ),
    ("Zaria 3", "ANAS ZUBAIRU", "LGA Secretary", "08030000024", "KD-ZARIA", "KD-ZARIA", "", ""),
]


def test_a_federal_constituency_coordinator_coordinates_their_lgas(admin, tmp_path):
    path = csv_file(tmp_path, FEDERAL, header="sn,name,role,phone,place,area,vin,hold")
    report = run(path, "--commit", state="KD")
    assert "line 3 (S/N FC 2): areas of different levels" in report
    assert "line 4 (S/N FC 3): area not in this state" in report
    assert "2 to import, 2 held back." in report
    assert "2 roles, 2 of them new." in report
    assert "1 coordinators may appoint in their areas." in report

    # One appointment over both LGAs, with an LGA Coordinator's powers.
    coordinator = Member.objects.get(full_name="Musa Federal")
    assignment = RoleAssignment.objects.select_related("role").get(member=coordinator)
    role = assignment.role
    lga_coordinator = RoleDefinition.objects.get(code=Role.LGA_COORDINATOR)
    assert role.name == "Federal Constituency Coordinator"
    assert role.permitted_levels == [GeographyLevel.LGA]
    assert sorted(role.permissions) == sorted(lga_coordinator.permissions)
    assert role.supervising_role.code == Role.STATE_COORDINATOR
    assert sorted(area.lga.code for area in assignment.areas.all()) == ["KD-SOBA", "KD-ZARIA"]
    grant = DelegationGrant.objects.get(recipient=coordinator)
    assert grant.levels == ["lga", "ward", "pollingUnit"]
    assert sorted(area.lga.code for area in grant.areas.all()) == ["KD-SOBA", "KD-ZARIA"]

    # They appoint an LGA Coordinator in either of their LGAs, not outside them.
    cache.clear()
    login = APIClient().post(
        "/api/v1/auth/login/",
        {"username": "08030000041", "password": "08030000041"},
        format="json",
    )
    assert login.status_code == 200, login.content
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {login.json()['access']}")
    candidate = Member.objects.get(full_name="Anas Zubairu")

    def appoint(lga):
        return client.post(
            "/api/v1/access/assignments/",
            {
                "member": candidate.membership_number,
                "role": Role.LGA_COORDINATOR.value,
                "areas": [{"level": "lga", "lgaId": lga}],
            },
            format="json",
        )

    assert appoint("KD-KADUNA-NORTH").status_code == 403
    inside = appoint("KD-SOBA")
    assert inside.status_code == 201, inside.content

"""Importing a state chapter's officers (manage.py import_state_officers).

Names and numbers here are made up; real lists never go into tests.
"""

import io

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from rest_framework.test import APIClient

from apps.access.models import DelegationGrant, RoleAssignment, RoleDefinition
from apps.accounts.models import User
from apps.core.choices import GeographyLevel, Role, VerificationStatus
from apps.geography.models import State
from apps.geography.polling_units import load_polling_units
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
    load_polling_units(lga_codes=["KE-ARGUNGU", "KE-YAURI", "KD-ZARIA", "JI-BUJI", "JI-HADEJIA"])
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
    assert "line 4 (S/N 3): area not a senatorial district of this state" in report
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

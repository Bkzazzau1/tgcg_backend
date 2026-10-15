"""Importing a chapter's polling unit executives (manage.py import_executives).

Names and numbers here are made up; real lists never go into tests.
"""

import io

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from rest_framework.test import APIClient

from apps.access.models import AuditEvent, RoleAssignment, RoleDefinition
from apps.accounts.models import User
from apps.core.choices import GeographyLevel, Role
from apps.geography.models import State
from apps.geography.polling_units import load_polling_units
from apps.membership.management.commands.import_executives import PU_ROLES
from apps.membership.models import Member
from conftest import PASSWORD

pytestmark = pytest.mark.django_db

KAURA = "18-23-05"
ROWS = [
    # sn, name, rank, phone, polling unit
    ("1", "NAFI'U MUSA", "P.U ORG OFFICER", "08030000001", "001"),
    ("2", "ADA BELLO", "ASST P.U YOUTH", "08030000002", "1"),
    ("3", "HAUWA'U SANI", "P.U WOMEN COORDINATOR", "08030000003", "002"),
    ("4", "BALA DOGO", "ASST PU COORDINATOR", "07060000004", "002"),
    ("5", "SANI DOGO", "ASST PU COORDINATOR", "07060000004", "003"),  # row 4's number
    ("6", "AUDU KANO", "P.U ORG OFFICER", "08-30000006", "004"),  # 10 digits
    ("7", "LADI YARO", "ASST PU COORDINATOR", "08930000007", "004"),  # no such prefix
    ("8", "MUSA YARO", "TREASURER", "08030000008", "005"),  # not a PU rank
    ("9", "IDI GARBA", "P.U ORG OFFICER", "08030000009", "999"),  # no such unit
]


@pytest.fixture
def admin():
    load_polling_units(lga_codes=["KD-ZARIA"])
    return User.objects.create_user(
        "NA-1", PASSWORD, full_name="National Admin", role=Role.NATIONAL_ADMINISTRATOR
    )


def csv_file(tmp_path, rows=ROWS):
    path = tmp_path / "executives.csv"
    lines = ["sn,name,rank,phone,polling_unit"]
    lines += [",".join(f'"{value}"' for value in row) for row in rows]
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def run(path, *flags, by="NA-1"):
    out = io.StringIO()
    call_command(
        "import_executives",
        str(path),
        "--ward",
        KAURA,
        "--by",
        by,
        "--source",
        "Test chapter: polling unit executives list",
        *flags,
        stdout=out,
    )
    return out.getvalue()


def test_a_dry_run_reports_held_rows_by_number_and_saves_nothing(admin, tmp_path):
    report = run(csv_file(tmp_path))
    assert "3 to import, 6 held back." in report
    assert "line 5 (S/N 4): phone number shared with another row" in report
    assert "line 6 (S/N 5): phone number shared with another row" in report
    assert "line 7 (S/N 6): not a valid Nigerian mobile number" in report
    assert "line 8 (S/N 7): not a valid Nigerian mobile number" in report
    assert "line 9 (S/N 8): rank not recognised" in report
    assert "line 10 (S/N 9): polling unit not in this ward" in report
    # Nobody's name or number is printed.
    for _, name, _, phone, _ in ROWS:
        assert name.split()[0].lower() not in report.lower()
        assert phone not in report
    assert "Dry run" in report
    assert not Member.objects.exists()
    assert not RoleDefinition.objects.filter(name__in=PU_ROLES).exists()


def test_executives_are_imported_with_working_roles_and_their_phone_as_password(admin, tmp_path):
    report = run(csv_file(tmp_path), "--commit")
    assert "Imported 3 members with their roles." in report

    # All six polling unit roles exist, reporting to the Ward Coordinator.
    roles = RoleDefinition.objects.filter(name__in=PU_ROLES).select_related("supervising_role")
    assert roles.count() == 6
    assert {role.supervising_role.code for role in roles} == {Role.WARD_COORDINATOR.value}

    member = Member.objects.select_related("polling_unit").get(phone_number="+2348030000001")
    assert member.full_name == "Nafi'u Musa" and member.polling_unit.code == "18-23-05-001"
    assert member.registration_channel == "import" and member.consent_at
    # Checked manually by the chapter, on the administrator's word; never by SMS.
    assert member.verification_status == "verified" and member.verified_by == admin
    assert member.phone_verified_at is None
    assignment = RoleAssignment.objects.select_related("role").get(member=member)
    assert assignment.role.name == "PU Organising Officer"
    assert AuditEvent.objects.filter(action="member.imported").count() == 3

    # The first password is the phone number as written, and the role works.
    login = APIClient().post(
        "/api/v1/auth/login/",
        {"username": "08030000001", "password": "08030000001"},
        format="json",
    )
    assert login.status_code == 200, login.content
    capabilities = login.json()["user"]["capabilities"]
    for capability in (
        "createIncident",
        "submitElectionResult",
        "verifyMemberIdentity",
        "fieldDuty",
    ):
        assert capability in capabilities

    # Running again adds nobody twice.
    again = run(csv_file(tmp_path), "--commit")
    assert "0 to import" in again and "registered already" in again
    assert Member.objects.count() == 3


def test_only_a_national_administrator_imports(admin, tmp_path):
    User.objects.create_user(
        "SC-KD",
        PASSWORD,
        full_name="Coordinator",
        role=Role.STATE_COORDINATOR,
        scope_level=GeographyLevel.STATE,
        state=State.objects.get(code="KD"),
    )
    with pytest.raises(CommandError):
        run(csv_file(tmp_path), "--commit", by="SC-KD")
    assert not Member.objects.exists()

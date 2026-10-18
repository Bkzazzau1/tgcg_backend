"""Importing people from a list as members only (manage.py import_members).

Names and numbers here are made up; real lists never go into tests.
"""

import io

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from rest_framework.test import APIClient

from apps.access.models import AuditEvent, RoleAssignment
from apps.accounts.models import User
from apps.core.choices import GeographyLevel, RegistrationChannel, Role, VerificationStatus
from apps.geography.models import State
from apps.geography.polling_units import load_polling_units
from apps.membership.models import Member
from conftest import PASSWORD

pytestmark = pytest.mark.django_db

ROWS = [
    # sn, name, phone, place, hold
    ("Buji 1", "BALA MUSA", "08030000001", "17-05-02-001", ""),
    ("Buji 2", "ADA BELLO", "08030000002", "17-05-02", ""),
    ("Buji 3", "HAUWA SANI", "08030000003", "JI-BUJI", ""),
    ("Buji 4", "LADI YARO", "08030000004", "JI", ""),
    ("Buji 5", "AUDU KANO", "080300000055", "17-05-02", ""),  # 12 digits
    ("Buji 6", "IDI GARBA", "08030000006", "17-05-02", ""),
    ("Buji 7", "MUSA YARO", "08030000006", "17-05-02", ""),  # Buji 6's phone, another name
    ("Buji 8", "SANI DOGO", "08030000008", "17-05-02-001", ""),
    ("Buji 9", "SANI DOGO", "08030000008", "17-05-02-001", ""),  # listed again
    ("Buji 10", "YUSUF ALI", "08030000010", "17-05-02-001", ""),
    ("Buji 11", "YUSUF ALI", "08030000010", "17-05-02", ""),  # another place
    ("Buji 12", "UMAR BALA", "08030000012", "18-23-05", ""),  # a Kaduna ward
    ("Buji 13", "", "08030000013", "17-05-02", ""),
    ("Buji 14", "GARBA SALE", "08030000014", "17-05-02", "several names, one phone"),
]


@pytest.fixture
def admin():
    load_polling_units(lga_codes=["JI-BUJI", "KD-ZARIA"])
    return User.objects.create_user(
        "NA-1", PASSWORD, full_name="National Admin", role=Role.NATIONAL_ADMINISTRATOR
    )


def csv_file(tmp_path, rows=ROWS):
    path = tmp_path / "members.csv"
    lines = ["sn,name,phone,place,hold"]
    lines += [",".join(f'"{value}"' for value in row) for row in rows]
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def run(path, *flags, by="NA-1"):
    out = io.StringIO()
    call_command(
        "import_members",
        str(path),
        "--state",
        "JI",
        "--by",
        by,
        "--source",
        "Test chapter: polling unit agents list",
        *flags,
        stdout=out,
    )
    return out.getvalue()


def test_a_dry_run_reports_held_rows_by_number_and_saves_nothing(admin, tmp_path):
    report = run(csv_file(tmp_path))
    assert "5 to import, 9 held back. Placed at 2 polling units, 1 wards and 1 LGAs." in report
    assert "1 placed at state level." in report
    assert "line 6 (S/N Buji 5): not a valid Nigerian mobile number" in report
    assert "line 7 (S/N Buji 6): phone number shared with another row" in report
    assert "line 8 (S/N Buji 7): phone number shared with another row" in report
    assert "line 10 (S/N Buji 9): the same person listed again (imported once)" in report
    assert "line 11 (S/N Buji 10): the same person has another place on another row" in report
    assert "line 12 (S/N Buji 11): the same person has another place on another row" in report
    assert "line 13 (S/N Buji 12): place not in this state's INEC register" in report
    assert "line 14 (S/N Buji 13): no name" in report
    assert "line 15 (S/N Buji 14): held by the list: several names, one phone" in report
    # Never a name or a phone number in the report.
    assert "SANI" not in report.upper() and "0803" not in report
    assert not Member.objects.exists()


def test_people_become_members_with_no_post_and_unchecked_identity(admin, tmp_path):
    report = run(csv_file(tmp_path), "--commit")
    assert "Imported 5 members, with no post; their identity is not checked yet." in report

    members = {m.full_name: m for m in Member.objects.select_related("polling_unit")}
    assert set(members) == {"Bala Musa", "Ada Bello", "Hauwa Sani", "Ladi Yaro", "Sani Dogo"}
    assert members["Bala Musa"].scope_level == GeographyLevel.POLLING_UNIT
    assert members["Bala Musa"].polling_unit.code == "17-05-02-001"
    assert members["Ada Bello"].scope_level == GeographyLevel.WARD
    assert members["Hauwa Sani"].scope_level == GeographyLevel.LGA
    assert members["Ladi Yaro"].scope_level == GeographyLevel.STATE
    for member in members.values():
        assert member.registration_channel == RegistrationChannel.IMPORT
        assert member.consent_at is not None
        # The list's word covers consent only: a post waits for the usual check.
        assert member.verification_status != VerificationStatus.VERIFIED
        assert member.verified_by is None and member.verified_at is None
        assert member.state.code == "JI"
    assert not RoleAssignment.objects.exists()
    record = AuditEvent.objects.get(action="member.imported", target_label="Ada Bello")
    assert record.changes == {"identity": "not checked yet", "consent": "through the chapter"}

    # The phone number as written is the first password.
    login = APIClient().post(
        "/api/v1/auth/login/",
        {"username": "08030000003", "password": "08030000003"},
        format="json",
    )
    assert login.status_code == 200, login.content

    # Run again: those imported are held as registered; nothing is added twice.
    again = run(csv_file(tmp_path), "--commit")
    assert "0 to import, 14 held back." in again
    assert "line 3 (S/N Buji 2): phone number registered already" in again
    assert Member.objects.count() == 5


def test_only_a_national_administrator_imports(admin, tmp_path):
    User.objects.create_user(
        "SC-1",
        PASSWORD,
        full_name="Coordinator",
        role=Role.STATE_COORDINATOR,
        scope_level=GeographyLevel.STATE,
        state=State.objects.get(code="JI"),
    )
    with pytest.raises(CommandError):
        run(csv_file(tmp_path), "--commit", by="SC-1")
    assert not Member.objects.exists()

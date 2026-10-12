"""Joining TGCG by USSD (apps.membership.ussd)."""

import logging
import re
from types import SimpleNamespace

import pytest
from django.test import Client
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.core.choices import GeographyLevel, RegistrationChannel, Role, VerificationStatus
from apps.geography.models import State
from apps.geography.polling_units import load_polling_units
from apps.membership import ussd
from apps.membership.models import Member
from conftest import PASSWORD

pytestmark = pytest.mark.django_db

TOKEN = "t" * 40
CODE = "*384*1234#"
PHONE = "+2348031234567"
PIN = "482915"
VIN = "90F5B0E8D0295847263"
# Everything up to the data-use agreement: menu, name, DELIM, unit, gender,
# date of birth, VIN, PIN twice.
JOIN = ["1", "musa BELLO", "18*23*01*001", "1", "1", "14051990", VIN, PIN, PIN]


@pytest.fixture(autouse=True)
def setup(settings):
    settings.USSD_CALLBACK_TOKEN = TOKEN
    settings.USSD_SERVICE_CODE = CODE
    load_polling_units(lga_codes=["KD-ZARIA"])


def post(text="", *, session="ATUid_1", phone=PHONE, token=TOKEN, code=CODE):
    return Client().post(
        f"/api/v1/ussd/{token}/",
        {"sessionId": session, "serviceCode": code, "phoneNumber": phone, "text": text},
    )


def dial(*answers, session="ATUid_1", phone=PHONE):
    """One step of a session: everything typed so far, as the provider sends it."""
    response = post("*".join(answers), session=session, phone=phone)
    assert response.status_code == 200
    assert response["Content-Type"].startswith("text/plain")
    body = response.content.decode()
    assert body[:4] in ("CON ", "END ")
    assert len(body) - 4 <= ussd.SCREEN_LIMIT and body.isascii(), body
    return body


def test_joining_step_by_step():
    assert dial() == "CON Welcome to TGCG\n1. Join TGCG\n2. My membership"
    assert dial("1") == "CON Enter your full name as written on your PVC:"
    assert dial("1", "musa BELLO").startswith("CON Enter the DELIM code on your PVC")
    # The DELIM code one number at a time, each named back to the member...
    assert dial("1", "musa BELLO", "18").startswith("CON Kaduna State. Enter the 2nd number")
    assert dial("1", "musa BELLO", "18", "23").startswith("CON Zaria LGA. Enter the 3rd number")
    assert dial("1", "musa BELLO", "18", "23", "1").startswith("CON KWARBAI")
    # ...or all at once, with * between the numbers.
    unit = dial("1", "musa BELLO", "18*23*01*001")
    assert unit.startswith("CON Polling unit: ") and unit.endswith("1. Correct\n2. Enter it again")
    assert dial(*JOIN[:4]) == "CON Are you\n1. Male\n2. Female"
    assert "date of birth" in dial(*JOIN[:5])
    assert "VIN" in dial(*JOIN[:6])
    assert "6-digit PIN" in dial(*JOIN[:7])
    assert dial(*JOIN[:8]) == "CON Enter the same PIN again:"
    assert dial(*JOIN).startswith("CON TGCG keeps your details")

    done = dial(*JOIN, "1")
    assert re.fullmatch(
        r"END Welcome to TGCG, Musa! Your membership number is TGCG-\d{6}\. Sign in to the "
        r"TGCG app with this phone number and your PIN\.",
        done,
    )
    member = Member.objects.get(phone_number=PHONE)
    assert member.full_name == "Musa Bello"
    assert member.registration_channel == RegistrationChannel.USSD
    assert member.polling_unit.code == "18-23-01-001" and member.lga.code == "KD-ZARIA"
    assert (member.vin, member.gender, str(member.date_of_birth)) == (VIN, "male", "1990-05-14")
    assert member.phone_verified_at and member.consent_at
    assert not member.photo and not member.pvc_image
    assert member.verification_status == VerificationStatus.PENDING_REVIEW

    checks = {check.kind: check for check in member.verification_checks.all()}
    assert checks["phoneVerified"].result == "passed"
    assert "USSD" in checks["phoneVerified"].detail
    assert checks["pvcCaptured"].result == "pending"  # VIN typed, no photo of the card
    assert checks["selfieCaptured"].result == "failed"  # so no coordinator can verify yet

    login = APIClient().post(
        "/api/v1/auth/login/", {"username": "08031234567", "password": PIN}, format="json"
    )
    assert login.status_code == 200
    assert login.json()["user"]["role"] == "member"


def test_the_last_step_sent_again_joins_once():
    first = dial(*JOIN, "1")
    assert dial(*JOIN, "1") == first
    assert Member.objects.count() == 1


def test_optional_answers_can_be_skipped():
    dial("1", "Amina Yusuf", "18*23*01*001", "1", "2", "0", "0", PIN, PIN, "1")
    member = Member.objects.get(phone_number=PHONE)
    assert (member.gender, member.date_of_birth, member.vin) == ("female", None, None)
    checks = dict(member.verification_checks.values_list("kind", "detail"))
    assert checks["pvcCaptured"] == "Polling unit code given by USSD, without the VIN."


def test_wrong_answers_are_asked_again():
    name = ["1", "Musa Bello"]
    assert dial("1", "M").startswith("CON Enter at least two names, in letters.\n")
    assert dial("1", "Musa 4ello").startswith("CON Enter at least two names, in letters.\n")
    assert dial(*name, "99").startswith("CON No state has that number.\n")
    assert dial(*name, "18", "77").startswith("CON There is no LGA with that number.\n")
    assert dial(*name, "18", "23", "99").startswith("CON There is no ward with that number.\n")
    assert dial(*name, "18*23*1*999").startswith("CON There is no polling unit with that number.\n")
    # A wrong answer is asked again, and the next answer counts.
    assert dial(*name, "99", "18").startswith("CON Kaduna State.")
    # The member can enter the code again if the unit named is not theirs.
    assert dial(*name, "18*23*01*001", "2").startswith("CON Enter the DELIM code")

    unit = [*name, "18*23*01*001", "1"]
    assert dial(*unit, "3").startswith("CON Choose 1 or 2.\n")
    assert dial(*unit, "1", "31022000").startswith("CON Use 8 digits: day, month, year.\n")
    assert dial(*unit, "1", "14052020").startswith("CON Check your date of birth.\n")
    assert dial(*unit, "1", "0", "ABC").startswith("CON A VIN has 19 letters and digits.\n")
    assert dial(*unit, "1", "0", "0", "123456").startswith(
        "CON This PIN is too easy to guess. Choose another.\n"
    )
    assert dial(*unit, "1", "0", "0", PIN, "482916").startswith(
        "CON The PINs did not match.\nChoose a 6-digit PIN"
    )
    assert dial(*JOIN, "5").startswith("CON Choose 1 or 2.\n")
    assert not Member.objects.exists()


def test_cancelling_joins_nobody():
    assert dial(*JOIN, "2") == "END You have not joined TGCG. Dial again when you are ready."
    assert not Member.objects.exists()


def test_a_number_or_vin_already_in_use_cannot_join_again():
    dial(*JOIN, "1")
    number = Member.objects.get().membership_number
    assert dial("1", session="ATUid_2") == (
        f"END This number is already a TGCG member ({number}). "
        "Dial again and choose 2 to see your membership."
    )
    other = "+2348039998888"
    assert dial(*JOIN[:6], VIN, session="ATUid_3", phone=other).startswith(
        "CON That VIN is already registered.\n"
    )
    User.objects.create_user(
        "SC-KD",
        PASSWORD,
        full_name="State Coordinator",
        phone_number="+2348035550000",
        role=Role.STATE_COORDINATOR,
        scope_level=GeographyLevel.STATE,
        state=State.objects.get(code="KD"),
    )
    assert dial("1", session="ATUid_4", phone="+2348035550000").startswith(
        "END This number belongs to a TGCG staff account"
    )


def test_my_membership():
    assert dial("2") == (
        "END This number is not a TGCG member yet. Dial again and choose 1 to join."
    )
    dial(*JOIN, "1")
    Member.objects.update(full_name="Musa " + "Abdullahi " * 14 + "Bello")  # very long
    screen = dial("2", session="ATUid_2")
    assert screen.startswith("END Musa Abdullahi")
    assert "\nMember no. TGCG-" in screen
    assert screen.endswith("\nStatus: awaiting review")


def test_the_callback_is_closed_without_the_right_token(settings):
    assert post(token="wrong" * 8).status_code == 404
    assert Client().get(f"/api/v1/ussd/{TOKEN}/").status_code == 405
    settings.USSD_CALLBACK_TOKEN = ""
    assert post().status_code == 404


def test_other_codes_numbers_and_broken_requests_are_refused():
    assert post(code="*123#").content == b"END This service is not available."
    assert post(session="").content == b"END This service is not available."
    assert post(phone="+447700900123").content == (
        b"END TGCG membership by USSD is for Nigerian mobile numbers."
    )


def test_every_screen_fits_on_a_phone():
    # Each question with the longest error it can show, and very long names.
    longest_error = {
        "menu": "Choose 1 or 2.",
        "name": "Enter at least two names, in letters.",
        "state": "No state has that number.",
        "lga": "There is no LGA with that number.",
        "ward": "There is no ward with that number.",
        "unit": "There is no polling unit with that number.",
        "confirm_unit": "Choose 1 or 2.",
        "gender": "Choose 1 or 2.",
        "born": "Use 8 digits: day, month, year.",
        "vin": "A VIN has 19 letters and digits.",
        "pin": "This PIN is too easy to guess. Choose another.",
        "pin_again": "",
        "agree": "Choose 1 or 2.",
    }
    session = ussd._Session(PHONE, "ATUid_1")
    session.state_name = "Federal Capital Territory"
    session.lga = session.ward = session.unit = SimpleNamespace(name="W" * 300)
    for step, error in longest_error.items():
        session.step, session.error = step, error
        text = session.prompt().text
        assert len(text) <= ussd.SCREEN_LIMIT and text.isascii(), step


def test_nothing_typed_reaches_the_logs(caplog, monkeypatch):
    caplog.set_level(logging.DEBUG)
    dial(*JOIN, "1")

    def broken(**kwargs):
        raise RuntimeError(f"failed for {kwargs['text']}")

    monkeypatch.setattr(ussd, "respond", broken)
    assert dial(*JOIN, "1", session="ATUid_2") == (
        "END Sorry, TGCG could not finish this. Please try again later."
    )
    assert "USSD request failed: RuntimeError" in caplog.text
    assert PIN not in caplog.text
    assert "8031234567" not in caplog.text

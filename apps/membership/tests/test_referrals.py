"""Referral codes (25 October 2026): a member's 7-digit code and link; joining
with it earns the member registration credits."""

import re

import pytest
from rest_framework.test import APIClient

from apps.access.tests.test_access import client_for, member_account
from apps.accounts.models import User
from apps.core.choices import MembershipState, Role
from apps.geography.polling_units import load_polling_units
from apps.membership.models import Member
from apps.membership.tests.test_ussd import CODE as USSD_CODE
from apps.membership.tests.test_ussd import PIN as USSD_PIN
from apps.membership.tests.test_ussd import TOKEN as USSD_TOKEN
from apps.membership.tests.test_ussd import dial
from apps.membership.tests.test_ussd import join as ussd_join
from conftest import PASSWORD

# A refused registration marks the transaction for rollback (DRF), so these
# tests use real transactions, as the registration tests do.
pytestmark = pytest.mark.django_db(transaction=True, serialized_rollback=True)

VIN = "90F5B0E8D0295847263"
PIN = "482915"


@pytest.fixture(autouse=True)
def setup(settings):
    settings.REGISTRATION_PHONE_CODE_REQUIRED = False
    settings.USSD_CALLBACK_TOKEN = USSD_TOKEN
    settings.USSD_SERVICE_CODE = USSD_CODE
    settings.USSD_DRAFT_TTL_DAYS = 30
    load_polling_units(lga_codes=["KD-ZARIA"])


@pytest.fixture
def inviter():
    return member_account("08030000001", name="Ada Obi")


@pytest.fixture
def admin():
    return User.objects.create_user(
        "NA-1", PASSWORD, full_name="National Admin", role=Role.NATIONAL_ADMINISTRATOR
    )


def my_referral(member):
    response = client_for(member.user).get("/api/v1/me/referral/")
    assert response.status_code == 200, response.content
    return response.json()


def join(**overrides):
    data = {
        "phone_number": "08031234567",
        "vin": VIN,
        "delim": "18/23/01/001",
        "full_name": "Musa Bello",
        "pin": PIN,
        "consent": True,
        **overrides,
    }
    return APIClient().post("/api/v1/register/", data, format="multipart")


def test_a_member_has_one_code_and_a_link(inviter):
    first = my_referral(inviter)
    assert re.fullmatch(r"[1-9]\d{6}", first["code"])
    assert first["link"] == f"https://app.tgcg.online/?ref={first['code']}"
    assert first["joined"] == first["credited"] == 0
    assert my_referral(inviter) == first  # the same code every time


def test_joining_with_a_code_credits_the_inviter_at_once(inviter, admin):
    code = my_referral(inviter)["code"]
    response = join(referral_code=f" {code} ")
    assert response.status_code == 201, response.content
    joined = Member.objects.get(vin=VIN)
    assert joined.referred_by == inviter.user

    wallet = client_for(inviter.user).get("/api/v1/me/wallet/").json()
    assert wallet["credits"] == 1
    assert wallet["registrations"] == {
        "registered": 1,
        "with_code": 1,
        "signed_in": 1,  # joining in the app signs the member in
        "waiting": 0,
        "credited": 1,
    }
    assert my_referral(inviter)["joined"] == my_referral(inviter)["credited"] == 1
    row = client_for(admin).get("/api/v1/wallets/registrars/").json()["results"][0]
    assert (row["full_name"], row["with_code"], row["credited"]) == ("Ada Obi", 1, 1)
    people = client_for(admin).get(
        f"/api/v1/wallets/registrars/{inviter.user.access_id}/registered/"
    )
    assert people.json()["results"][0]["membership_number"] == joined.membership_number

    # Signing in later earns nothing more.
    login = APIClient().post(
        "/api/v1/auth/login/", {"username": "08031234567", "password": PIN}, format="json"
    )
    assert login.status_code == 200
    assert client_for(inviter.user).get("/api/v1/me/wallet/").json()["credits"] == 1


def test_a_wrong_code_stops_joining_and_says_why(inviter):
    response = join(referral_code="1234567")
    assert response.status_code == 400
    assert "referral_code" in response.json(), response.json()
    assert not Member.objects.filter(vin=VIN).exists()


def test_a_suspended_members_code_does_not_work(inviter):
    code = my_referral(inviter)["code"]
    Member.objects.filter(pk=inviter.pk).update(membership_state=MembershipState.SUSPENDED)
    assert join(referral_code=code).status_code == 400
    assert client_for(inviter.user).get("/api/v1/me/referral/").status_code == 403


def test_joining_without_a_code_is_unchanged(inviter):
    assert join().status_code == 201
    assert Member.objects.get(vin=VIN).referred_by is None


def test_a_member_who_joined_by_ussd_gives_the_code_afterwards_once(inviter):
    code = my_referral(inviter)["code"]
    assert "3. Referral code" in dial()
    member = ussd_join()

    assert dial("3", session="r1").startswith("CON Enter the 7-digit referral code")
    assert "not found" in dial("3", "7654321" if code != "7654321" else "7654322", session="r2")
    assert dial("3", code, session="r3") == "END Thank you. Your referral code is recorded."
    member.refresh_from_db()
    assert member.referred_by == inviter.user
    assert "already recorded" in dial("3", session="r4")

    # The inviter earns once the new member first signs in to the app.
    answers = ["1", "Musa Bello", USSD_PIN, USSD_PIN]
    for count in range(1, len(answers) + 1):  # one screen at a time, as on a phone
        dial(*answers[:count], session="profile")
    assert client_for(inviter.user).get("/api/v1/me/wallet/").json()["credits"] == 0
    login = APIClient().post(
        "/api/v1/auth/login/", {"username": "08031234567", "password": USSD_PIN}, format="json"
    )
    assert login.status_code == 200
    assert client_for(inviter.user).get("/api/v1/me/wallet/").json()["credits"] == 1


def test_ussd_referral_codes_are_for_members_who_joined_by_ussd(inviter):
    assert "Join TGCG first" in dial("3", session="r1")
    code = my_referral(inviter)["code"]
    assert "given when joining" in dial("3", code, session="r2", phone="+2348030000001")

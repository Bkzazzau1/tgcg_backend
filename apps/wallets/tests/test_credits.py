"""Credits for registering members (23 October 2026): whoever registered a
member at /me/registered/ earns them at that member's first sign-in, and
converts them to naira at the rate a National Administrator sets."""

import re
from unittest.mock import patch

import pytest
from rest_framework.test import APIClient
from rest_framework_simplejwt.token_blacklist.models import OutstandingToken

from apps.access.models import AuditEvent
from apps.access.tests.test_access import client_for, member_account
from apps.membership.models import Member
from apps.wallets.models import Wallet, WalletEntry
from conftest import PASSWORD

pytestmark = pytest.mark.django_db

REGISTERED = "/api/v1/me/registered/"
WALLET = "/api/v1/me/wallet/"
RATES = "/api/v1/wallets/credit-rates/"


@pytest.fixture
def registrant():
    """An ordinary verified member who registers people."""
    return member_account("08030000001", name="Ada Obi")


@pytest.fixture
def outbox(monkeypatch):
    sent = []
    monkeypatch.setattr(
        "apps.accounts.verification.send_sms", lambda phone, text: sent.append((phone, text))
    )
    return sent


def register(client, phone, name="Sani Umar"):
    body = {"full_name": name, "phone_number": phone, "consent": True}
    response = client.post(REGISTERED, body, format="json")
    assert response.status_code == 201, response.content
    return response.json()


def sign_in(username, password=None):
    """The first password of a member registered by someone is their phone number."""
    body = {"username": username, "password": password or username}
    response = APIClient().post("/api/v1/auth/login/", body, format="json")
    assert response.status_code == 200, response.content
    return response


def wallet(member):
    return client_for(member.user).get(WALLET).json()


def test_the_registrar_earns_credits_at_the_members_first_sign_in(registrant, finance):
    number = register(client_for(registrant.user), "08051234567")["membership_number"]
    assert wallet(registrant)["credits"] == 0  # not until the member signs in

    sign_in("08051234567")
    mine = wallet(registrant)
    assert mine["credits"] == 1 and mine["credits_worth"] == "100.00"
    assert mine["balance"] == "0.00"
    # The starting rates, until an administrator sets any.
    assert mine["rate"] == {
        "credits_per_registration": 1,
        "naira_per_credit": "100.00",
        "min_withdrawal": "1000.00",
        "reason": "",
        "set_by": None,
        "created_at": None,
    }
    entries = [
        (e["kind"], e["amount"], e["credits"], e["credits_after"], e["purpose"])
        for e in mine["entries"]
    ]
    assert entries == [("reward", "0.00", 1, 1, "Registered Sani Umar")]

    # Once per member, however often they sign in.
    sign_in("08051234567")
    assert wallet(registrant)["credits"] == 1
    event = AuditEvent.objects.get(action="wallet.creditsEarned")
    assert event.actor is None and event.target_id == registrant.membership_number
    assert event.changes == {
        "credits": 1,
        "credits_after": 1,
        "registered": number,
        "proof": "signIn",
    }
    # Staff see whom by membership number only.
    staff = finance["kaduna"].get(f"/api/v1/wallets/{registrant.membership_number}/").json()
    assert staff["credits"] == 1
    assert staff["entries"][0]["purpose"] == f"Registered {number}"


def test_only_members_registered_by_a_member_earn(registrant, lga_coordinator):
    # A member who joined another way.
    other = member_account("08030000002", name="Bala Musa")
    sign_in(other.membership_number, PASSWORD)
    # A staff account without a membership has no wallet to earn into.
    register(client_for(lga_coordinator), "08051234568")
    sign_in("08051234568")
    assert not WalletEntry.objects.exists()


def test_credits_wait_for_the_earners_verification_to_be_converted():
    waiting = member_account("08030000003", name="Musa Bello", verified=False)
    register(client_for(waiting.user), "08051234569")
    sign_in("08051234569")
    mine = wallet(waiting)
    assert mine["has_wallet"] is False and mine["credits"] == 1
    refused = client_for(waiting.user).post(f"{WALLET}convert/", {"credits": 1}, format="json")
    assert refused.status_code == 400
    assert wallet(waiting)["credits"] == 1


def test_with_sms_working_only_a_member_who_proves_the_phone_earns(registrant, outbox, settings):
    settings.CREDITS_EARNED_ON = "smsCode"
    register(client_for(registrant.user), "08051234570")
    sign_in("08051234570")  # the registrar knows this password
    assert wallet(registrant)["credits"] == 0

    member_app = APIClient()
    phone = {"phone_number": "08051234570"}
    assert member_app.post("/api/v1/register/pin-code/", phone, format="json").status_code == 200
    code = re.search(r"\b(\d{6})\b", outbox[-1][1]).group(1)
    pin = {**phone, "code": code, "pin": "739104"}
    assert member_app.post("/api/v1/register/pin/", pin, format="json").status_code == 200
    assert wallet(registrant)["credits"] == 1
    assert AuditEvent.objects.get(action="wallet.creditsEarned").changes["proof"] == "smsCode"
    assert Member.objects.get(phone_number="+2348051234570").phone_verified_at is not None


def test_a_national_administrator_sets_the_rates(registrant, admin, finance):
    rate = {
        "credits_per_registration": 5,
        "naira_per_credit": "50",
        "min_withdrawal": "2000",
        "reason": "Registration drive",
    }
    assert finance["kaduna"].get(RATES).status_code == 200
    assert finance["kaduna"].post(RATES, rate, format="json").status_code == 403
    assert client_for(registrant.user).get(RATES).status_code == 403
    no_reason = client_for(admin).post(RATES, {**rate, "reason": " "}, format="json")
    assert no_reason.status_code == 400

    set_ = client_for(admin).post(RATES, rate, format="json")
    assert set_.status_code == 201, set_.content
    assert set_.json()["set_by"] == "NA-1" and set_.json()["naira_per_credit"] == "50.00"
    event = AuditEvent.objects.get(action="wallet.creditRatesSet")
    assert event.changes == {
        "from": {
            "credits_per_registration": 1,
            "naira_per_credit": "100.00",
            "min_withdrawal": "1000.00",
        },
        "to": {
            "credits_per_registration": 5,
            "naira_per_credit": "50.00",
            "min_withdrawal": "2000.00",
        },
    }

    register(client_for(registrant.user), "08051234571")
    sign_in("08051234571")
    mine = wallet(registrant)
    assert mine["credits"] == 5 and mine["credits_worth"] == "250.00"

    # Nought pauses the incentive.
    paused = {**rate, "credits_per_registration": 0, "reason": "Drive over"}
    assert client_for(admin).post(RATES, paused, format="json").status_code == 201
    register(client_for(registrant.user), "08051234572", name="Ali Garba")
    sign_in("08051234572")
    assert wallet(registrant)["credits"] == 5
    rates = finance["kaduna"].get(RATES).json()
    assert rates["current"]["credits_per_registration"] == 0
    assert [r["reason"] for r in rates["history"]] == ["Drive over", "Registration drive"]


def test_the_member_converts_credits_to_naira_at_the_days_rate(registrant):
    client = client_for(registrant.user)
    for n in range(3):
        register(client, f"0805123458{n}", name=f"Member {n}")
        sign_in(f"0805123458{n}")
    assert wallet(registrant)["credits"] == 3

    convert = f"{WALLET}convert/"
    assert client.post(convert, {"credits": 0}, format="json").status_code == 400
    too_many = client.post(convert, {"credits": 4}, format="json")
    assert too_many.status_code == 400 and "You have 3 credits." in str(too_many.json())

    converted = client.post(convert, {"credits": 2}, format="json")
    assert converted.status_code == 200, converted.content
    mine = converted.json()
    assert mine["credits"] == 1 and mine["balance"] == "200.00"
    latest = mine["entries"][0]
    assert (latest["kind"], latest["amount"], latest["credits"], latest["purpose"]) == (
        "conversion",
        "200.00",
        -2,
        "Converted 2 credits",
    )
    assert AuditEvent.objects.get(action="wallet.creditsConverted").changes == {
        "credits": 2,
        "naira_per_credit": "100.00",
        "amount": "200.00",
        "balance": "200.00",
    }



@pytest.mark.django_db(transaction=True, serialized_rollback=True)
def test_a_reward_failure_rolls_back_sign_in_and_the_retry_earns_once(registrant):
    number = register(client_for(registrant.user), "08051234591")["membership_number"]
    member = Member.objects.select_related("user").get(membership_number=number)
    body = {"username": "08051234591", "password": "08051234591"}
    before_tokens = OutstandingToken.objects.filter(user=member.user).count()
    with patch("apps.wallets.services._audit", side_effect=RuntimeError("Synthetic audit failure")):
        failed = APIClient().post("/api/v1/auth/login/", body, format="json")
    assert failed.status_code == 503
    assert "access" not in failed.json() and "refresh" not in failed.json()
    member.user.refresh_from_db()
    assert member.user.last_login is None
    assert not Wallet.objects.filter(member=registrant).exists()
    assert not WalletEntry.objects.filter(registered_member=member).exists()
    assert OutstandingToken.objects.filter(user=member.user).count() == before_tokens

    retried = APIClient().post("/api/v1/auth/login/", body, format="json")
    assert retried.status_code == 200, retried.content
    assert "access" in retried.json() and "refresh" in retried.json()
    member.user.refresh_from_db()
    assert member.user.last_login is not None
    assert wallet(registrant)["credits"] == 1
    sign_in("08051234591")
    assert wallet(registrant)["credits"] == 1
    assert WalletEntry.objects.filter(registered_member=member).count() == 1


def test_a_paused_first_sign_in_does_not_earn_after_the_rate_resumes(registrant, admin):
    settings = {
        "credits_per_registration": 0,
        "naira_per_credit": "100",
        "min_withdrawal": "1000",
        "reason": "Synthetic pause",
    }
    assert client_for(admin).post(RATES, settings, format="json").status_code == 201
    number = register(client_for(registrant.user), "08051234592")["membership_number"]
    sign_in("08051234592")
    member = Member.objects.select_related("user").get(membership_number=number)
    assert member.user.last_login is not None
    assert wallet(registrant)["credits"] == 0

    resumed = {**settings, "credits_per_registration": 1, "reason": "Synthetic resume"}
    assert client_for(admin).post(RATES, resumed, format="json").status_code == 201
    sign_in("08051234592")
    assert wallet(registrant)["credits"] == 0
    assert not WalletEntry.objects.filter(registered_member=member).exists()

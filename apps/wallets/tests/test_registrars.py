"""The registrars list (24 October 2026): who registers members, how many,
how many have signed in (each first sign-in earns a credit), and the one-off
credit for members who signed in before credits started."""

from io import StringIO

import pytest
from django.core.management import CommandError, call_command
from django.utils import timezone

from apps.access.models import AuditEvent
from apps.access.tests.test_access import client_for, member_account
from apps.accounts.models import User
from apps.membership.models import Member
from apps.wallets.models import CreditRate, WalletEntry

from .test_credits import register, sign_in, wallet

pytestmark = pytest.mark.django_db

REGISTRARS = "/api/v1/wallets/registrars/"


@pytest.fixture
def registrars():
    """A Kaduna registrar with three people (one signed in), a Kano registrar
    with one (not yet)."""
    kaduna = member_account("08030000001", name="Ada Obi")
    kano = member_account("08030000020", name="Kano Registrar", lga="KN-AJINGI")
    for phone in ("08051230001", "08051230002", "08051230003"):
        register(client_for(kaduna.user), phone)
    register(client_for(kano.user), "08051230004")
    sign_in("08051230002")
    return {"kaduna": kaduna, "kano": kano}


def rows(client, query=""):
    response = client.get(REGISTRARS + query)
    assert response.status_code == 200, response.content
    return response.json()["results"]


def counts(row):
    keys = ("registered", "signed_in", "waiting", "credited", "credits_earned", "credits")
    return {key: row[key] for key in keys}


def test_the_national_administrator_sees_every_registrar_most_registered_first(registrars, admin):
    listed = rows(client_for(admin))
    assert [row["full_name"] for row in listed] == ["Ada Obi", "Kano Registrar"]
    ada = listed[0]
    assert counts(ada) == {
        "registered": 3,
        "signed_in": 1,
        "waiting": 2,
        "credited": 1,
        "credits_earned": 1,
        "credits": 1,
    }
    assert ada["membership_number"] == registrars["kaduna"].membership_number
    assert ada["identity_verified"] is True
    assert ada["scope"]["lgaId"] == "KD-KADUNA-NORTH"
    assert ada["last_registered_at"]
    assert counts(listed[1])["waiting"] == 1

    summary = client_for(admin).get(REGISTRARS + "summary/").json()
    assert summary == {
        "registrars": 2,
        "registered": 4,
        "with_code": 0,
        "signed_in": 1,
        "waiting": 3,
        "credited": 1,
    }
    # Most waiting first; one state; a name.
    assert rows(client_for(admin), "?ordering=-waiting")[0]["full_name"] == "Ada Obi"
    assert [row["full_name"] for row in rows(client_for(admin), "?state=KN")] == ["Kano Registrar"]
    assert [row["full_name"] for row in rows(client_for(admin), "?search=kano")] == [
        "Kano Registrar"
    ]


def test_finance_staff_see_the_registrars_in_their_area_only(registrars, finance):
    assert [row["full_name"] for row in rows(finance["kaduna"])] == ["Ada Obi"]
    assert [row["full_name"] for row in rows(finance["kano"])] == ["Kano Registrar"]
    assert finance["kano"].get(REGISTRARS + "summary/").json()["registered"] == 1
    # Someone else's registrar is not there to open.
    ada = registrars["kaduna"].membership_number
    assert finance["kano"].get(f"{REGISTRARS}{ada}/registered/").status_code == 404


def test_members_without_a_wallet_power_do_not_see_it(registrars):
    assert client_for(registrars["kaduna"].user).get(REGISTRARS).status_code == 403


def test_a_registrars_people_show_who_signed_in_and_who_earned_a_credit(registrars, finance):
    ada = registrars["kaduna"].membership_number
    response = finance["kaduna"].get(f"{REGISTRARS}{ada}/registered/")
    assert response.status_code == 200
    people = response.json()["results"]
    assert len(people) == 3  # registered without a place: they go with their registrar
    signed = [person for person in people if person["signed_in"]]
    assert len(signed) == 1 and signed[0]["credited"] and signed[0]["last_sign_in"]
    assert "phone_number" not in people[0]
    waiting = finance["kaduna"].get(f"{REGISTRARS}{ada}/registered/?signed_in=false").json()
    assert waiting["count"] == 2
    assert not any(person["credited"] for person in waiting["results"])


def test_people_placed_in_another_area_stay_with_that_area(registrars, finance):
    ada = registrars["kaduna"]
    placed = Member.objects.filter(created_by=ada.user).first()
    elsewhere = member_account("08030000030", name="Placed Elsewhere", lga="KN-AJINGI")
    placed.scope_level, placed.state, placed.lga = "lga", elsewhere.state, elsewhere.lga
    placed.save()
    people = finance["kaduna"].get(f"{REGISTRARS}{ada.membership_number}/registered/").json()
    assert people["count"] == 2
    # The registrar's own count still includes them.
    assert rows(finance["kaduna"])[0]["registered"] == 3


def test_a_registrars_wallet_says_how_many_are_still_to_sign_in(registrars):
    assert wallet(registrars["kaduna"])["registrations"] == {
        "registered": 3,
        "with_code": 0,
        "signed_in": 1,
        "waiting": 2,
        "credited": 1,
    }


def test_registrations_signed_in_before_credits_started_are_credited_once(
    registrars, lga_coordinator
):
    ada = registrars["kaduna"]
    # Two of Ada's people signed in before credits existed: no credit then.
    early = Member.objects.filter(created_by=ada.user, registration_reward__isnull=True)[:2]
    User.objects.filter(member__in=list(early)).update(last_login=timezone.now())
    # A staff account without a membership has no wallet to credit.
    register(client_for(lga_coordinator), "08051230005")
    User.objects.filter(member__phone_number__endswith="51230005").update(last_login=timezone.now())

    out = StringIO()
    call_command("credit_past_registrations", stdout=out)
    assert "3 signed in without a credit; 2 to credit (2 credits) to 1 registrars" in out.getvalue()
    assert "1 skipped" in out.getvalue()
    assert WalletEntry.objects.count() == 1  # a dry run

    out = StringIO()
    call_command("credit_past_registrations", "--commit", stdout=out)
    assert "Credited 2 registrations." in out.getvalue()
    assert wallet(ada)["credits"] == 3
    backfilled = AuditEvent.objects.filter(action="wallet.creditsEarned", changes__backfill=True)
    assert backfilled.count() == 2

    out = StringIO()
    call_command("credit_past_registrations", "--commit", stdout=out)
    assert "Credited 0 registrations." in out.getvalue()
    assert wallet(ada)["credits"] == 3


def test_the_catch_up_refuses_if_credits_were_ever_paused(registrars, admin):
    CreditRate.objects.create(
        credits_per_registration=0,
        kobo_per_credit=10000,
        min_withdrawal_kobo=100000,
        reason="Paused",
        set_by=admin,
    )
    with pytest.raises(CommandError, match="paused"):
        call_command("credit_past_registrations", "--commit", stdout=StringIO())

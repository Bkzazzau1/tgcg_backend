"""Withdrawals: naira leave the wallet when asked for, a second person
approves, and Paystack pays them into the member's own account."""

import hashlib
import hmac
import io
import json
import urllib.request
from types import SimpleNamespace

import pytest
from django.core.management import call_command
from django.test import Client
from django.urls import resolve

from apps.access.models import AuditEvent
from apps.access.tests.test_access import client_for, member_account
from apps.wallets import paystack
from apps.wallets.models import Withdrawal
from apps.wallets.services import names_match

# Approving runs outside the request transaction, so these tests use real ones.
pytestmark = pytest.mark.django_db(transaction=True, serialized_rollback=True)

SECRET = "sk_test_secret"
MINE = "/api/v1/me/wallet/withdrawals/"
STAFF = "/api/v1/withdrawals/"
ACCOUNT = "8012345678"


@pytest.fixture
def bank(settings, monkeypatch):
    """Paystack as the tests need it: whose each account is, how a transfer
    goes, and what was sent."""
    settings.PAYSTACK_SECRET_KEY = SECRET
    fake = SimpleNamespace(
        names={ACCOUNT: "SANI HAUWA"},
        status="pending",
        refuse=None,
        unreachable=False,
        transfers=[],
        known={},
    )

    def resolve_account(account_number, bank_code):
        if account_number not in fake.names:
            raise paystack.PaystackError("Could not resolve account name.", status=422)
        return fake.names[account_number]

    def create_recipient(*, name, account_number, bank_code):
        return f"RCP_{bank_code}_{account_number}"

    def transfer(*, amount_kobo, recipient, reference, reason):
        if fake.unreachable:
            raise paystack.Unreachable("Paystack could not be reached.")
        if fake.refuse:
            raise paystack.PaystackError(fake.refuse, status=400)
        fake.transfers.append((amount_kobo, recipient, reference))
        return {"status": fake.status, "transfer_code": "TRF_1", "reference": reference}

    def verify_transfer(reference):
        if reference not in fake.known:
            raise paystack.PaystackError("Transfer not found", status=404)
        return {"status": fake.known[reference], "reference": reference}

    for name, function in [
        ("resolve_account", resolve_account),
        ("create_recipient", create_recipient),
        ("transfer", transfer),
        ("verify_transfer", verify_transfer),
    ]:
        monkeypatch.setattr(paystack, name, function)
    return fake


@pytest.fixture
def payee(finance):
    """A verified member in Kaduna with ₦5,000 and an OPay account."""
    member = member_account("08035550050", name="Hauwa Sani")
    client = client_for(member.user)
    account = {"bank": "opay", "account_number": ACCOUNT}
    assert client.put("/api/v1/me/wallet/bank-account/", account, format="json").status_code == 200
    payment = {"member": member.membership_number, "amount": "5000", "purpose": "PU agent pay"}
    body = finance["kaduna"].post("/api/v1/wallet-payments/", payment, format="json").json()
    finance["kaduna2"].post(f"/api/v1/wallet-payments/{body['id']}/approve/", {}, format="json")
    member.client = client
    return member


def balance(member):
    return member.client.get("/api/v1/me/wallet/").json()["balance"]


def withdraw(member, amount="3000"):
    return member.client.post(MINE, {"amount": amount}, format="json")


def event(kind, withdrawal, **data):
    body = json.dumps(
        {
            "event": kind,
            "data": {
                "reference": withdrawal.reference,
                "amount": withdrawal.amount_kobo,
                "status": kind.split(".")[1],
                **data,
            },
        }
    ).encode()
    signature = hmac.new(SECRET.encode(), body, hashlib.sha512).hexdigest()
    return Client().post(
        "/api/v1/paystack/webhook/",
        data=body,
        content_type="application/json",
        headers={"X-Paystack-Signature": signature},
    )


def test_a_member_withdraws_to_their_own_account_through_paystack(payee, finance, bank):
    asked = withdraw(payee)
    assert asked.status_code == 201, asked.content
    body = asked.json()
    assert body["status"] == "pending" and body["amount"] == "3000.00"
    assert body["account_number"] == ACCOUNT and body["can_cancel"] is True
    # The money leaves the wallet at once; one withdrawal at a time.
    assert balance(payee) == "2000.00"
    entry = payee.client.get("/api/v1/me/wallet/").json()["entries"][0]
    assert (entry["kind"], entry["amount"], entry["purpose"]) == (
        "withdrawal",
        "-3000.00",
        "Withdrawal to OPay ******5678",
    )
    assert withdraw(payee, "500").status_code == 400

    # Staff in the member's area see it, with the account number masked.
    seen = finance["kaduna"].get(f"{STAFF}?status=pending").json()["results"]
    assert [w["id"] for w in seen] == [body["id"]]
    assert seen[0]["account_number"] == "******5678" and seen[0]["can_decide"] is True
    assert finance["kano"].get(STAFF).json()["results"] == []
    url = f"{STAFF}{body['id']}/"
    assert finance["kano"].post(f"{url}approve/", {}, format="json").status_code == 404
    assert payee.client.post(f"{url}approve/", {}, format="json").status_code == 403

    approved = finance["kaduna"].post(f"{url}approve/", {}, format="json")
    assert approved.status_code == 200, approved.content
    assert approved.json()["status"] == "processing"
    assert approved.json()["bank_account_name"] == "SANI HAUWA"
    withdrawal = Withdrawal.objects.get()
    assert bank.transfers == [(300000, f"RCP_999992_{ACCOUNT}", withdrawal.reference)]
    assert withdrawal.reference.startswith("tgcg-wd-") and 16 <= len(withdrawal.reference) <= 50

    assert event("transfer.success", withdrawal).status_code == 200
    assert event("transfer.success", withdrawal).status_code == 200  # Paystack retries
    withdrawal.refresh_from_db()
    assert withdrawal.status == "paid" and withdrawal.finished_at is not None
    assert balance(payee) == "2000.00"
    actions = AuditEvent.objects.filter(action__startswith="wallet.withdrawal")
    assert sorted(actions.values_list("action", flat=True)) == [
        "wallet.withdrawalApproved",
        "wallet.withdrawalPaid",
        "wallet.withdrawalRequested",
    ]
    assert AuditEvent.objects.get(action="wallet.withdrawalRequested").changes["account"] == (
        "******5678"
    )


def test_withdrawals_that_are_refused(payee, finance):
    assert withdraw(payee, "999").status_code == 400  # below the ₦1,000 minimum
    over = withdraw(payee, "5000.01")
    assert over.status_code == 400 and "Your balance is ₦5000.00." in str(over.json())

    # No account named yet, or a bank Paystack does not pay.
    other = member_account("08035550051", name="Ali Garba")
    client = client_for(other.user)
    assert client.post(MINE, {"amount": "1000"}, format="json").status_code == 400
    lapo = {"bank": "lapo", "account_number": "1234567890"}
    assert client.put("/api/v1/me/wallet/bank-account/", lapo, format="json").status_code == 200
    refused = client.post(MINE, {"amount": "1000"}, format="json")
    assert refused.status_code == 400 and "LAPO" in str(refused.json())

    unverified = member_account("08035550052", name="Bala Idris", verified=False)
    response = client_for(unverified.user).post(MINE, {"amount": "1000"}, format="json")
    assert response.status_code == 400
    assert not Withdrawal.objects.exists() and balance(payee) == "5000.00"


def test_cancelled_rejected_and_unpaid_withdrawals_come_back(payee, finance, bank, settings):
    cancelled = withdraw(payee).json()
    # Nobody else even sees it among their own.
    assert finance["kaduna"].post(f"{MINE}{cancelled['id']}/cancel/").status_code == 404
    assert payee.client.post(f"{MINE}{cancelled['id']}/cancel/").json()["status"] == "cancelled"
    assert balance(payee) == "5000.00"
    assert payee.client.post(f"{MINE}{cancelled['id']}/cancel/").status_code == 400

    rejected = withdraw(payee).json()
    url = f"{STAFF}{rejected['id']}/"
    assert finance["kaduna"].post(f"{url}reject/", {}, format="json").status_code == 400
    response = finance["kaduna"].post(f"{url}reject/", {"reason": "Audit first."}, format="json")
    assert response.json()["status"] == "rejected"
    assert balance(payee) == "5000.00"

    # Without Paystack on the server, nothing is approved.
    waiting = withdraw(payee).json()
    url = f"{STAFF}{waiting['id']}/"
    settings.PAYSTACK_SECRET_KEY = ""
    assert finance["kaduna"].post(f"{url}approve/", {}, format="json").status_code == 503
    assert Withdrawal.objects.get(uuid=waiting["id"]).status == "pending"

    # The bank holds the account in someone else's name: nothing is sent.
    settings.PAYSTACK_SECRET_KEY = SECRET
    bank.names[ACCOUNT] = "MUSA BELLO"
    failed = finance["kaduna"].post(f"{url}approve/", {}, format="json").json()
    assert failed["status"] == "failed" and bank.transfers == []
    reason = "The bank holds this account in the name MUSA BELLO, not Hauwa Sani."
    assert failed["failure_reason"] == reason
    assert balance(payee) == "5000.00"
    entry = payee.client.get("/api/v1/me/wallet/").json()["entries"][0]
    assert (entry["kind"], entry["amount"], entry["purpose"]) == (
        "refund",
        "3000.00",
        f"Withdrawal not paid: {reason}",
    )

    # Paystack refuses the transfer (say its balance is too low).
    bank.names[ACCOUNT] = "HAUWA SANI"
    bank.refuse = "Your balance is not enough to fulfil this request"
    refused = withdraw(payee).json()
    result = finance["kaduna"].post(f"{STAFF}{refused['id']}/approve/", {}, format="json").json()
    assert result["status"] == "failed" and result["failure_reason"].startswith("Paystack: ")
    assert balance(payee) == "5000.00"


def test_paystack_reports_a_transfer_failed_or_reversed(payee, finance, bank):
    def approved():
        asked = withdraw(payee).json()
        finance["kaduna"].post(f"{STAFF}{asked['id']}/approve/", {}, format="json")
        return Withdrawal.objects.get(uuid=asked["id"])

    failed = approved()
    assert balance(payee) == "2000.00"
    assert event("transfer.failed", failed).status_code == 200
    assert event("transfer.failed", failed).status_code == 200
    failed.refresh_from_db()
    assert failed.status == "failed" and balance(payee) == "5000.00"

    # Paid, then returned by the bank.
    reversed_ = approved()
    event("transfer.success", reversed_)
    event("transfer.reversed", reversed_)
    event("transfer.reversed", reversed_)
    reversed_.refresh_from_db()
    assert reversed_.status == "reversed" and balance(payee) == "5000.00"

    # Unsigned, or about another amount: ignored.
    other = approved()
    forged = Client().post(
        "/api/v1/paystack/webhook/",
        data=json.dumps({"event": "transfer.failed", "data": {"reference": other.reference}}),
        content_type="application/json",
        headers={"X-Paystack-Signature": "0" * 128},
    )
    assert forged.status_code == 401
    assert event("transfer.failed", other, amount=1).status_code == 200
    other.refresh_from_db()
    assert other.status == "processing" and balance(payee) == "2000.00"


def test_withdrawals_without_an_answer_are_checked_later(payee, finance, bank):
    bank.unreachable = True
    lost = withdraw(payee).json()
    response = finance["kaduna"].post(f"{STAFF}{lost['id']}/approve/", {}, format="json")
    assert response.json()["status"] == "processing"
    assert balance(payee) == "2000.00"

    out = io.StringIO()
    call_command("check_withdrawals", minutes=0, stdout=out)
    assert "Settled 1" in out.getvalue()  # Paystack never got it
    assert Withdrawal.objects.get(uuid=lost["id"]).status == "failed"
    assert balance(payee) == "5000.00"

    bank.unreachable = False
    sent = withdraw(payee).json()
    finance["kaduna"].post(f"{STAFF}{sent['id']}/approve/", {}, format="json")
    withdrawal = Withdrawal.objects.get(uuid=sent["id"])
    bank.known[withdrawal.reference] = "pending"
    call_command("check_withdrawals", minutes=0, stdout=io.StringIO())
    assert Withdrawal.objects.get(uuid=sent["id"]).status == "processing"
    bank.known[withdrawal.reference] = "success"
    call_command("check_withdrawals", minutes=0, stdout=io.StringIO())
    assert Withdrawal.objects.get(uuid=sent["id"]).status == "paid"


def test_approving_is_not_one_transaction_with_the_request():
    """The transfer must not be undone by a rollback after the money left."""
    match = resolve(f"{STAFF}00000000-0000-0000-0000-000000000000/approve/")
    assert match.func._non_atomic_requests == {"default"}


def test_the_banks_name_for_the_account_is_the_members():
    assert names_match("YAHAYA IBRAHIM BASHIR", "Ibrahim Bashir Yahaya")
    assert names_match("IBRAHIM YAHAYA", "Ibrahim Bashir Yahaya")  # middle name left out
    assert names_match("HAUWA SANNI", "Hauwa Sani")  # a letter more
    assert not names_match("IBRAHIM MUSA", "Ibrahim Bashir Yahaya")
    assert not names_match("MUSA BELLO", "Hauwa Sani")
    assert not names_match("", "Hauwa Sani")
    # Distinct words, including reassignment of an earlier fuzzy match.
    assert names_match("MUSA MUSSA", "Musa Musaab")
    assert names_match("SANI", "Sani")
    assert not names_match("HASSAN MUSA", "Hassan Hassani")
    assert not names_match("BELLO MUSA", "Bello Bello")
    assert not names_match("HASSAN HASSAN", "Hassan Hassani")


@pytest.mark.parametrize(
    ("member_name", "bank_name"),
    [
        ("Hassan Hassani", "HASSAN MUSA"),
        ("Bello Bello", "BELLO MUSA"),
        ("Hassan Hassani", "HASSAN HASSAN"),
    ],
)
def test_one_bank_word_cannot_authorize_a_two_word_members_payout(
    payee, finance, bank, member_name, bank_name
):
    payee.full_name = member_name
    payee.save(update_fields=["full_name"])
    bank.names[ACCOUNT] = bank_name
    asked = withdraw(payee).json()

    response = finance["kaduna"].post(
        f"{STAFF}{asked['id']}/approve/", {}, format="json"
    )
    assert response.status_code == 200, response.content
    assert response.json()["status"] == "failed"
    assert bank.transfers == []
    assert balance(payee) == "5000.00"
    withdrawal = Withdrawal.objects.get(uuid=asked["id"])
    assert withdrawal.status == "failed"
    assert withdrawal.entries.filter(kind="withdrawal").count() == 1
    assert withdrawal.entries.filter(kind="refund").count() == 1


def test_the_paystack_client_sends_the_secret_key_and_reads_the_answer(settings, monkeypatch):
    settings.PAYSTACK_SECRET_KEY = SECRET
    sent = []

    class Answer(io.BytesIO):
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def urlopen(request, timeout):
        sent.append(request)
        return Answer(b'{"status": true, "data": {"status": "pending", "transfer_code": "TRF_9"}}')

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    answer = paystack.transfer(
        amount_kobo=150000, recipient="RCP_1", reference="tgcg-wd-abcdef0123456789", reason="Pay"
    )
    assert answer == {"status": "pending", "transfer_code": "TRF_9"}
    request = sent[0]
    assert request.full_url == "https://api.paystack.co/transfer"
    assert request.get_header("Authorization") == f"Bearer {SECRET}"
    assert json.loads(request.data) == {
        "source": "balance",
        "amount": 150000,
        "recipient": "RCP_1",
        "reference": "tgcg-wd-abcdef0123456789",
        "reason": "Pay",
    }

    settings.PAYSTACK_SECRET_KEY = ""
    with pytest.raises(paystack.NotConfigured):
        paystack.resolve_account(ACCOUNT, "999992")

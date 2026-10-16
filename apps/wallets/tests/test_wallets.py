"""Members' wallets: the payout account, payments with a second person, the ledger."""

import pytest

from apps.access.models import AuditEvent
from apps.access.tests.test_access import KD, KN, appoint, client_for, custom_role, member_account
from apps.accounts.models import User
from apps.core.choices import Role
from apps.core.permissions import Capability
from apps.wallets.models import Wallet, WalletEntry
from conftest import PASSWORD

pytestmark = pytest.mark.django_db
C = Capability
FINANCE = [C.VIEW_WALLETS, C.CREDIT_WALLETS, C.APPROVE_WALLET_CREDITS]


@pytest.fixture
def admin():
    return User.objects.create_user(
        "NA-1", PASSWORD, full_name="National Admin", role=Role.NATIONAL_ADMINISTRATOR
    )


@pytest.fixture
def finance(admin):
    """Finance officers: two in Kaduna, one in Kano."""
    role = custom_role(admin, "Finance Officer", FINANCE)

    def officer(phone, name, area):
        member = member_account(phone, name=name)
        assert appoint(admin, member, role, [area]).status_code == 201
        return client_for(member.user)

    return {
        "kaduna": officer("08035550001", "Kaduna Finance One", KD),
        "kaduna2": officer("08035550002", "Kaduna Finance Two", KD),
        "kano": officer("08035550003", "Kano Finance", KN),
    }


def test_a_verified_member_names_the_account_their_wallet_is_paid_into():
    member = member_account("08035550010", name="Musa Bello")
    client = client_for(member.user)
    wallet = client.get("/api/v1/me/wallet/").json()
    assert wallet["has_wallet"] is True and wallet["balance"] == "0.00"
    assert wallet["account_name"] == "Musa Bello" and wallet["bank_account"] is None
    banks = client.get("/api/v1/wallets/banks/").json()
    assert {"code": "gtbank", "name": "Guaranty Trust Bank (GTBank)"} in banks

    # The account name is the member's own and cannot be sent.
    account = {"bank": "gtbank", "account_number": "0123 456 789", "account_name": "Someone Else"}
    saved = client.put("/api/v1/me/wallet/bank-account/", account, format="json")
    assert saved.status_code == 200, saved.content
    assert saved.json()["account_name"] == "Musa Bello"
    assert saved.json()["bank_account"] == {
        "bank": "gtbank",
        "bank_name": "Guaranty Trust Bank (GTBank)",
        "account_number": "0123456789",
    }
    event = AuditEvent.objects.get(action="wallet.bankAccountSet")
    assert event.changes == {"bank": "gtbank", "account": "******6789", "replaced": False}

    for wrong in ({"bank": "gtbank", "account_number": "12345"}, {"bank": "nobank"}):
        response = client.put("/api/v1/me/wallet/bank-account/", wrong, format="json")
        assert response.status_code == 400
    other = client_for(member_account("08035550011", name="Ada Obi").user)
    taken = other.put(
        "/api/v1/me/wallet/bank-account/",
        {"bank": "gtbank", "account_number": "0123456789"},
        format="json",
    )
    assert taken.status_code == 400
    assert "already registered" in str(taken.json())

    waiting = client_for(member_account("08035550012", name="Bala Idris", verified=False).user)
    assert waiting.get("/api/v1/me/wallet/").json()["has_wallet"] is False
    refused = waiting.put(
        "/api/v1/me/wallet/bank-account/",
        {"bank": "opay", "account_number": "8012345678"},
        format="json",
    )
    assert refused.status_code == 400


def test_a_payment_needs_a_second_person_in_the_members_area(finance):
    payee = member_account("08035550020", name="Hauwa Sani")
    client_for(payee.user).put(
        "/api/v1/me/wallet/bank-account/",
        {"bank": "opay", "account_number": "8012345678"},
        format="json",
    )
    payment = {"member": payee.membership_number, "amount": "5000.50", "purpose": "PU agent pay"}
    requested = finance["kaduna"].post("/api/v1/wallet-payments/", payment, format="json")
    assert requested.status_code == 201, requested.content
    body = requested.json()
    assert body["status"] == "pending" and body["amount"] == "5000.50"
    assert body["can_cancel"] is True and body["can_decide"] is False
    url = f"/api/v1/wallet-payments/{body['id']}/"

    # Not by whoever requested it, and not outside the member's area.
    assert finance["kaduna"].post(f"{url}approve/", {}, format="json").status_code == 403
    assert finance["kano"].get("/api/v1/wallet-payments/").json()["results"] == []
    assert finance["kano"].post(f"{url}approve/", {}, format="json").status_code == 404
    assert Wallet.objects.filter(member=payee).count() == 0

    seen = finance["kaduna2"].get("/api/v1/wallet-payments/?status=pending").json()["results"]
    assert [p["id"] for p in seen] == [body["id"]] and seen[0]["can_decide"] is True
    approved = finance["kaduna2"].post(f"{url}approve/", {}, format="json")
    assert approved.status_code == 200, approved.content
    assert approved.json()["status"] == "approved"
    assert finance["kaduna2"].post(f"{url}approve/", {}, format="json").status_code == 400

    wallet = client_for(payee.user).get("/api/v1/me/wallet/").json()
    assert wallet["balance"] == "5000.50"
    assert [(e["amount"], e["balance_after"], e["purpose"]) for e in wallet["entries"]] == [
        ("5000.50", "5000.50", "PU agent pay")
    ]
    # Staff see the balance, and only the end of the account number.
    staff = finance["kaduna2"].get(f"/api/v1/wallets/{payee.membership_number}/").json()
    assert staff["balance"] == "5000.50"
    assert staff["bank_account"]["account_number"] == "******5678"
    assert finance["kano"].get(f"/api/v1/wallets/{payee.membership_number}/").status_code == 404

    events = AuditEvent.objects.filter(action__startswith="wallet.payment")
    actions = sorted(events.values_list("action", flat=True))
    assert actions == ["wallet.paymentApproved", "wallet.paymentRequested"]


def test_payments_that_are_refused_rejected_or_cancelled(finance):
    payee = member_account("08035550030", name="Sani Umar")
    number = payee.membership_number

    def request(client, **fields):
        payment = {"member": number, "amount": "1000", "purpose": "Transport", **fields}
        return client.post("/api/v1/wallet-payments/", payment, format="json")

    assert request(finance["kaduna"], amount="0").status_code == 400
    assert request(finance["kaduna"], amount="1000000.01").status_code == 400
    assert request(finance["kaduna"], purpose=" ").status_code == 400
    assert request(finance["kaduna"], member="TGCG-999999").status_code == 400
    assert request(finance["kano"]).status_code == 403  # outside their area
    unverified = member_account("08035550031", name="Ada Obi", verified=False)
    assert request(finance["kaduna"], member=unverified.membership_number).status_code == 400
    # Nobody pays themselves.
    own = User.objects.get(full_name="Kaduna Finance One").member.membership_number
    assert request(finance["kaduna"], member=own).status_code == 403

    rejected = request(finance["kaduna"]).json()
    url = f"/api/v1/wallet-payments/{rejected['id']}/"
    assert finance["kaduna2"].post(f"{url}reject/", {}, format="json").status_code == 400
    response = finance["kaduna2"].post(f"{url}reject/", {"reason": "Not on duty."}, format="json")
    assert response.json()["status"] == "rejected"
    assert response.json()["decision_reason"] == "Not on duty."

    cancelled = request(finance["kaduna"]).json()
    url = f"/api/v1/wallet-payments/{cancelled['id']}/"
    assert finance["kaduna2"].post(f"{url}cancel/", {}, format="json").status_code == 403
    assert finance["kaduna"].post(f"{url}cancel/", {}, format="json").json()["status"] == (
        "cancelled"
    )
    assert not Wallet.objects.filter(member=payee).exists()


def test_ledger_entries_are_never_changed(finance):
    payee = member_account("08035550040", name="Bala Idris")
    payment = {"member": payee.membership_number, "amount": "250", "purpose": "Data bundle"}
    body = finance["kaduna"].post("/api/v1/wallet-payments/", payment, format="json").json()
    finance["kaduna2"].post(f"/api/v1/wallet-payments/{body['id']}/approve/", {}, format="json")
    entry = WalletEntry.objects.get()
    assert entry.amount_kobo == entry.balance_after_kobo == 25000
    entry.amount_kobo = 1
    with pytest.raises(ValueError):
        entry.save()
    with pytest.raises(ValueError):
        entry.delete()
    assert WalletEntry.objects.get().amount_kobo == 25000

"""Wallet payments and payout accounts. All changes go through here.

Only a member whose identity is verified (by a coordinator or automatically)
has a wallet. A payment is requested by someone holding creditWallets for
the member's area and decided by a different person holding
approveWalletCredits there; nobody pays themselves. Approving adds the
amount to the wallet as a new ledger entry.
"""

from decimal import Decimal

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.access import audit
from apps.access.services import authority_over_member, same_person
from apps.core.choices import MembershipState, VerificationStatus
from apps.core.permissions import Capability
from apps.membership.models import Member

from .models import KOBO, BankAccount, Payment, Wallet, WalletEntry, masked

C = Capability
P = Payment.Status


def naira(kobo: int) -> str:
    return f"{Decimal(kobo) / KOBO:.2f}"


def has_wallet(member) -> bool:
    return member.verification_status == VerificationStatus.VERIFIED


def _require_wallet(member, whose="This member's"):
    if not has_wallet(member):
        raise ValidationError(f"{whose} identity is not verified yet, so there is no wallet.")


def _audit(actor, action, member, reason="", changes=None):
    audit.record(
        actor=actor,
        action=action,
        target_type="member",
        target_id=member.membership_number,
        target_label=member.full_name,
        changes=changes,
        reason=reason,
    )


@transaction.atomic
def set_bank_account(*, member: Member, bank: str, account_number: str) -> BankAccount:
    """The member names the account their wallet is paid into. The account
    name is their name from the PVC, which they cannot change here."""
    member = Member.objects.select_for_update(of=("self",)).select_related("user").get(pk=member.pk)
    if member.membership_state != MembershipState.ACTIVE:
        raise ValidationError("Your membership is not active.")
    _require_wallet(member, "Your")
    account = BankAccount.objects.filter(member=member).first()
    replacing = account is not None
    account = account or BankAccount(member=member)
    account_number = "".join((account_number or "").split())
    if replacing and (account.bank, account.account_number) == (bank, account_number):
        return account
    account.bank, account.account_number = bank, account_number
    account.full_clean()
    try:
        with transaction.atomic():
            account.save()
    except IntegrityError as exc:  # registered by another member meanwhile
        raise ValidationError("This account is already registered to another member.") from exc
    _audit(
        member.user,
        "wallet.bankAccountSet",
        member,
        changes={"bank": bank, "account": masked(account_number), "replaced": replacing},
    )
    return account


@transaction.atomic
def request_payment(*, actor, member: Member, amount_kobo: int, purpose: str) -> Payment:
    """A payment to the member's wallet, waiting for a second person."""
    member = Member.objects.select_related("user").get(pk=member.pk)
    authority_over_member(actor, C.CREDIT_WALLETS, member)
    _require_wallet(member)
    purpose = " ".join((purpose or "").split())
    if not purpose:
        raise ValidationError({"purpose": "Say what the payment is for."})
    payment = Payment(member=member, amount_kobo=amount_kobo, purpose=purpose, requested_by=actor)
    payment.full_clean()
    payment.save()
    _audit(
        actor,
        "wallet.paymentRequested",
        member,
        changes={"payment": str(payment.uuid), "amount": naira(amount_kobo), "purpose": purpose},
    )
    return payment


def _lock(payment) -> Payment:
    return (
        Payment.objects.select_for_update(of=("self",))
        .select_related("member__user", "requested_by")
        .get(pk=payment.pk)
    )


def _decide(payment, actor, status, reason):
    payment.status = status
    payment.decided_by = actor
    payment.decided_at = timezone.now()
    payment.decision_reason = reason
    payment.save(
        update_fields=["status", "decided_by", "decided_at", "decision_reason", "updated_at"]
    )


def _require_decider(actor, payment):
    authority_over_member(actor, C.APPROVE_WALLET_CREDITS, payment.member)
    if same_person(actor, payment.requested_by):
        raise PermissionDenied("Someone other than the person who requested a payment decides it.")
    if payment.status != P.PENDING:
        raise ValidationError(f"This payment is already {payment.status}.")


@transaction.atomic
def approve_payment(*, actor, payment: Payment, reason: str = "") -> Payment:
    """The second person approves: the amount is added to the member's wallet."""
    payment = _lock(payment)
    _require_decider(actor, payment)
    member = payment.member
    _require_wallet(member)  # verification may have been withdrawn since the request
    wallet, _ = Wallet.objects.get_or_create(member=member)
    wallet = Wallet.objects.select_for_update().get(pk=wallet.pk)
    wallet.balance_kobo += payment.amount_kobo
    wallet.save(update_fields=["balance_kobo", "updated_at"])
    WalletEntry.objects.create(
        wallet=wallet,
        kind=WalletEntry.Kind.CREDIT,
        amount_kobo=payment.amount_kobo,
        balance_after_kobo=wallet.balance_kobo,
        payment=payment,
    )
    _decide(payment, actor, P.APPROVED, reason.strip())
    _audit(
        actor,
        "wallet.paymentApproved",
        member,
        reason.strip(),
        changes={
            "payment": str(payment.uuid),
            "amount": naira(payment.amount_kobo),
            "balance": naira(wallet.balance_kobo),
        },
    )
    return payment


@transaction.atomic
def reject_payment(*, actor, payment: Payment, reason: str) -> Payment:
    payment = _lock(payment)
    _require_decider(actor, payment)
    if not reason.strip():
        raise ValidationError({"reason": "Say why the payment was not approved."})
    _decide(payment, actor, P.REJECTED, reason.strip())
    _audit(
        actor,
        "wallet.paymentRejected",
        payment.member,
        reason.strip(),
        changes={"payment": str(payment.uuid), "amount": naira(payment.amount_kobo)},
    )
    return payment


@transaction.atomic
def cancel_payment(*, actor, payment: Payment, reason: str = "") -> Payment:
    """Whoever requested a payment withdraws it before it is decided."""
    payment = _lock(payment)
    if not same_person(actor, payment.requested_by):
        raise PermissionDenied("Only the person who requested a payment can cancel it.")
    if payment.status != P.PENDING:
        raise ValidationError(f"This payment is already {payment.status}.")
    _decide(payment, actor, P.CANCELLED, reason.strip())
    _audit(
        actor,
        "wallet.paymentCancelled",
        payment.member,
        reason.strip(),
        changes={"payment": str(payment.uuid), "amount": naira(payment.amount_kobo)},
    )
    return payment

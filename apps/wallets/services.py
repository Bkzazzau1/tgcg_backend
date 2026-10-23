"""Wallet payments, credits, withdrawals and payout accounts. All changes go
through here.

Only a member whose identity is verified (by a coordinator or automatically)
has a wallet. A payment is requested by someone holding creditWallets for
the member's area and decided by a different person holding
approveWalletCredits there; nobody pays themselves. Approving adds the
amount to the wallet as a new ledger entry.

Credits are the incentive for registering members (decided on 23 October
2026). Whoever registered a member at /me/registered/ earns them once, when
that member first signs in (see CREDITS_EARNED_ON). Credits are kept before
the earner is verified; converting them to naira at the rate of the day, and
withdrawing naira, wait until then. A withdrawal leaves the wallet when it is
asked for; a different person holding approveWithdrawals for the member's
area approves it, and Paystack pays it into the member's account once the
bank confirms the account is in the member's name. A withdrawal that is not
paid comes back to the wallet.
"""

import difflib
import logging
import uuid
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.access import audit
from apps.access.services import authority_over_member, same_person
from apps.core.choices import MembershipState, RegistrationChannel, VerificationStatus
from apps.core.permissions import Capability, has_capability
from apps.membership.card_match import NAME_LIKENESS, name_words
from apps.membership.models import Member

from . import paystack
from .banks import BANKS, PAYSTACK_CODES
from .models import (
    KOBO,
    BankAccount,
    CreditRate,
    Payment,
    Wallet,
    WalletEntry,
    Withdrawal,
    masked,
)

logger = logging.getLogger(__name__)

C = Capability
P = Payment.Status
W = Withdrawal.Status
K = WalletEntry.Kind
OPEN = (W.PENDING, W.PROCESSING)


def naira(kobo: int) -> str:
    return f"{Decimal(kobo) / KOBO:.2f}"


def has_wallet(member) -> bool:
    return member.verification_status == VerificationStatus.VERIFIED


def _require_wallet(member, whose="This member's"):
    if not has_wallet(member):
        raise ValidationError(f"{whose} identity is not verified yet, so there is no wallet.")


def _require_own_wallet(member):
    """The member uses their wallet themselves: active and verified."""
    if member.membership_state != MembershipState.ACTIVE:
        raise ValidationError("Your membership is not active.")
    _require_wallet(member, "Your")


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


def _locked_wallet(member) -> Wallet:
    wallet, _ = Wallet.objects.get_or_create(member=member)
    return Wallet.objects.select_for_update().get(pk=wallet.pk)


def _add_entry(wallet, kind, *, amount_kobo=0, credits=0, **links) -> WalletEntry:
    """Change a locked wallet and record the change."""
    wallet.balance_kobo += amount_kobo
    wallet.credits += credits
    wallet.save(update_fields=["balance_kobo", "credits", "updated_at"])
    return WalletEntry.objects.create(
        wallet=wallet,
        kind=kind,
        amount_kobo=amount_kobo,
        balance_after_kobo=wallet.balance_kobo,
        credits=credits,
        credits_after=wallet.credits,
        **links,
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


# --- Payments from TGCG -------------------------------------------------------------


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
    wallet = _locked_wallet(member)
    _add_entry(wallet, K.CREDIT, amount_kobo=payment.amount_kobo, payment=payment)
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


# --- Credits ------------------------------------------------------------------------


def default_rate() -> CreditRate:
    """Until an administrator sets them: 1 credit per registration, worth
    ₦100, and withdrawals of ₦1,000 or more."""
    return CreditRate(
        credits_per_registration=1, kobo_per_credit=100 * KOBO, min_withdrawal_kobo=1_000 * KOBO
    )


def current_rate() -> CreditRate:
    return CreditRate.objects.first() or default_rate()


def _rate_changes(rate) -> dict:
    return {
        "credits_per_registration": rate.credits_per_registration,
        "naira_per_credit": naira(rate.kobo_per_credit),
        "min_withdrawal": naira(rate.min_withdrawal_kobo),
    }


@transaction.atomic
def set_credit_rate(
    *, actor, credits_per_registration, kobo_per_credit, min_withdrawal_kobo, reason
) -> CreditRate:
    """A National Administrator sets the rates. They apply from now on:
    credits already earned keep their number, and are worth the new rate."""
    if not has_capability(actor, C.MANAGE_CREDIT_RATES):
        raise PermissionDenied("Only a National Administrator sets the credit rates.")
    reason = " ".join((reason or "").split())
    if not reason:
        raise ValidationError({"reason": "Say why the rates change."})
    before = current_rate()
    rate = CreditRate(
        credits_per_registration=credits_per_registration,
        kobo_per_credit=kobo_per_credit,
        min_withdrawal_kobo=min_withdrawal_kobo,
        set_by=actor,
        reason=reason,
    )
    rate.full_clean()
    rate.save()
    audit.record(
        actor=actor,
        action="wallet.creditRatesSet",
        target_type="creditRate",
        target_id=rate.pk,
        target_label=str(rate),
        reason=reason,
        changes={"from": _rate_changes(before), "to": _rate_changes(rate)},
    )
    return rate


def reward_registration(member: Member, *, proof: str) -> WalletEntry | None:
    """The person who registered `member` at /me/registered/ earns credits,
    once. `proof` is how the member showed up: "signIn" (any first sign-in)
    or "smsCode" (they proved the phone with an SMS code). Under
    CREDITS_EARNED_ON = "smsCode" only the second earns."""
    if settings.CREDITS_EARNED_ON == "smsCode" and proof != "smsCode":
        return None
    if member.registration_channel != RegistrationChannel.REFERRED or not member.created_by_id:
        return None
    earner = Member.objects.filter(user_id=member.created_by_id).first()
    if earner is None:  # a staff account with no membership has no wallet
        return None
    rate = current_rate()
    if not rate.credits_per_registration:  # the incentive is paused
        return None
    with transaction.atomic():
        wallet = _locked_wallet(earner)
        if WalletEntry.objects.filter(registered_member=member).exists():
            return None
        entry = _add_entry(
            wallet, K.REWARD, credits=rate.credits_per_registration, registered_member=member
        )
        _audit(
            None,
            "wallet.creditsEarned",
            earner,
            changes={
                "credits": rate.credits_per_registration,
                "credits_after": wallet.credits,
                "registered": member.membership_number,
                "proof": proof,
            },
        )
    return entry


def reward_registration_quietly(member: Member, *, proof: str) -> None:
    """For sign-in and PIN setting: a failure here is logged, and never
    stops the member signing in."""
    try:
        with transaction.atomic():
            reward_registration(member, proof=proof)
    except Exception:
        logger.exception("Could not reward the registration of %s", member.membership_number)


@transaction.atomic
def convert_credits(*, member: Member, credits: int) -> WalletEntry:
    """The member turns credits into naira at the rate of the day."""
    member = Member.objects.select_related("user").get(pk=member.pk)
    _require_own_wallet(member)
    if credits < 1:
        raise ValidationError({"credits": "Convert at least one credit."})
    rate = current_rate()
    wallet = _locked_wallet(member)
    if credits > wallet.credits:
        raise ValidationError({"credits": f"You have {wallet.credits} credits."})
    amount_kobo = credits * rate.kobo_per_credit
    entry = _add_entry(wallet, K.CONVERSION, amount_kobo=amount_kobo, credits=-credits)
    _audit(
        member.user,
        "wallet.creditsConverted",
        member,
        changes={
            "credits": credits,
            "naira_per_credit": naira(rate.kobo_per_credit),
            "amount": naira(amount_kobo),
            "balance": naira(wallet.balance_kobo),
        },
    )
    return entry


# --- Withdrawals ----------------------------------------------------------------------


def payable(bank: str) -> bool:
    """Paystack pays into this bank."""
    return bank in PAYSTACK_CODES


def names_match(bank_name: str, member_name: str) -> bool:
    """The bank's name for the account is the member's: at least two words
    of the member's name (one if it has only one) among the bank's, in any
    order, a misread letter allowed. Banks order names their own way and
    may leave out a middle name."""
    wanted = name_words(member_name)
    held = set(name_words(bank_name))
    found = sum(
        any(
            word == other or difflib.SequenceMatcher(None, word, other).ratio() >= NAME_LIKENESS
            for other in held
        )
        for word in wanted
    )
    return bool(wanted) and found >= min(2, len(wanted))


@transaction.atomic
def request_withdrawal(*, member: Member, amount_kobo: int) -> Withdrawal:
    """The member asks for naira to be paid into their account. The amount
    leaves the wallet now and comes back if it is not paid."""
    member = Member.objects.select_for_update(of=("self",)).select_related("user").get(pk=member.pk)
    _require_own_wallet(member)
    account = BankAccount.objects.filter(member=member).first()
    if account is None:
        raise ValidationError("Name the bank account to pay into first.")
    if not payable(account.bank):
        raise ValidationError(
            f"Withdrawals cannot be paid into {BANKS[account.bank]}. Choose another bank."
        )
    rate = current_rate()
    if amount_kobo < rate.min_withdrawal_kobo:
        raise ValidationError(
            {"amount": f"Withdraw at least ₦{naira(rate.min_withdrawal_kobo)} at a time."}
        )
    if Withdrawal.objects.filter(member=member, status__in=OPEN).exists():
        raise ValidationError("A withdrawal of yours is still being handled.")
    wallet = _locked_wallet(member)
    if amount_kobo > wallet.balance_kobo:
        raise ValidationError({"amount": f"Your balance is ₦{naira(wallet.balance_kobo)}."})
    withdrawal = Withdrawal(
        member=member,
        amount_kobo=amount_kobo,
        bank=account.bank,
        account_number=account.account_number,
        # Paystack's rules: 16 to 50 lowercase letters, digits, - and _.
        reference=f"tgcg-wd-{uuid.uuid4().hex}",
    )
    withdrawal.full_clean()
    withdrawal.save()
    _add_entry(wallet, K.WITHDRAWAL, amount_kobo=-amount_kobo, withdrawal=withdrawal)
    _audit(
        member.user,
        "wallet.withdrawalRequested",
        member,
        changes={
            "withdrawal": str(withdrawal.uuid),
            "amount": naira(amount_kobo),
            "bank": account.bank,
            "account": masked(account.account_number),
        },
    )
    return withdrawal


def _lock_withdrawal(withdrawal) -> Withdrawal:
    return (
        Withdrawal.objects.select_for_update(of=("self",))
        .select_related("member__user")
        .get(pk=withdrawal.pk)
    )


def _refund(withdrawal):
    wallet = _locked_wallet(withdrawal.member)
    _add_entry(wallet, K.REFUND, amount_kobo=withdrawal.amount_kobo, withdrawal=withdrawal)


def _changes(withdrawal, **extra) -> dict:
    return {"withdrawal": str(withdrawal.uuid), "amount": naira(withdrawal.amount_kobo), **extra}


def _close(withdrawal, actor, status, reason):
    """Rejected or cancelled before anything was sent: the money comes back."""
    withdrawal.status = status
    withdrawal.decided_by = actor
    withdrawal.decided_at = withdrawal.finished_at = timezone.now()
    withdrawal.decision_reason = reason
    withdrawal.save()
    _refund(withdrawal)


@transaction.atomic
def cancel_withdrawal(*, member: Member, withdrawal: Withdrawal) -> Withdrawal:
    """The member takes back a withdrawal nobody has approved yet."""
    withdrawal = _lock_withdrawal(withdrawal)
    if withdrawal.member_id != member.pk:
        raise PermissionDenied("Only the member who asked for a withdrawal can cancel it.")
    if withdrawal.status != W.PENDING:
        raise ValidationError(f"This withdrawal is already {withdrawal.status}.")
    _close(withdrawal, member.user, W.CANCELLED, "")
    _audit(
        member.user, "wallet.withdrawalCancelled", withdrawal.member, changes=_changes(withdrawal)
    )
    return withdrawal


def _require_withdrawal_decider(actor, withdrawal):
    authority_over_member(actor, C.APPROVE_WITHDRAWALS, withdrawal.member)
    if withdrawal.status != W.PENDING:
        raise ValidationError(f"This withdrawal is already {withdrawal.status}.")


@transaction.atomic
def reject_withdrawal(*, actor, withdrawal: Withdrawal, reason: str) -> Withdrawal:
    withdrawal = _lock_withdrawal(withdrawal)
    _require_withdrawal_decider(actor, withdrawal)
    reason = reason.strip()
    if not reason:
        raise ValidationError({"reason": "Say why the withdrawal was not approved."})
    _close(withdrawal, actor, W.REJECTED, reason)
    _audit(actor, "wallet.withdrawalRejected", withdrawal.member, reason, _changes(withdrawal))
    return withdrawal


class PaymentsUnavailable(Exception):
    """Paystack is not set up on this server."""


def approve_withdrawal(*, actor, withdrawal: Withdrawal, reason: str = "") -> Withdrawal:
    """The second person approves, and the money is sent at once.

    Not one transaction: the approval is saved before Paystack is asked to
    pay, and the outcome after, so no rollback can forget money that left.
    The view calling this must be `transaction.non_atomic_requests`.
    """
    if not paystack.configured():
        raise PaymentsUnavailable
    with transaction.atomic():
        withdrawal = _lock_withdrawal(withdrawal)
        _require_withdrawal_decider(actor, withdrawal)
        member = withdrawal.member
        if member.membership_state != MembershipState.ACTIVE or not has_wallet(member):
            raise ValidationError(
                "The member is no longer active and verified; reject the withdrawal instead."
            )
        withdrawal.status = W.PROCESSING
        withdrawal.decided_by = actor
        withdrawal.decided_at = timezone.now()
        withdrawal.decision_reason = reason.strip()
        withdrawal.save()
        _audit(
            actor,
            "wallet.withdrawalApproved",
            member,
            reason.strip(),
            _changes(withdrawal, bank=withdrawal.bank, account=masked(withdrawal.account_number)),
        )
    return _send(withdrawal)


def _send(withdrawal: Withdrawal) -> Withdrawal:
    """Ask the bank whose account it is, and pay it if it is the member's."""
    member = withdrawal.member
    code = PAYSTACK_CODES.get(withdrawal.bank)
    if code is None:
        return settle(withdrawal, W.FAILED, "Paystack does not pay into this bank.")
    try:
        name = paystack.resolve_account(withdrawal.account_number, code)
        if not names_match(name, member.full_name):
            return settle(
                withdrawal,
                W.FAILED,
                f"The bank holds this account in the name {name}, not {member.full_name}.",
                bank_account_name=name,
            )
        recipient = paystack.create_recipient(
            name=name, account_number=withdrawal.account_number, bank_code=code
        )
        sent = paystack.transfer(
            amount_kobo=withdrawal.amount_kobo,
            recipient=recipient,
            reference=withdrawal.reference,
            reason=f"TGCG wallet {member.membership_number}",
        )
    except paystack.PaystackError as exc:
        return settle(withdrawal, W.FAILED, f"Paystack: {exc}")
    except paystack.Unreachable:
        # Perhaps sent, perhaps not: check_withdrawals asks Paystack later.
        return Withdrawal.objects.get(pk=withdrawal.pk)
    Withdrawal.objects.filter(pk=withdrawal.pk).update(
        bank_account_name=name, transfer_code=sent.get("transfer_code") or ""
    )
    return _outcome(withdrawal, sent.get("status"), sent.get("reason") or "")


# Paystack's word on a transfer, and what it means here. Any other status
# (pending, received, queued, otp...) means it is still under way.
FINISHED = {"success": W.PAID, "failed": W.FAILED, "reversed": W.REVERSED, "abandoned": W.FAILED}


def _outcome(withdrawal, status, reason="") -> Withdrawal:
    if status == "otp":
        logger.error(
            "Paystack holds transfer %s for an OTP: turn off OTP for transfers.",
            withdrawal.reference,
        )
    if status in FINISHED:
        return settle(withdrawal, FINISHED[status], reason)
    return Withdrawal.objects.get(pk=withdrawal.pk)


@transaction.atomic
def settle(withdrawal: Withdrawal, status: str, reason: str = "", **fields) -> Withdrawal:
    """Record how a withdrawal being paid ended. Repeating it changes nothing.
    One not paid, or returned by the bank after it was paid, comes back to
    the wallet."""
    withdrawal = _lock_withdrawal(withdrawal)
    under_way = withdrawal.status == W.PROCESSING and status in (W.PAID, W.FAILED, W.REVERSED)
    returned = withdrawal.status == W.PAID and status == W.REVERSED
    if not (under_way or returned):
        return withdrawal
    withdrawal.status = status
    withdrawal.failure_reason = reason
    withdrawal.finished_at = timezone.now()
    for field, value in fields.items():
        setattr(withdrawal, field, value)
    withdrawal.save()
    if status != W.PAID:
        _refund(withdrawal)
    action = {
        W.PAID: "wallet.withdrawalPaid",
        W.FAILED: "wallet.withdrawalFailed",
        W.REVERSED: "wallet.withdrawalReversed",
    }[status]
    _audit(None, action, withdrawal.member, reason, _changes(withdrawal))
    return withdrawal


TRANSFER_EVENTS = {
    "transfer.success": "success",
    "transfer.failed": "failed",
    "transfer.reversed": "reversed",
}


def handle_paystack_event(event: dict) -> None:
    """A transfer's outcome, as Paystack posted it (signature checked)."""
    status = TRANSFER_EVENTS.get(event.get("event"))
    data = event.get("data") or {}
    if status is None:
        return
    withdrawal = Withdrawal.objects.filter(reference=data.get("reference") or "").first()
    if withdrawal is None:
        return
    if data.get("amount") != withdrawal.amount_kobo:
        logger.error("Paystack reported another amount for %s; ignored.", withdrawal.reference)
        return
    _outcome(withdrawal, status, data.get("reason") or "")


def check_withdrawal(withdrawal: Withdrawal) -> Withdrawal:
    """Ask Paystack how a withdrawal being paid stands, for when its event
    never came or the transfer request got no answer."""
    try:
        data = paystack.verify_transfer(withdrawal.reference)
    except paystack.PaystackError as exc:
        if exc.status == 404:  # the transfer request never reached Paystack
            return settle(withdrawal, W.FAILED, "Paystack has no record of this transfer.")
        logger.warning("Paystack refused to verify %s: %s", withdrawal.reference, exc)
        return withdrawal
    return _outcome(withdrawal, data.get("status"), data.get("reason") or "")

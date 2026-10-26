"""Members' wallets: what TGCG owes each member, and where it is to be paid.

TGCG credits wallets; members do not deposit. A wallet holds naira and
credits. Naira come from payments that one person requests and a second
person approves, and from credits the member converts. Credits are the
incentive for registering members: the person who registered a member earns
them when that member first signs in. A member withdraws naira to their bank
account through Paystack once a second person approves, and only into an
account the bank holds in the name on their PVC. Every change is a ledger
entry that is never changed or removed. Amounts are kept in kobo.
"""

import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator, RegexValidator
from django.db import models
from django.db.models import Q
from django.utils import timezone

from apps.core.models import TimeStampedModel
from apps.membership.models import Member

from .banks import BANKS

KOBO = 100
# A sanity limit on one payment (₦1,000,000), against a slip of the keyboard.
MAX_PAYMENT_KOBO = 1_000_000 * KOBO


def validate_bank(value):
    if value not in BANKS:
        raise ValidationError("Choose a bank from the list.")


class Wallet(TimeStampedModel):
    """A member's naira and credits: the sums of their ledger entries, kept current."""

    member = models.OneToOneField(Member, on_delete=models.PROTECT, related_name="wallet")
    balance_kobo = models.BigIntegerField(default=0)
    credits = models.BigIntegerField(default=0)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(balance_kobo__gte=0), name="wallet_balance_not_negative"
            ),
            models.CheckConstraint(condition=Q(credits__gte=0), name="wallet_credits_not_negative"),
        ]

    def __str__(self):
        return f"{self.member.membership_number}: {self.balance_kobo / KOBO:.2f}"


class Payment(TimeStampedModel):
    """A payment TGCG makes to a member's wallet: requested by one person,
    approved or rejected by another."""

    class Status(models.TextChoices):
        PENDING = "pending", "Awaiting approval"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"
        CANCELLED = "cancelled", "Cancelled"

    member = models.ForeignKey(Member, on_delete=models.PROTECT, related_name="wallet_payments")
    amount_kobo = models.PositiveBigIntegerField(
        validators=[MinValueValidator(1), MaxValueValidator(MAX_PAYMENT_KOBO)]
    )
    purpose = models.CharField(max_length=200)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.PENDING)
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
    )
    decided_at = models.DateTimeField(null=True, blank=True)
    decision_reason = models.TextField(blank=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        constraints = [
            models.CheckConstraint(condition=Q(amount_kobo__gt=0), name="payment_amount_positive")
        ]

    def __str__(self):
        return f"{self.member.membership_number}: {self.amount_kobo / KOBO:.2f} ({self.status})"


class CreditRate(models.Model):
    """What registering a member earns and what a credit is worth, as a
    National Administrator set them. Each change is a new row; the latest
    applies from then on."""

    credits_per_registration = models.PositiveIntegerField(
        validators=[MaxValueValidator(1000)], help_text="0 pauses the incentive."
    )
    kobo_per_credit = models.PositiveBigIntegerField(
        validators=[MinValueValidator(1), MaxValueValidator(MAX_PAYMENT_KOBO)]
    )
    min_withdrawal_kobo = models.PositiveBigIntegerField(
        validators=[MinValueValidator(1), MaxValueValidator(MAX_PAYMENT_KOBO)]
    )
    set_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, related_name="+"
    )
    reason = models.TextField(blank=True)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ["-created_at", "-id"]

    def __str__(self):
        return (
            f"{self.credits_per_registration} per registration, "
            f"{self.kobo_per_credit / KOBO:.2f} per credit"
        )


class Withdrawal(TimeStampedModel):
    """Naira a member takes out of their wallet to their bank account.

    The amount leaves the wallet when it is requested, and comes back (as a
    new entry) if the withdrawal is cancelled, rejected or not paid. A second
    person approves it; the server then asks the bank, through Paystack,
    whose name the account is in, and sends the money only to the member's.
    """

    class Status(models.TextChoices):
        PENDING = "pending", "Awaiting approval"
        PROCESSING = "processing", "Being paid"
        PAID = "paid", "Paid"
        FAILED = "failed", "Not paid"
        REVERSED = "reversed", "Returned by the bank"
        REJECTED = "rejected", "Rejected"
        CANCELLED = "cancelled", "Cancelled"

    member = models.ForeignKey(Member, on_delete=models.PROTECT, related_name="withdrawals")
    amount_kobo = models.PositiveBigIntegerField(
        validators=[MinValueValidator(1), MaxValueValidator(MAX_PAYMENT_KOBO)]
    )
    # The account as it was when the member asked: the one the approver sees.
    bank = models.CharField(max_length=32, validators=[validate_bank])
    account_number = models.CharField(max_length=10)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.PENDING)
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
    )
    decided_at = models.DateTimeField(null=True, blank=True)
    decision_reason = models.TextField(blank=True)
    # Paystack's side: our reference for the transfer, the bank's name for
    # the account, and Paystack's own code for the transfer.
    reference = models.CharField(max_length=50, unique=True)
    bank_account_name = models.CharField(max_length=150, blank=True)
    transfer_code = models.CharField(max_length=64, blank=True)
    failure_reason = models.TextField(blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        constraints = [
            models.CheckConstraint(
                condition=Q(amount_kobo__gt=0), name="withdrawal_amount_positive"
            ),
            # One at a time: the next waits until this one is settled.
            models.UniqueConstraint(
                fields=["member"],
                condition=Q(status__in=["pending", "processing"]),
                name="one_open_withdrawal_per_member",
                violation_error_message="A withdrawal of yours is still being handled.",
            ),
        ]

    def __str__(self):
        return f"{self.member.membership_number}: {self.amount_kobo / KOBO:.2f} ({self.status})"


def tgc_tokens(count: int) -> str:
    """Credits as members see them, since 26 October 2026: TGC tokens. The API
    and the database still call them credits."""
    return f"{count} TGC token" + ("" if abs(count) == 1 else "s")


class WalletEntry(models.Model):
    """One change to a wallet's naira or credits (TGC tokens). Entries are only
    ever added."""

    class Kind(models.TextChoices):
        CREDIT = "credit", "Payment from TGCG"
        REWARD = "reward", "TGC tokens for registering a member"
        CONVERSION = "conversion", "TGC tokens converted to naira"
        WITHDRAWAL = "withdrawal", "Withdrawal to the bank account"
        REFUND = "refund", "Withdrawal returned to the wallet"

    uuid = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    wallet = models.ForeignKey(Wallet, on_delete=models.PROTECT, related_name="entries")
    kind = models.CharField(max_length=12, choices=Kind.choices)
    amount_kobo = models.BigIntegerField(help_text="Positive into the wallet, negative out.")
    balance_after_kobo = models.BigIntegerField()
    credits = models.BigIntegerField(default=0, help_text="Positive earned, negative converted.")
    credits_after = models.BigIntegerField(default=0)
    payment = models.OneToOneField(
        Payment, on_delete=models.PROTECT, null=True, blank=True, related_name="entry"
    )
    # A reward: the member whose registration earned it, once per member.
    registered_member = models.OneToOneField(
        Member,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="registration_reward",
    )
    withdrawal = models.ForeignKey(
        Withdrawal, on_delete=models.PROTECT, null=True, blank=True, related_name="entries"
    )
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        verbose_name_plural = "wallet entries"

    def __str__(self):
        member = self.wallet.member.membership_number
        sign = "+" if self.credits >= 0 else "-"
        return f"{member}: {self.amount_kobo / KOBO:+.2f}, {sign}{tgc_tokens(abs(self.credits))}"

    def save(self, *args, **kwargs):
        if self.pk is not None:
            raise ValueError("A wallet entry is never changed; record a new one.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValueError("A wallet entry is never removed; record a new one.")


class BankAccount(TimeStampedModel):
    """Where a member's wallet is to be paid. The account name is not stored:
    it is always the member's name from their PVC, which the bank API will
    check against the bank's own record of the account."""

    member = models.OneToOneField(Member, on_delete=models.PROTECT, related_name="bank_account")
    bank = models.CharField(max_length=32, validators=[validate_bank])
    account_number = models.CharField(
        max_length=10,
        validators=[RegexValidator(r"^\d{10}$", "Enter the 10-digit account number (NUBAN).")],
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["bank", "account_number"],
                name="one_member_per_bank_account",
                violation_error_message="This account is already registered to another member.",
            )
        ]

    def __str__(self):
        return f"{self.member.membership_number}: {self.bank} {masked(self.account_number)}"


def masked(account_number: str) -> str:
    """The last four digits only, for records and staff screens."""
    return f"******{account_number[-4:]}" if account_number else ""

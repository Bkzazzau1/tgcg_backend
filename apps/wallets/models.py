"""Members' wallets: what TGCG owes each member, and where it is to be paid.

TGCG credits wallets; members do not deposit. Every credit is a payment that
one person requests and a second person approves, and each approved payment
becomes a ledger entry that is never changed or removed. The money stays in
TGCG's bank until a member withdraws, which waits for a bank API; that API
will also confirm the payout account is in the name on the member's PVC.
Amounts are kept in kobo.
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
    """A member's balance: the sum of their ledger entries, kept current."""

    member = models.OneToOneField(Member, on_delete=models.PROTECT, related_name="wallet")
    balance_kobo = models.BigIntegerField(default=0)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(balance_kobo__gte=0), name="wallet_balance_not_negative"
            )
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


class WalletEntry(models.Model):
    """One change to a wallet's balance. Entries are only ever added."""

    class Kind(models.TextChoices):
        CREDIT = "credit", "Payment from TGCG"

    uuid = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    wallet = models.ForeignKey(Wallet, on_delete=models.PROTECT, related_name="entries")
    kind = models.CharField(max_length=12, choices=Kind.choices)
    amount_kobo = models.BigIntegerField(help_text="Positive into the wallet, negative out.")
    balance_after_kobo = models.BigIntegerField()
    payment = models.OneToOneField(
        Payment, on_delete=models.PROTECT, null=True, blank=True, related_name="entry"
    )
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        verbose_name_plural = "wallet entries"

    def __str__(self):
        return f"{self.wallet.member.membership_number}: {self.amount_kobo / KOBO:+.2f}"

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

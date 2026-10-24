from decimal import Decimal

from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from apps.access.services import same_person
from apps.core.choices import VerificationStatus
from apps.core.permissions import Capability
from apps.geography.scope import scope_contains, scope_payload

from .banks import BANK_CHOICES, BANKS
from .models import KOBO, MAX_PAYMENT_KOBO, CreditRate, Payment, WalletEntry, Withdrawal, masked
from .registrars import own_counts
from .services import current_rate, has_wallet, naira, payable

C = Capability
# How much history a wallet shows at once.
ENTRIES_SHOWN = 100
NAIRA = {"max_digits": 10, "decimal_places": 2, "min_value": Decimal("0.01")}


class BankSerializer(serializers.Serializer):
    code = serializers.CharField()
    name = serializers.CharField()
    payable = serializers.BooleanField(help_text="Withdrawals can be paid into this bank.")


class BankAccountSerializer(serializers.Serializer):
    """The payout account. Staff see only the last four digits."""

    bank = serializers.CharField()
    bank_name = serializers.SerializerMethodField()
    account_number = serializers.SerializerMethodField()
    payable = serializers.SerializerMethodField(
        help_text="Withdrawals can be paid into this account's bank."
    )

    def get_bank_name(self, account) -> str:
        return BANKS.get(account.bank, account.bank)

    def get_account_number(self, account) -> str:
        if self.context.get("masked"):
            return masked(account.account_number)
        return account.account_number

    def get_payable(self, account) -> bool:
        return payable(account.bank)


class BankAccountInputSerializer(serializers.Serializer):
    bank = serializers.ChoiceField(choices=BANK_CHOICES)
    account_number = serializers.CharField(max_length=20)


class CreditRateSerializer(serializers.ModelSerializer):
    """What registering a member earns, what a credit is worth, and the
    smallest withdrawal. `set_by` and `created_at` are null for the starting
    rates, before an administrator sets any."""

    naira_per_credit = serializers.SerializerMethodField()
    min_withdrawal = serializers.SerializerMethodField()
    set_by = serializers.CharField(source="set_by.access_id", default=None, read_only=True)
    created_at = serializers.SerializerMethodField()

    class Meta:
        model = CreditRate
        fields = [
            "credits_per_registration",
            "naira_per_credit",
            "min_withdrawal",
            "reason",
            "set_by",
            "created_at",
        ]
        read_only_fields = fields

    def get_naira_per_credit(self, rate) -> str:
        return naira(rate.kobo_per_credit)

    def get_min_withdrawal(self, rate) -> str:
        return naira(rate.min_withdrawal_kobo)

    @extend_schema_field(serializers.DateTimeField(allow_null=True))
    def get_created_at(self, rate):
        return serializers.DateTimeField().to_representation(rate.created_at) if rate.pk else None


class CreditRateInputSerializer(serializers.Serializer):
    credits_per_registration = serializers.IntegerField(
        min_value=0, max_value=1000, help_text="0 pauses the incentive."
    )
    naira_per_credit = serializers.DecimalField(**NAIRA, max_value=Decimal(MAX_PAYMENT_KOBO) / KOBO)
    min_withdrawal = serializers.DecimalField(**NAIRA, max_value=Decimal(MAX_PAYMENT_KOBO) / KOBO)
    reason = serializers.CharField(max_length=1000)


class WalletEntrySerializer(serializers.ModelSerializer):
    id = serializers.UUIDField(source="uuid", read_only=True)
    amount = serializers.SerializerMethodField(help_text="Naira in (positive) or out (negative).")
    balance_after = serializers.SerializerMethodField()
    purpose = serializers.SerializerMethodField()

    class Meta:
        model = WalletEntry
        fields = [
            "id",
            "kind",
            "amount",
            "balance_after",
            "credits",
            "credits_after",
            "purpose",
            "created_at",
        ]
        read_only_fields = fields

    def get_amount(self, entry) -> str:
        return naira(entry.amount_kobo)

    def get_balance_after(self, entry) -> str:
        return naira(entry.balance_after_kobo)

    def get_purpose(self, entry) -> str:
        K = WalletEntry.Kind
        if entry.kind == K.CREDIT:
            return entry.payment.purpose if entry.payment else ""
        if entry.kind == K.REWARD:
            registered = entry.registered_member
            # Staff see the membership number; the member, the name they registered.
            masked_view = self.context.get("masked")
            shown = registered.membership_number if masked_view else registered.full_name
            return f"Registered {shown}"
        if entry.kind == K.CONVERSION:
            return f"Converted {-entry.credits} credits"
        withdrawal = entry.withdrawal
        if entry.kind == K.WITHDRAWAL:
            bank = BANKS.get(withdrawal.bank, withdrawal.bank)
            return f"Withdrawal to {bank} {masked(withdrawal.account_number)}"
        why = withdrawal.failure_reason or withdrawal.decision_reason
        return f"Withdrawal {withdrawal.get_status_display().lower()}" + (f": {why}" if why else "")


class RegistrationCountsSerializer(serializers.Serializer):
    registered = serializers.IntegerField()
    signed_in = serializers.IntegerField(help_text="Have signed in at least once.")
    waiting = serializers.IntegerField(help_text="Not signed in yet: no credit until they do.")
    credited = serializers.IntegerField(help_text="Earned their registrar a credit.")


class RegistrarSerializer(serializers.Serializer):
    """Someone who registers members, with how many and how far each got."""

    access_id = serializers.CharField()
    full_name = serializers.SerializerMethodField()
    membership_number = serializers.SerializerMethodField(
        help_text="Null for an account without a membership, which earns no credits."
    )
    scope = serializers.SerializerMethodField(
        help_text="The member's place, or the account's area."
    )
    identity_verified = serializers.SerializerMethodField(
        help_text="Credits turn into naira only once the registrar's identity is checked."
    )
    registered = serializers.IntegerField()
    signed_in = serializers.IntegerField()
    waiting = serializers.IntegerField()
    credited = serializers.IntegerField()
    credits_earned = serializers.IntegerField()
    credits = serializers.SerializerMethodField(help_text="Credits in the wallet now.")
    last_registered_at = serializers.DateTimeField()

    def _member(self, user):
        return getattr(user, "member", None)

    def get_full_name(self, user) -> str:
        member = self._member(user)
        return member.full_name if member else user.full_name

    def get_membership_number(self, user) -> str | None:
        member = self._member(user)
        return member.membership_number if member else None

    @extend_schema_field(serializers.DictField())
    def get_scope(self, user):
        return scope_payload(self._member(user) or user)

    def get_identity_verified(self, user) -> bool:
        member = self._member(user)
        return bool(member) and member.verification_status == VerificationStatus.VERIFIED

    def get_credits(self, user) -> int:
        wallet = getattr(self._member(user), "wallet", None)
        return wallet.credits if wallet else 0


class RegistrarTotalsSerializer(serializers.Serializer):
    registrars = serializers.IntegerField()
    registered = serializers.IntegerField()
    signed_in = serializers.IntegerField()
    waiting = serializers.IntegerField()
    credited = serializers.IntegerField()


class RegisteredMemberRowSerializer(serializers.Serializer):
    """One person a registrar registered: whether they signed in, and whether
    the registrar was credited. No phone numbers."""

    membership_number = serializers.CharField()
    full_name = serializers.CharField()
    scope = serializers.SerializerMethodField()
    registered_at = serializers.DateTimeField(source="created_at")
    signed_in = serializers.SerializerMethodField()
    last_sign_in = serializers.SerializerMethodField()
    credited = serializers.SerializerMethodField()
    verification_status = serializers.CharField()
    membership_state = serializers.CharField()

    @extend_schema_field(serializers.DictField())
    def get_scope(self, member):
        return scope_payload(member)

    def get_signed_in(self, member) -> bool:
        return bool(member.user_id and member.user.last_login)

    @extend_schema_field(serializers.DateTimeField(allow_null=True))
    def get_last_sign_in(self, member):
        if not self.get_signed_in(member):
            return None
        return serializers.DateTimeField().to_representation(member.user.last_login)

    def get_credited(self, member) -> bool:
        return hasattr(member, "registration_reward")


class WalletSerializer(serializers.Serializer):
    """A member's wallet: the naira TGCG owes them, their credits and what
    the credits are worth today, its history, and the account it is paid
    into, in the name on their PVC."""

    membership_number = serializers.CharField()
    has_wallet = serializers.SerializerMethodField(
        help_text="Verified: the member may convert credits and withdraw."
    )
    account_name = serializers.CharField(source="full_name")
    balance = serializers.SerializerMethodField()
    credits = serializers.SerializerMethodField()
    credits_worth = serializers.SerializerMethodField(help_text="Naira, at today's rate.")
    rate = serializers.SerializerMethodField()
    bank_account = serializers.SerializerMethodField()
    entries = serializers.SerializerMethodField()
    registrations = serializers.SerializerMethodField(
        help_text="People this member registered: a credit comes when each first signs in."
    )

    def get_has_wallet(self, member) -> bool:
        return has_wallet(member)

    def _wallet(self, member):
        return getattr(member, "wallet", None)

    def _rate(self):
        if "rate" not in self.context:
            self.context["rate"] = current_rate()
        return self.context["rate"]

    def get_balance(self, member) -> str:
        wallet = self._wallet(member)
        return naira(wallet.balance_kobo if wallet else 0)

    def get_credits(self, member) -> int:
        wallet = self._wallet(member)
        return wallet.credits if wallet else 0

    def get_credits_worth(self, member) -> str:
        return naira(self.get_credits(member) * self._rate().kobo_per_credit)

    @extend_schema_field(CreditRateSerializer)
    def get_rate(self, member):
        return CreditRateSerializer(self._rate()).data

    @extend_schema_field(BankAccountSerializer(allow_null=True))
    def get_bank_account(self, member):
        account = getattr(member, "bank_account", None)
        return BankAccountSerializer(account, context=self.context).data if account else None

    @extend_schema_field(WalletEntrySerializer(many=True))
    def get_entries(self, member):
        wallet = self._wallet(member)
        if wallet is None:
            return []
        entries = wallet.entries.select_related("payment", "registered_member", "withdrawal")
        return WalletEntrySerializer(entries[:ENTRIES_SHOWN], many=True, context=self.context).data

    @extend_schema_field(RegistrationCountsSerializer)
    def get_registrations(self, member):
        if not member.user_id:
            return {"registered": 0, "signed_in": 0, "waiting": 0, "credited": 0}
        return own_counts(member.user_id)


class PaymentSerializer(serializers.ModelSerializer):
    id = serializers.UUIDField(source="uuid", read_only=True)
    member = serializers.CharField(source="member.membership_number", read_only=True)
    member_name = serializers.CharField(source="member.full_name", read_only=True)
    amount = serializers.SerializerMethodField()
    requested_by = serializers.CharField(source="requested_by.access_id", read_only=True)
    decided_by = serializers.CharField(source="decided_by.access_id", default=None, read_only=True)
    can_decide = serializers.SerializerMethodField()
    can_cancel = serializers.SerializerMethodField()

    class Meta:
        model = Payment
        fields = [
            "id",
            "member",
            "member_name",
            "amount",
            "purpose",
            "status",
            "requested_by",
            "created_at",
            "decided_by",
            "decided_at",
            "decision_reason",
            "can_decide",
            "can_cancel",
        ]
        read_only_fields = fields

    def _user(self):
        request = self.context.get("request")
        return request.user if request else None

    def get_amount(self, payment) -> str:
        return naira(payment.amount_kobo)

    def get_can_decide(self, payment) -> bool:
        user = self._user()
        return bool(
            user
            and payment.status == Payment.Status.PENDING
            and not same_person(user, payment.requested_by)
            and payment.member.user_id != user.pk
            and scope_contains(user, payment.member, permission=C.APPROVE_WALLET_CREDITS)
        )

    def get_can_cancel(self, payment) -> bool:
        user = self._user()
        return bool(
            user
            and payment.status == Payment.Status.PENDING
            and same_person(user, payment.requested_by)
        )


class PaymentCreateSerializer(serializers.Serializer):
    member = serializers.CharField(max_length=16, help_text="Membership number.")
    amount = serializers.DecimalField(
        **NAIRA, max_value=Decimal(MAX_PAYMENT_KOBO) / KOBO, help_text="Naira."
    )
    purpose = serializers.CharField(max_length=200)


class PaymentDecisionSerializer(serializers.Serializer):
    reason = serializers.CharField(required=False, allow_blank=True, max_length=1000)


class ConvertCreditsSerializer(serializers.Serializer):
    credits = serializers.IntegerField(min_value=1)


class WithdrawalCreateSerializer(serializers.Serializer):
    amount = serializers.DecimalField(
        **NAIRA, max_value=Decimal(MAX_PAYMENT_KOBO) / KOBO, help_text="Naira."
    )


class WithdrawalSerializer(serializers.ModelSerializer):
    """A withdrawal. Staff see only the last four digits of the account.
    `bank_account_name` is the bank's own name for the account, once asked;
    `failure_reason` says why one was not paid."""

    id = serializers.UUIDField(source="uuid", read_only=True)
    member = serializers.CharField(source="member.membership_number", read_only=True)
    member_name = serializers.CharField(source="member.full_name", read_only=True)
    amount = serializers.SerializerMethodField()
    bank_name = serializers.SerializerMethodField()
    account_number = serializers.SerializerMethodField()
    decided_by = serializers.CharField(source="decided_by.access_id", default=None, read_only=True)
    can_decide = serializers.SerializerMethodField()
    can_cancel = serializers.SerializerMethodField()

    class Meta:
        model = Withdrawal
        fields = [
            "id",
            "member",
            "member_name",
            "amount",
            "bank",
            "bank_name",
            "account_number",
            "status",
            "created_at",
            "decided_by",
            "decided_at",
            "decision_reason",
            "bank_account_name",
            "failure_reason",
            "finished_at",
            "can_decide",
            "can_cancel",
        ]
        read_only_fields = fields

    def _user(self):
        request = self.context.get("request")
        return request.user if request else None

    def get_amount(self, withdrawal) -> str:
        return naira(withdrawal.amount_kobo)

    def get_bank_name(self, withdrawal) -> str:
        return BANKS.get(withdrawal.bank, withdrawal.bank)

    def get_account_number(self, withdrawal) -> str:
        if self.context.get("masked"):
            return masked(withdrawal.account_number)
        return withdrawal.account_number

    def get_can_decide(self, withdrawal) -> bool:
        user = self._user()
        return bool(
            user
            and withdrawal.status == Withdrawal.Status.PENDING
            and withdrawal.member.user_id != user.pk
            and scope_contains(user, withdrawal.member, permission=C.APPROVE_WITHDRAWALS)
        )

    def get_can_cancel(self, withdrawal) -> bool:
        user = self._user()
        return bool(
            user
            and withdrawal.status == Withdrawal.Status.PENDING
            and withdrawal.member.user_id == user.pk
        )

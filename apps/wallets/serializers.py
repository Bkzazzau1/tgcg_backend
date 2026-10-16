from decimal import Decimal

from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from apps.access.services import same_person
from apps.core.permissions import Capability
from apps.geography.scope import scope_contains

from .banks import BANK_CHOICES, BANKS
from .models import KOBO, MAX_PAYMENT_KOBO, Payment, WalletEntry, masked
from .services import has_wallet, naira

C = Capability
# How much history a wallet shows at once.
ENTRIES_SHOWN = 100


class BankSerializer(serializers.Serializer):
    code = serializers.CharField()
    name = serializers.CharField()


class BankAccountSerializer(serializers.Serializer):
    """The payout account. Staff see only the last four digits."""

    bank = serializers.CharField()
    bank_name = serializers.SerializerMethodField()
    account_number = serializers.SerializerMethodField()

    def get_bank_name(self, account) -> str:
        return BANKS.get(account.bank, account.bank)

    def get_account_number(self, account) -> str:
        if self.context.get("masked"):
            return masked(account.account_number)
        return account.account_number


class BankAccountInputSerializer(serializers.Serializer):
    bank = serializers.ChoiceField(choices=BANK_CHOICES)
    account_number = serializers.CharField(max_length=20)


class WalletEntrySerializer(serializers.ModelSerializer):
    id = serializers.UUIDField(source="uuid", read_only=True)
    amount = serializers.SerializerMethodField()
    balance_after = serializers.SerializerMethodField()
    purpose = serializers.CharField(source="payment.purpose", default="", read_only=True)

    class Meta:
        model = WalletEntry
        fields = ["id", "kind", "amount", "balance_after", "purpose", "created_at"]
        read_only_fields = fields

    def get_amount(self, entry) -> str:
        return naira(entry.amount_kobo)

    def get_balance_after(self, entry) -> str:
        return naira(entry.balance_after_kobo)


class WalletSerializer(serializers.Serializer):
    """A member's wallet: the balance TGCG owes them, its history, and the
    account it is paid into, in the name on their PVC."""

    membership_number = serializers.CharField()
    has_wallet = serializers.SerializerMethodField()
    account_name = serializers.CharField(source="full_name")
    balance = serializers.SerializerMethodField()
    bank_account = serializers.SerializerMethodField()
    entries = serializers.SerializerMethodField()

    def get_has_wallet(self, member) -> bool:
        return has_wallet(member)

    def get_balance(self, member) -> str:
        wallet = getattr(member, "wallet", None)
        return naira(wallet.balance_kobo if wallet else 0)

    @extend_schema_field(BankAccountSerializer(allow_null=True))
    def get_bank_account(self, member):
        account = getattr(member, "bank_account", None)
        return BankAccountSerializer(account, context=self.context).data if account else None

    @extend_schema_field(WalletEntrySerializer(many=True))
    def get_entries(self, member):
        wallet = getattr(member, "wallet", None)
        if wallet is None:
            return []
        entries = wallet.entries.select_related("payment")[:ENTRIES_SHOWN]
        return WalletEntrySerializer(entries, many=True).data


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
        max_digits=10,
        decimal_places=2,
        min_value=Decimal("0.01"),
        max_value=Decimal(MAX_PAYMENT_KOBO) / KOBO,
        help_text="Naira.",
    )
    purpose = serializers.CharField(max_length=200)


class PaymentDecisionSerializer(serializers.Serializer):
    reason = serializers.CharField(required=False, allow_blank=True, max_length=1000)

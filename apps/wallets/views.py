from django.db import transaction
from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt
from drf_spectacular.utils import extend_schema, inline_serializer
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core.permissions import Capability, requires
from apps.geography.scope import any_of, within_scope_q
from apps.membership.models import Member
from apps.membership.registration_views import IsMember

from . import paystack, services
from .banks import BANK_CHOICES
from .models import KOBO, CreditRate, Payment, Withdrawal
from .serializers import (
    BankAccountInputSerializer,
    BankSerializer,
    ConvertCreditsSerializer,
    CreditRateInputSerializer,
    CreditRateSerializer,
    PaymentCreateSerializer,
    PaymentDecisionSerializer,
    PaymentSerializer,
    WalletSerializer,
    WithdrawalCreateSerializer,
    WithdrawalSerializer,
)

C = Capability
# Any of these shows the payments in the holder's area.
SEEING = (C.VIEW_WALLETS, C.CREDIT_WALLETS, C.APPROVE_WALLET_CREDITS)
# And these, the withdrawals.
SEEING_WITHDRAWALS = (C.VIEW_WALLETS, C.APPROVE_WITHDRAWALS)
# How many past rates the credit rates page lists.
RATES_SHOWN = 50


def _with_wallet(queryset):
    return queryset.select_related("wallet", "bank_account")


def _my_wallet(request):
    member = _with_wallet(Member.objects).get(pk=request.user.member.pk)
    return Response(WalletSerializer(member).data)


class BanksView(APIView):
    """The banks a wallet can be paid into. `payable`: withdrawals can be
    paid into it (Paystack does not pay every bank)."""

    permission_classes = [IsAuthenticated]

    @extend_schema(responses=BankSerializer(many=True))
    def get(self, request):
        return Response(
            [
                {"code": code, "name": name, "payable": services.payable(code)}
                for code, name in BANK_CHOICES
            ]
        )


class MyWalletView(APIView):
    """The signed-in member's wallet: naira, credits and their worth today,
    the rates, the payout account and the history."""

    permission_classes = [IsMember]

    @extend_schema(responses=WalletSerializer)
    def get(self, request):
        return _my_wallet(request)


class MyBankAccountView(APIView):
    """The account the member's wallet is paid into. The account name is
    always their name from the PVC."""

    permission_classes = [IsMember]

    @extend_schema(request=BankAccountInputSerializer, responses=WalletSerializer)
    def put(self, request):
        data = BankAccountInputSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        services.set_bank_account(member=request.user.member, **data.validated_data)
        return _my_wallet(request)


class ConvertCreditsView(APIView):
    """The member converts credits to naira at today's rate."""

    permission_classes = [IsMember]

    @extend_schema(request=ConvertCreditsSerializer, responses=WalletSerializer)
    def post(self, request):
        data = ConvertCreditsSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        services.convert_credits(member=request.user.member, **data.validated_data)
        return _my_wallet(request)


class MemberWalletView(APIView):
    """A member's wallet, for holders of viewWallets in their area. The
    account number shows only its last four digits."""

    permission_classes = [requires(C.VIEW_WALLETS)]

    @extend_schema(responses=WalletSerializer)
    def get(self, request, membership_number):
        members = Member.objects.filter(within_scope_q(request.user, permission=C.VIEW_WALLETS))
        member = get_object_or_404(_with_wallet(members), membership_number=membership_number)
        return Response(WalletSerializer(member, context={"masked": True}).data)


class CreditRatesView(APIView):
    """What registering a member earns, what a credit is worth and the
    smallest withdrawal: the current rates and the earlier ones. A National
    Administrator (manageCreditRates) sets new ones, from then on."""

    def get_permissions(self):
        if self.request.method == "POST":
            return [requires(C.MANAGE_CREDIT_RATES)()]
        return [requires(C.VIEW_WALLETS, C.MANAGE_CREDIT_RATES)()]

    @extend_schema(
        responses=inline_serializer(
            "CreditRates",
            {"current": CreditRateSerializer(), "history": CreditRateSerializer(many=True)},
        )
    )
    def get(self, request):
        history = CreditRate.objects.select_related("set_by")[:RATES_SHOWN]
        return Response(
            {
                "current": CreditRateSerializer(services.current_rate()).data,
                "history": CreditRateSerializer(history, many=True).data,
            }
        )

    @extend_schema(request=CreditRateInputSerializer, responses={201: CreditRateSerializer})
    def post(self, request):
        data = CreditRateInputSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        fields = data.validated_data
        rate = services.set_credit_rate(
            actor=request.user,
            credits_per_registration=fields["credits_per_registration"],
            kobo_per_credit=int(fields["naira_per_credit"] * KOBO),
            min_withdrawal_kobo=int(fields["min_withdrawal"] * KOBO),
            reason=fields["reason"],
        )
        return Response(CreditRateSerializer(rate).data, status=status.HTTP_201_CREATED)


class PaymentViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    viewsets.GenericViewSet,
):
    """Payments to members' wallets in the account's area.

    Holders of creditWallets request a payment; a different person holding
    approveWalletCredits for the member's area approves (the amount joins the
    wallet) or rejects it. Whoever requested a payment may cancel it until
    then. `can_decide` and `can_cancel` say what this account may do.
    """

    serializer_class = PaymentSerializer
    lookup_field = "uuid"
    filterset_fields = {"status": ["exact"], "member__membership_number": ["exact"]}
    search_fields = ["member__full_name", "member__membership_number", "purpose"]
    ordering_fields = ["created_at"]

    def get_permissions(self):
        if self.action in ("create", "cancel"):
            return [requires(C.CREDIT_WALLETS)()]
        if self.action in ("approve", "reject"):
            return [requires(C.APPROVE_WALLET_CREDITS)()]
        return [requires(*SEEING)()]

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return Payment.objects.none()
        user = self.request.user
        visible = any_of(
            *(within_scope_q(user, prefix="member__", permission=code) for code in SEEING)
        )
        return Payment.objects.filter(visible).select_related(
            "member", "requested_by", "decided_by"
        )

    def _payment(self, payment):
        payment = self.get_queryset().get(pk=payment.pk)
        return PaymentSerializer(payment, context={"request": self.request}).data

    @extend_schema(request=PaymentCreateSerializer, responses={201: PaymentSerializer})
    def create(self, request):
        data = PaymentCreateSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        member = Member.objects.filter(membership_number=data.validated_data["member"]).first()
        if member is None:
            raise ValidationError({"member": "No member has this membership number."})
        payment = services.request_payment(
            actor=request.user,
            member=member,
            amount_kobo=int(data.validated_data["amount"] * KOBO),
            purpose=data.validated_data["purpose"],
        )
        return Response(self._payment(payment), status=status.HTTP_201_CREATED)

    def _decide(self, request, decide):
        data = PaymentDecisionSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        payment = decide(
            actor=request.user,
            payment=self.get_object(),
            reason=data.validated_data.get("reason", ""),
        )
        return Response(self._payment(payment))

    @extend_schema(request=PaymentDecisionSerializer, responses=PaymentSerializer)
    @action(detail=True, methods=["post"])
    def approve(self, request, uuid=None):
        return self._decide(request, services.approve_payment)

    @extend_schema(request=PaymentDecisionSerializer, responses=PaymentSerializer)
    @action(detail=True, methods=["post"])
    def reject(self, request, uuid=None):
        return self._decide(request, services.reject_payment)

    @extend_schema(request=PaymentDecisionSerializer, responses=PaymentSerializer)
    @action(detail=True, methods=["post"])
    def cancel(self, request, uuid=None):
        return self._decide(request, services.cancel_payment)


class MyWithdrawalViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    viewsets.GenericViewSet,
):
    """The member's withdrawals, newest first. POST `{"amount"}` (naira)
    takes it out of the wallet at once, to be paid into the payout account
    once someone approves; `.../cancel/` takes back one not yet approved.
    One at a time."""

    permission_classes = [IsMember]
    serializer_class = WithdrawalSerializer
    lookup_field = "uuid"
    filterset_fields = {"status": ["exact"]}

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return Withdrawal.objects.none()
        member = self.request.user.member
        return Withdrawal.objects.filter(member=member).select_related("member", "decided_by")

    def _withdrawal(self, withdrawal, **extra):
        withdrawal = self.get_queryset().get(pk=withdrawal.pk)
        return Response(
            WithdrawalSerializer(withdrawal, context={"request": self.request}).data, **extra
        )

    @extend_schema(request=WithdrawalCreateSerializer, responses={201: WithdrawalSerializer})
    def create(self, request):
        data = WithdrawalCreateSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        withdrawal = services.request_withdrawal(
            member=request.user.member,
            amount_kobo=int(data.validated_data["amount"] * KOBO),
        )
        return self._withdrawal(withdrawal, status=status.HTTP_201_CREATED)

    @extend_schema(request=None, responses=WithdrawalSerializer)
    @action(detail=True, methods=["post"])
    def cancel(self, request, uuid=None):
        withdrawal = services.cancel_withdrawal(
            member=request.user.member, withdrawal=self.get_object()
        )
        return self._withdrawal(withdrawal)


# Approving sends money: its steps are saved one by one, never rolled back together.
@method_decorator(transaction.non_atomic_requests, name="dispatch")
class WithdrawalViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    viewsets.GenericViewSet,
):
    """Withdrawals by members in the account's area (viewWallets or
    approveWithdrawals), with the account number masked.

    A different person holding approveWithdrawals for the member's area
    approves one (the money is sent through Paystack at once, if the bank
    holds the account in the member's name) or rejects it (it goes back to
    the wallet). `can_decide` says whether this account may.
    """

    serializer_class = WithdrawalSerializer
    lookup_field = "uuid"
    filterset_fields = {"status": ["exact"], "member__membership_number": ["exact"]}
    search_fields = ["member__full_name", "member__membership_number"]
    ordering_fields = ["created_at"]

    def get_permissions(self):
        if self.action in ("approve", "reject"):
            return [requires(C.APPROVE_WITHDRAWALS)()]
        return [requires(*SEEING_WITHDRAWALS)()]

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return Withdrawal.objects.none()
        user = self.request.user
        visible = any_of(
            *(
                within_scope_q(user, prefix="member__", permission=code)
                for code in SEEING_WITHDRAWALS
            )
        )
        return Withdrawal.objects.filter(visible).select_related("member", "decided_by")

    def get_serializer_context(self):
        return {**super().get_serializer_context(), "masked": True}

    def _withdrawal(self, withdrawal):
        withdrawal = self.get_queryset().get(pk=withdrawal.pk)
        return Response(self.get_serializer(withdrawal).data)

    @extend_schema(request=PaymentDecisionSerializer, responses=WithdrawalSerializer)
    @action(detail=True, methods=["post"])
    def approve(self, request, uuid=None):
        data = PaymentDecisionSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        try:
            withdrawal = services.approve_withdrawal(
                actor=request.user,
                withdrawal=self.get_object(),
                reason=data.validated_data.get("reason", ""),
            )
        except services.PaymentsUnavailable:
            detail = "Withdrawals cannot be paid yet: Paystack is not set up on the server."
            return Response({"detail": detail}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        return self._withdrawal(withdrawal)

    @extend_schema(request=PaymentDecisionSerializer, responses=WithdrawalSerializer)
    @action(detail=True, methods=["post"])
    def reject(self, request, uuid=None):
        data = PaymentDecisionSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        withdrawal = services.reject_withdrawal(
            actor=request.user,
            withdrawal=self.get_object(),
            reason=data.validated_data.get("reason", ""),
        )
        return self._withdrawal(withdrawal)


@method_decorator(csrf_exempt, name="dispatch")
class PaystackWebhookView(View):
    """Events Paystack posts (a transfer paid, failed or reversed), signed
    with this server's Paystack secret key. Set it as the webhook URL in the
    Paystack dashboard."""

    def post(self, request):
        try:
            event = paystack.read_webhook(
                request.body, request.headers.get("X-Paystack-Signature", "")
            )
        except (paystack.PaystackError, paystack.NotConfigured):
            return HttpResponse(status=401)
        services.handle_paystack_event(event)
        return HttpResponse(status=200)

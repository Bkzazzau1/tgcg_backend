from django.shortcuts import get_object_or_404
from drf_spectacular.utils import extend_schema
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

from . import services
from .banks import BANK_CHOICES
from .models import KOBO, Payment
from .serializers import (
    BankAccountInputSerializer,
    BankSerializer,
    PaymentCreateSerializer,
    PaymentDecisionSerializer,
    PaymentSerializer,
    WalletSerializer,
)

C = Capability
# Any of these shows the payments in the holder's area.
SEEING = (C.VIEW_WALLETS, C.CREDIT_WALLETS, C.APPROVE_WALLET_CREDITS)


def _with_wallet(queryset):
    return queryset.select_related("wallet", "bank_account")


class BanksView(APIView):
    """The banks a wallet can be paid into."""

    permission_classes = [IsAuthenticated]

    @extend_schema(responses=BankSerializer(many=True))
    def get(self, request):
        return Response([{"code": code, "name": name} for code, name in BANK_CHOICES])


class MyWalletView(APIView):
    """The signed-in member's wallet."""

    permission_classes = [IsMember]

    @extend_schema(responses=WalletSerializer)
    def get(self, request):
        member = _with_wallet(Member.objects).get(pk=request.user.member.pk)
        return Response(WalletSerializer(member).data)


class MyBankAccountView(APIView):
    """The account the member's wallet is paid into. The account name is
    always their name from the PVC."""

    permission_classes = [IsMember]

    @extend_schema(request=BankAccountInputSerializer, responses=WalletSerializer)
    def put(self, request):
        data = BankAccountInputSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        services.set_bank_account(member=request.user.member, **data.validated_data)
        member = _with_wallet(Member.objects).get(pk=request.user.member.pk)
        return Response(WalletSerializer(member).data)


class MemberWalletView(APIView):
    """A member's wallet, for holders of viewWallets in their area. The
    account number shows only its last four digits."""

    permission_classes = [requires(C.VIEW_WALLETS)]

    @extend_schema(responses=WalletSerializer)
    def get(self, request, membership_number):
        members = Member.objects.filter(within_scope_q(request.user, permission=C.VIEW_WALLETS))
        member = get_object_or_404(_with_wallet(members), membership_number=membership_number)
        return Response(WalletSerializer(member, context={"masked": True}).data)


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

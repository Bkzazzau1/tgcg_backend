"""Registration, member-home and card-verification endpoints.

Views that check SMS codes are wrapped with `transaction.non_atomic_requests`
in config/urls.py so failed code attempts are recorded.
"""

import mimetypes

from django.db import transaction
from django.http import FileResponse, Http404
from drf_spectacular.utils import OpenApiParameter, extend_schema, inline_serializer
from rest_framework import generics, serializers, status
from rest_framework.exceptions import PermissionDenied
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import AllowAny, BasePermission
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView
from rest_framework_simplejwt.tokens import RefreshToken

from apps.announcements.serializers import AnnouncementSerializer
from apps.announcements.views import announcements_for
from apps.core.permissions import Capability, has_capability, is_staff_operator
from apps.geography.models import PollingUnit

from . import pvc_reading, registration, services
from .models import Member, PollingUnitChangeRequest
from .serializers import (
    AssistedRegistrationSerializer,
    CardVerificationSerializer,
    MemberCardSerializer,
    MemberSelfUpdateSerializer,
    MemberSerializer,
    PinCodeSerializer,
    PollingUnitChangeRequestSerializer,
    PollingUnitChangeSerializer,
    PollingUnitSummarySerializer,
    RegistrationCodeSerializer,
    SelfRegistrationSerializer,
    SetPinSerializer,
    VolunteerApplicationSerializer,
    VolunteerOfferSerializer,
    member_from_card_token,
)

C = Capability
CODE_SENT = inline_serializer("CodeSent", {"expires_in": serializers.IntegerField()})
MEMBER_SESSION = inline_serializer(
    "MemberSession",
    {
        "access": serializers.CharField(),
        "refresh": serializers.CharField(),
        "member": MemberCardSerializer(),
    },
)


def _member_queryset():
    return Member.objects.select_related(
        "polling_unit__ward__lga__state", "zone", "state", "senatorial_district", "lga", "ward"
    ).prefetch_related("accreditations")


def _session(member, user, request):
    refresh = RefreshToken.for_user(user)
    member = _member_queryset().get(pk=member.pk)
    return {
        "access": str(refresh.access_token),
        "refresh": str(refresh),
        "member": MemberCardSerializer(member, context={"request": request}).data,
    }


class PublicRegistrationView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "registration"


class RegistrationCodeView(PublicRegistrationView):
    """Step: send an SMS code to the new member's phone (checks phone and VIN are free)."""

    @extend_schema(request=RegistrationCodeSerializer, responses={200: CODE_SENT})
    def post(self, request):
        serializer = RegistrationCodeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        expires_in = registration.request_registration_code(**serializer.validated_data)
        return Response({"expires_in": expires_in})


class SelfRegistrationView(PublicRegistrationView):
    """Final step of in-app registration. Returns a signed-in member session."""

    parser_classes = [MultiPartParser, FormParser]

    @extend_schema(
        request={"multipart/form-data": SelfRegistrationSerializer},
        responses={201: MEMBER_SESSION},
    )
    def post(self, request):
        serializer = SelfRegistrationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        member, user = registration.self_register(**serializer.validated_data)
        return Response(_session(member, user, request), status=status.HTTP_201_CREATED)


class DelimLookupView(PublicRegistrationView):
    """Show which polling unit a PVC's polling unit code belongs to, before registering."""

    throttle_scope = "delim_lookup"

    @extend_schema(
        parameters=[OpenApiParameter("delim", str, required=True, description="e.g. 18/23/01/001")],
        responses={200: PollingUnitSummarySerializer},
    )
    def get(self, request):
        unit = registration.polling_unit_from_delim(request.query_params.get("delim", ""))
        return Response(PollingUnitSummarySerializer(unit, context={"hide_location": True}).data)


class ReadPvcView(PublicRegistrationView):
    """Read the text on a PVC photo, for registering from a browser (see
    apps.membership.pvc_reading). The app picks out the fields; the member
    checks them. Nothing is stored."""

    throttle_scope = "pvc_reading"
    parser_classes = [MultiPartParser, FormParser]

    @extend_schema(
        request={
            "multipart/form-data": inline_serializer(
                "PvcPhoto", {"image": serializers.ImageField()}
            )
        },
        responses={200: inline_serializer("PvcText", {"text": serializers.CharField()})},
    )
    def post(self, request):
        upload = request.FILES.get("image")
        registration.validate_image(upload, "image", required=True)
        try:
            text = pvc_reading.read_text(upload)
        except pvc_reading.ReaderUnavailable:
            detail = "Reading cards is not set up on this server. Type the details from the card."
            return Response({"detail": detail}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        except pvc_reading.ReadFailed:
            detail = "The card could not be read just now. Type the details from the card."
            return Response({"detail": detail}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        return Response({"text": text})


class PinCodeView(PublicRegistrationView):
    """Send a code to set or reset a member's PIN (same reply for unknown phones)."""

    @extend_schema(request=PinCodeSerializer, responses={200: CODE_SENT})
    def post(self, request):
        serializer = PinCodeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return Response({"expires_in": registration.request_pin_code(**serializer.validated_data)})


class SetPinView(PublicRegistrationView):
    @extend_schema(request=SetPinSerializer, responses={200: MEMBER_SESSION})
    def post(self, request):
        serializer = SetPinSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        member, user = registration.set_pin(**serializer.validated_data)
        return Response(_session(member, user, request))


class AssistedRegistrationView(APIView):
    """A coordinator registers a member in person; the PVC is optional."""

    parser_classes = [MultiPartParser, FormParser]

    def check_permissions(self, request):
        super().check_permissions(request)
        if not has_capability(request.user, C.REGISTER_MEMBERS, C.MANAGE_MEMBERSHIP):
            raise PermissionDenied("Your role does not permit registering members.")

    @extend_schema(
        request={"multipart/form-data": AssistedRegistrationSerializer},
        responses={201: MemberSerializer},
    )
    def post(self, request):
        serializer = AssistedRegistrationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        member = registration.assisted_register(actor=request.user, **serializer.validated_data)
        member = _member_queryset().get(pk=member.pk)
        return Response(
            MemberSerializer(member, context={"request": request}).data,
            status=status.HTTP_201_CREATED,
        )


# --- member home ---------------------------------------------------------------


class IsMember(BasePermission):
    message = "Only members have a member home."

    def has_permission(self, request, view):
        user = request.user
        return bool(user and user.is_authenticated and hasattr(user, "member"))


class MemberHomeMixin:
    permission_classes = [IsMember]

    def member(self):
        return _member_queryset().get(pk=self.request.user.member.pk)


class MyMemberView(MemberHomeMixin, generics.RetrieveUpdateAPIView):
    """The signed-in member's profile and membership card."""

    http_method_names = ["get", "patch", "head", "options"]
    parser_classes = [JSONParser]

    def get_object(self):
        return self.member()

    def get_serializer_class(self):
        return (
            MemberSelfUpdateSerializer if self.request.method == "PATCH" else MemberCardSerializer
        )

    @extend_schema(request=MemberSelfUpdateSerializer, responses=MemberCardSerializer)
    def patch(self, request, *args, **kwargs):
        serializer = MemberSelfUpdateSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        with transaction.atomic():
            # Lock and save only the edited fields, so a suspension or review
            # decided meanwhile is never written back over.
            member = Member.objects.select_for_update().get(pk=request.user.member.pk)
            for name, value in serializer.validated_data.items():
                setattr(member, name, value)
            member.full_clean()
            member.save(update_fields=[*serializer.validated_data, "updated_at"])
        return Response(MemberCardSerializer(self.member(), context={"request": request}).data)


def image_response(image):
    """Serve a private image (never cached)."""
    if not image:
        raise Http404
    content_type = mimetypes.guess_type(image.name)[0] or "image/jpeg"
    response = FileResponse(image.open("rb"), content_type=content_type)
    response["Cache-Control"] = "private, no-store"
    return response


def _photo_response(member):
    return image_response(member.photo)


class MyMemberPhotoView(MemberHomeMixin, APIView):
    @extend_schema(responses={(200, "image/jpeg"): bytes})
    def get(self, request):
        return _photo_response(self.member())


class MyPollingUnitView(MemberHomeMixin, APIView):
    """The member's polling unit and how many TGCG members share it."""

    @extend_schema(
        responses=inline_serializer(
            "MyPollingUnit",
            {
                "polling_unit": PollingUnitSummarySerializer(allow_null=True),
                "members_in_unit": serializers.IntegerField(),
                "members_in_ward": serializers.IntegerField(),
            },
        )
    )
    def get(self, request):
        member = self.member()
        unit = None
        if member.polling_unit_id:
            unit = PollingUnit.objects.select_related("ward__lga__state").get(
                pk=member.polling_unit_id
            )
        return Response(
            {
                "polling_unit": PollingUnitSummarySerializer(unit).data if unit else None,
                "members_in_unit": (
                    Member.objects.filter(polling_unit_id=member.polling_unit_id).count()
                    if member.polling_unit_id
                    else 0
                ),
                "members_in_ward": (
                    Member.objects.filter(ward_id=member.ward_id).count() if member.ward_id else 0
                ),
            }
        )


class MyAnnouncementsView(MemberHomeMixin, generics.ListAPIView):
    serializer_class = AnnouncementSerializer

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return announcements_for(Member())
        return announcements_for(self.member())


class MyVolunteerView(MemberHomeMixin, APIView):
    """The member's offers of party work; POST to offer help.

    Volunteering is not a route to becoming an agent: coordinators accredit
    agents separately, and agents sign in with their own agent accounts.
    """

    @extend_schema(responses=VolunteerApplicationSerializer(many=True))
    def get(self, request):
        offers = self.member().volunteering.select_related(
            "member__zone", "member__state", "member__lga", "member__ward", "member__polling_unit"
        )
        return Response(
            VolunteerApplicationSerializer(offers, many=True, context={"request": request}).data
        )

    @extend_schema(
        request=VolunteerOfferSerializer, responses={201: VolunteerApplicationSerializer}
    )
    def post(self, request):
        serializer = VolunteerOfferSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        offer = services.apply_to_volunteer(member=self.member(), **serializer.validated_data)
        return Response(
            VolunteerApplicationSerializer(offer, context={"request": request}).data,
            status=status.HTTP_201_CREATED,
        )


class MyVolunteerWithdrawView(MemberHomeMixin, APIView):
    """Take back an offer the coordinators have not answered yet."""

    @extend_schema(request=None, responses=VolunteerApplicationSerializer)
    def post(self, request, uuid):
        member = self.member()
        offer = member.volunteering.filter(uuid=uuid).first()
        if offer is None:
            raise Http404
        offer = services.withdraw_volunteering(member=member, application=offer)
        return Response(VolunteerApplicationSerializer(offer, context={"request": request}).data)


# --- card verification (staff scan a member's QR code) --------------------------

TOKEN_PARAM = OpenApiParameter("token", str, required=True, description="Value from the QR code")


def _member_for_token(request):
    if not is_staff_operator(request.user):
        raise PermissionDenied("Only TGCG staff can verify membership cards.")
    token = request.query_params.get("token", "")
    member = member_from_card_token(token)
    if member is None:
        raise Http404("This is not a valid TGCG membership card.")
    return member, token


class CardVerificationView(APIView):
    @extend_schema(parameters=[TOKEN_PARAM], responses=CardVerificationSerializer)
    def get(self, request):
        member, token = _member_for_token(request)
        return Response(
            CardVerificationSerializer(member, context={"request": request, "token": token}).data
        )


class CardPhotoView(APIView):
    @extend_schema(parameters=[TOKEN_PARAM], responses={(200, "image/jpeg"): bytes})
    def get(self, request):
        member, _ = _member_for_token(request)
        return _photo_response(member)


# --- registered polling unit changes (after an INEC transfer) -------------------


class MyPollingUnitChangeView(MemberHomeMixin, APIView):
    """The member's requests to move their registered polling unit; POST the
    code and a photo of the updated voter card to ask for one."""

    parser_classes = [MultiPartParser, FormParser, JSONParser]

    @extend_schema(responses=PollingUnitChangeSerializer(many=True))
    def get(self, request):
        changes = self.member().polling_unit_changes.select_related(
            "member", "polling_unit", "from_polling_unit", "decided_by"
        )
        return Response(PollingUnitChangeSerializer(changes, many=True).data)

    @extend_schema(
        request={"multipart/form-data": PollingUnitChangeRequestSerializer},
        responses={201: PollingUnitChangeSerializer},
    )
    def post(self, request):
        serializer = PollingUnitChangeRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        change = services.request_polling_unit_change(
            member=self.member(), **serializer.validated_data
        )
        change = PollingUnitChangeRequest.objects.select_related(
            "member", "polling_unit", "from_polling_unit"
        ).get(pk=change.pk)
        return Response(PollingUnitChangeSerializer(change).data, status=status.HTTP_201_CREATED)


class MyPollingUnitChangeWithdrawView(MemberHomeMixin, APIView):
    """Take back a change request that has not been reviewed yet."""

    @extend_schema(request=None, responses=PollingUnitChangeSerializer)
    def post(self, request, uuid):
        member = self.member()
        change = member.polling_unit_changes.filter(uuid=uuid).first()
        if change is None:
            raise Http404
        change = services.withdraw_polling_unit_change(member=member, change=change)
        return Response(PollingUnitChangeSerializer(change).data)

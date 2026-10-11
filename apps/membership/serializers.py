from django.core import signing
from django.urls import reverse
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from apps.core.choices import AccreditationStatus, Gender
from apps.geography.scope import ScopeField, scope_payload, within_scope_q
from apps.geography.serializers import NavigationSerializer, navigation_for

from .models import (
    ACCREDITABLE_ROLES,
    AccreditationEvent,
    AccreditedAgent,
    Member,
    VolunteerApplication,
)

CARD_SALT = "tgcg.member-card"


def card_token(member: Member) -> str:
    """Signed value printed in the card's QR code; unguessable, unlike numbers."""
    return signing.dumps(str(member.uuid), salt=CARD_SALT, compress=True)


def member_from_card_token(token: str) -> Member | None:
    try:
        member_uuid = signing.loads(token, salt=CARD_SALT)
    except signing.BadSignature:
        return None
    return (
        Member.objects.select_related("polling_unit", "ward", "lga", "state", "zone")
        .filter(uuid=member_uuid)
        .first()
    )


def masked_vin(member: Member) -> str | None:
    return f"{'*' * 15}{member.vin[-4:]}" if member.vin else None


def _absolute(serializer, path):
    request = serializer.context.get("request")
    return request.build_absolute_uri(path) if request else path


def flatten_scope(validated_data: dict) -> dict:
    """Turn the ScopeField's nested attrs into model field kwargs."""
    data = dict(validated_data)
    data.update(data.pop("scope", {}))
    return data


class ActiveAccreditationSerializer(serializers.ModelSerializer):
    class Meta:
        model = AccreditedAgent
        fields = ["agent_id", "role", "status"]


class MemberSerializer(serializers.ModelSerializer):
    """Staff view of a member. Only the last 4 digits of the VIN are shown."""

    id = serializers.UUIDField(source="uuid", read_only=True)
    scope = ScopeField(required=False)
    vin = serializers.SerializerMethodField()
    has_pvc = serializers.BooleanField(read_only=True)
    phone_verified = serializers.SerializerMethodField()
    has_app_account = serializers.SerializerMethodField()
    photo_url = serializers.SerializerMethodField()
    active_accreditation = serializers.SerializerMethodField()

    class Meta:
        model = Member
        fields = [
            "id",
            "membership_number",
            "full_name",
            "phone_number",
            "email",
            "vin",
            "has_pvc",
            "gender",
            "date_of_birth",
            "occupation",
            "status",
            "registration_channel",
            "phone_verified",
            "has_app_account",
            "origin",
            "client_id",
            "scope",
            "photo_url",
            "active_accreditation",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "membership_number",
            "phone_number",
            "registration_channel",
            "origin",
            "client_id",
            "created_at",
            "updated_at",
        ]
        # Uniqueness is enforced by the model/service so offline retries
        # with the same client_id are idempotent instead of rejected.
        extra_kwargs = {
            "phone_number": {"validators": []},
            "client_id": {"validators": []},
        }
        validators = []

    def get_vin(self, member) -> str | None:
        return masked_vin(member)

    def get_phone_verified(self, member) -> bool:
        return member.phone_verified_at is not None

    def get_has_app_account(self, member) -> bool:
        return member.user_id is not None

    def get_photo_url(self, member) -> str | None:
        if not member.photo:
            return None
        path = reverse("member-photo", kwargs={"membership_number": member.membership_number})
        return _absolute(self, path)

    def get_active_accreditation(self, member) -> dict | None:
        return _active_accreditation(member)


def _active_accreditation(member):
    for agent in member.accreditations.all():
        if agent.status in (AccreditationStatus.PENDING, AccreditationStatus.APPROVED):
            return ActiveAccreditationSerializer(agent).data
    return None


class AssistedRegistrationSerializer(serializers.Serializer):
    """A coordinator registers someone in person (multipart form)."""

    phone_number = serializers.CharField(max_length=20)
    code = serializers.CharField(max_length=6, help_text="SMS code read out by the member.")
    full_name = serializers.CharField(max_length=150)
    gender = serializers.ChoiceField(choices=Gender.choices)
    date_of_birth = serializers.DateField(required=False, allow_null=True)
    occupation = serializers.CharField(max_length=100, required=False, allow_blank=True)
    email = serializers.EmailField(required=False, allow_blank=True)
    consent = serializers.BooleanField()
    photo = serializers.FileField(help_text="Photo of the member taken at registration.")
    vin = serializers.CharField(max_length=25, required=False, allow_blank=True)
    delim = serializers.CharField(
        max_length=20, required=False, allow_blank=True, help_text="PVC DELIM, e.g. 18/23/01/001"
    )
    pvc_image = serializers.FileField(required=False, allow_null=True)
    scope = ScopeField(required=False, help_text="Required without a PVC: LGA, ward or unit.")
    client_id = serializers.CharField(max_length=64, required=False, allow_null=True)


class SelfRegistrationSerializer(serializers.Serializer):
    """A member registers themselves in the app from their scanned PVC."""

    phone_number = serializers.CharField(max_length=20)
    code = serializers.CharField(max_length=6)
    vin = serializers.CharField(max_length=25)
    delim = serializers.CharField(max_length=20, help_text="PVC DELIM, e.g. 18/23/01/001")
    full_name = serializers.CharField(max_length=150)
    gender = serializers.ChoiceField(choices=Gender.choices)
    date_of_birth = serializers.DateField(required=False, allow_null=True)
    occupation = serializers.CharField(max_length=100, required=False, allow_blank=True)
    email = serializers.EmailField(required=False, allow_blank=True)
    pin = serializers.CharField(max_length=6, min_length=6, write_only=True)
    consent = serializers.BooleanField()
    photo = serializers.FileField(help_text="Selfie taken in the app.")
    pvc_image = serializers.FileField(help_text="Photo of the front of the PVC.")


class RegistrationCodeSerializer(serializers.Serializer):
    phone_number = serializers.CharField(max_length=20)
    vin = serializers.CharField(max_length=25, required=False, allow_blank=True)


class PinCodeSerializer(serializers.Serializer):
    phone_number = serializers.CharField(max_length=20)


class SetPinSerializer(serializers.Serializer):
    phone_number = serializers.CharField(max_length=20)
    code = serializers.CharField(max_length=6)
    pin = serializers.CharField(max_length=6, min_length=6, write_only=True)


class PollingUnitSummarySerializer(serializers.Serializer):
    code = serializers.CharField()
    name = serializers.CharField()
    location = serializers.CharField()
    ward = serializers.CharField(source="ward.name")
    ward_code = serializers.CharField(source="ward.code")
    lga = serializers.CharField(source="ward.lga.name")
    state = serializers.CharField(source="ward.lga.state.name")
    navigation = serializers.SerializerMethodField()

    @extend_schema_field(NavigationSerializer(allow_null=True))
    def get_navigation(self, unit):
        # A DELIM lookup during public registration shows the unit, not its location.
        if self.context.get("hide_location"):
            return None
        return navigation_for(unit)


class MemberCardSerializer(serializers.ModelSerializer):
    """What a member sees about themselves; also the data on the QR card."""

    scope = ScopeField(read_only=True)
    vin = serializers.SerializerMethodField()
    has_pvc = serializers.BooleanField(read_only=True)
    polling_unit = PollingUnitSummarySerializer(read_only=True)
    member_since = serializers.DateTimeField(source="created_at", read_only=True)
    photo_url = serializers.SerializerMethodField()
    qr_token = serializers.SerializerMethodField()

    class Meta:
        model = Member
        # No agent details: membership and agent accreditation are separate.
        fields = [
            "membership_number",
            "full_name",
            "phone_number",
            "email",
            "vin",
            "has_pvc",
            "gender",
            "date_of_birth",
            "occupation",
            "status",
            "scope",
            "polling_unit",
            "member_since",
            "photo_url",
            "qr_token",
        ]
        read_only_fields = [
            "membership_number",
            "full_name",
            "phone_number",
            "gender",
            "date_of_birth",
            "status",
        ]

    def get_vin(self, member) -> str | None:
        return masked_vin(member)

    def get_photo_url(self, member) -> str | None:
        return _absolute(self, reverse("my-member-photo")) if member.photo else None

    def get_qr_token(self, member) -> str:
        return card_token(member)


class MemberSelfUpdateSerializer(serializers.ModelSerializer):
    class Meta:
        model = Member
        fields = ["email", "occupation"]


class CardVerificationSerializer(serializers.ModelSerializer):
    """Shown to a coordinator who scans a membership card's QR code."""

    valid = serializers.SerializerMethodField()
    has_pvc = serializers.BooleanField(read_only=True)
    scope = ScopeField(read_only=True)
    photo_url = serializers.SerializerMethodField()
    member_since = serializers.DateTimeField(source="created_at")

    class Meta:
        model = Member
        fields = [
            "valid",
            "membership_number",
            "full_name",
            "status",
            "has_pvc",
            "scope",
            "member_since",
            "photo_url",
        ]

    def get_valid(self, member) -> bool:
        return member.status not in ("rejected", "archived")

    def get_photo_url(self, member) -> str | None:
        if not member.photo:
            return None
        return _absolute(self, f"{reverse('member-card-photo')}?token={self.context['token']}")


class VolunteerApplicationSerializer(serializers.ModelSerializer):
    """A member's offer of party work."""

    id = serializers.UUIDField(source="uuid", read_only=True)
    member = serializers.CharField(source="member.membership_number", read_only=True)
    member_name = serializers.CharField(source="member.full_name", read_only=True)
    member_phone = serializers.CharField(source="member.phone_number", read_only=True)
    scope = serializers.SerializerMethodField(help_text="Where the member is.")
    interests = serializers.ListField(
        child=serializers.ChoiceField(choices=VolunteerApplication.Interest.choices)
    )

    class Meta:
        model = VolunteerApplication
        fields = [
            "id",
            "member",
            "member_name",
            "member_phone",
            "scope",
            "interests",
            "message",
            "status",
            "decision_reason",
            "decided_at",
            "created_at",
        ]
        read_only_fields = ["status", "decision_reason", "decided_at", "created_at"]

    @extend_schema_field(OpenApiTypes.OBJECT)
    def get_scope(self, application) -> dict:
        return scope_payload(application.member)


class VolunteerOfferSerializer(serializers.Serializer):
    """What a member sends to volunteer."""

    interests = serializers.ListField(
        child=serializers.ChoiceField(choices=VolunteerApplication.Interest.choices),
        allow_empty=False,
        help_text="mobilisation, campaign, logistics, publicity and/or electionDay",
    )
    message = serializers.CharField(required=False, allow_blank=True, max_length=1000)


class DecisionSerializer(serializers.Serializer):
    reason = serializers.CharField(required=False, allow_blank=True, max_length=1000)


class ScopedMemberField(serializers.SlugRelatedField):
    """Member by membership number, limited to the requesting user's scope."""

    def get_queryset(self):
        user = self.context["request"].user
        return Member.objects.filter(within_scope_q(user))


class AgentSerializer(serializers.ModelSerializer):
    id = serializers.UUIDField(source="uuid", read_only=True)
    member = ScopedMemberField(slug_field="membership_number")
    member_name = serializers.CharField(source="member.full_name", read_only=True)
    role = serializers.ChoiceField(choices=[(r.value, r.label) for r in ACCREDITABLE_ROLES])
    scope = ScopeField()
    is_ready = serializers.BooleanField(read_only=True)

    class Meta:
        model = AccreditedAgent
        fields = [
            "id",
            "agent_id",
            "member",
            "member_name",
            "role",
            "status",
            "scope",
            "registered_phone_number",
            "device_id",
            "sim_fingerprint",
            "biometric_enrolled",
            "training_completed",
            "is_ready",
            "origin",
            "client_id",
            "status_changed_at",
            "created_at",
        ]
        read_only_fields = ["agent_id", "status", "status_changed_at", "created_at"]
        extra_kwargs = {"client_id": {"validators": []}}
        # Accreditation constraints are enforced by the model and service.
        validators = []


class ReadinessSerializer(serializers.ModelSerializer):
    class Meta:
        model = AccreditedAgent
        fields = ["device_id", "sim_fingerprint", "biometric_enrolled", "training_completed"]


class StatusChangeSerializer(serializers.Serializer):
    status = serializers.ChoiceField(choices=AccreditationStatus.choices)
    reason = serializers.CharField(required=False, allow_blank=True, max_length=1000)


class AccreditationEventSerializer(serializers.ModelSerializer):
    actor = serializers.SlugRelatedField(slug_field="access_id", read_only=True)

    class Meta:
        model = AccreditationEvent
        fields = ["from_status", "to_status", "reason", "actor", "created_at"]

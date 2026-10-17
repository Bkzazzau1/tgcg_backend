import json

from django.core import signing
from django.core.exceptions import ValidationError as DjangoValidationError
from django.urls import reverse
from django.utils import timezone
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema_field, inline_serializer
from rest_framework import serializers

from apps.accounts.phone import normalize_ng_phone
from apps.core.choices import AccreditationStatus, Disability, Gender
from apps.core.permissions import Capability
from apps.geography.scope import ScopeField, scope_contains, scope_payload, within_scope_q
from apps.geography.serializers import NavigationSerializer, navigation_for

from .card import card_missing
from .identity import verified_automatically
from .models import (
    ACCREDITABLE_ROLES,
    AccreditationEvent,
    AccreditedAgent,
    Member,
    PollingUnitChangeRequest,
    PvcUpdate,
    VerificationCheck,
    VolunteerApplication,
    clean_disabilities,
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


# Personal details shown only with viewMemberSensitiveData for the member's area.
SENSITIVE_MEMBER_FIELDS = (
    "phone_number",
    "email",
    "vin",
    "date_of_birth",
    "photo_url",
    "disabilities",
    "disability_other",
)


@extend_schema_field({"type": "array", "items": {"type": "string", "enum": Disability.values}})
class DisabilitiesField(serializers.Field):
    """A list of Disability codes. Multipart forms send it as JSON text."""

    default_error_messages = {"invalid": "Send the disabilities as a list."}

    def to_internal_value(self, data):
        if isinstance(data, str):
            try:
                data = json.loads(data) if data.strip() else []
            except ValueError:
                self.fail("invalid")
        if not isinstance(data, list) or not all(isinstance(kind, str) for kind in data):
            self.fail("invalid")
        return data

    def to_representation(self, value):
        return list(value or [])


class DisabilitySerializerMixin:
    """Checks `disabilities` and `disability_other` together, as the member
    model does, so a wrong choice is reported against its field."""

    def validate(self, attrs):
        attrs = super().validate(attrs)
        if "disabilities" not in attrs and "disability_other" not in attrs:
            return attrs
        if "disabilities" not in attrs:
            raise serializers.ValidationError(
                {"disabilities": "Send the disabilities with the words for other."}
            )
        try:
            attrs["disabilities"], attrs["disability_other"] = clean_disabilities(
                attrs["disabilities"], attrs.get("disability_other", "")
            )
        except DjangoValidationError as error:
            raise serializers.ValidationError(error.message_dict) from None
        return attrs


def may_see_sensitive(serializer, member) -> bool:
    """The member themselves, or holders of viewMemberSensitiveData for their area."""
    request = serializer.context.get("request")
    user = getattr(request, "user", None)
    if not getattr(user, "is_authenticated", False):
        return False
    if member.user_id and member.user_id == user.pk:
        return True
    return scope_contains(user, member, permission=Capability.VIEW_MEMBER_SENSITIVE_DATA)


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
    """Staff view of a member. Only the last 4 digits of the VIN are shown.

    Phone, email, VIN, date of birth and photo are withheld (null) unless the
    caller holds viewMemberSensitiveData for the member's area.
    """

    # Minimal USSD records may have no name; staff writes still require a real name.
    full_name = serializers.CharField(max_length=150)
    id = serializers.UUIDField(source="uuid", read_only=True)
    scope = ScopeField(required=False)
    phone_number = serializers.CharField(read_only=True, allow_null=True)
    email = serializers.EmailField(required=False, allow_blank=True, allow_null=True)
    vin = serializers.SerializerMethodField()
    sensitive_visible = serializers.SerializerMethodField(
        help_text="False when personal details were withheld."
    )
    roles = serializers.SerializerMethodField(help_text="Current appointments.")
    reason = serializers.CharField(
        write_only=True,
        required=False,
        allow_blank=True,
        max_length=1000,
        help_text="Required when a change sends a verified identity back to review.",
    )
    has_pvc = serializers.BooleanField(read_only=True)
    disabilities = DisabilitiesField(read_only=True)
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
            "disabilities",
            "disability_other",
            "status",
            "verification_status",
            "membership_state",
            "registration_channel",
            "phone_verified",
            "has_app_account",
            "origin",
            "client_id",
            "scope",
            "photo_url",
            "sensitive_visible",
            "roles",
            "reason",
            "active_accreditation",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "membership_number",
            "phone_number",
            "disability_other",
            "verification_status",
            "membership_state",
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

    def get_sensitive_visible(self, member) -> bool:
        return may_see_sensitive(self, member)

    @extend_schema_field(OpenApiTypes.OBJECT)
    def get_roles(self, member) -> list:
        return [
            {"role": a.role.code, "role_name": a.role.name, "state": a.effective_status()}
            for a in member.role_assignments.all()
            if a.status in ("active", "suspended")
        ]

    def validate_email(self, value):
        return value or ""

    def to_representation(self, member):
        data = super().to_representation(member)
        if not data["sensitive_visible"]:
            for field in SENSITIVE_MEMBER_FIELDS:
                data[field] = None
        return data


def _active_accreditation(member):
    for agent in member.accreditations.all():
        if agent.status in (AccreditationStatus.PENDING, AccreditationStatus.APPROVED):
            return ActiveAccreditationSerializer(agent).data
    return None


class AssistedRegistrationSerializer(DisabilitySerializerMixin, serializers.Serializer):
    """A coordinator registers someone in person (multipart form)."""

    phone_number = serializers.CharField(max_length=20)
    code = serializers.CharField(max_length=6, help_text="SMS code read out by the member.")
    full_name = serializers.CharField(max_length=150)
    gender = serializers.ChoiceField(choices=Gender.choices)
    date_of_birth = serializers.DateField(required=False, allow_null=True)
    occupation = serializers.CharField(max_length=100, required=False, allow_blank=True)
    disabilities = DisabilitiesField(required=False)
    disability_other = serializers.CharField(max_length=100, required=False, allow_blank=True)
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


class SelfRegistrationSerializer(DisabilitySerializerMixin, serializers.Serializer):
    """A member registers themselves in the app. As on USSD, joining needs the
    phone, the PVC's VIN and polling unit code, a name and a PIN; the rest is
    optional."""

    phone_number = serializers.CharField(max_length=20)
    code = serializers.CharField(
        max_length=6,
        required=False,
        allow_blank=True,
        help_text="SMS code. Needed only while `register/options/` says phone_code_required.",
    )
    vin = serializers.CharField(max_length=25)
    delim = serializers.CharField(
        max_length=20,
        required=False,
        allow_blank=True,
        help_text="Polling unit code (DELIM) from the PVC, e.g. 18/23/01/001.",
    )
    polling_unit = serializers.CharField(
        max_length=20,
        required=False,
        allow_blank=True,
        help_text="Instead of `delim`: the code of a unit found by name in `register/places/`.",
    )
    full_name = serializers.CharField(max_length=150)
    gender = serializers.ChoiceField(choices=Gender.choices, required=False, allow_blank=True)
    date_of_birth = serializers.DateField(required=False, allow_null=True)
    occupation = serializers.CharField(max_length=100, required=False, allow_blank=True)
    disabilities = DisabilitiesField(
        required=False, help_text='Optional, as JSON text in this form, e.g. ["visual"].'
    )
    disability_other = serializers.CharField(max_length=100, required=False, allow_blank=True)
    email = serializers.EmailField(required=False, allow_blank=True)
    pin = serializers.CharField(max_length=6, min_length=6, write_only=True)
    consent = serializers.BooleanField()
    photo = serializers.FileField(
        required=False, allow_null=True, help_text="Optional selfie taken in the app."
    )
    pvc_image = serializers.FileField(
        required=False, allow_null=True, help_text="Optional photo of the front of the PVC."
    )


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
    has_card_photo = serializers.SerializerMethodField()
    polling_unit = PollingUnitSummarySerializer(read_only=True)
    member_since = serializers.DateTimeField(source="created_at", read_only=True)
    photo_url = serializers.SerializerMethodField()
    qr_token = serializers.SerializerMethodField()
    disabilities = DisabilitiesField(read_only=True)
    card_missing = serializers.SerializerMethodField(
        help_text="What the member must add before downloading the card; empty when ready."
    )

    class Meta:
        model = Member
        # No agent details: membership and agent accreditation are separate.
        fields = [
            "membership_number",
            "full_name",
            "phone_number",
            "whatsapp_number",
            "email",
            "vin",
            "has_pvc",
            "has_card_photo",
            "gender",
            "date_of_birth",
            "occupation",
            "status",
            "verification_status",
            "membership_state",
            "scope",
            "polling_unit",
            "member_since",
            "photo_url",
            "qr_token",
            "disabilities",
            "disability_other",
            "card_missing",
        ]
        read_only_fields = [
            "membership_number",
            "full_name",
            "phone_number",
            "gender",
            "date_of_birth",
            "disability_other",
            "status",
            "verification_status",
            "membership_state",
        ]

    def get_vin(self, member) -> str | None:
        return masked_vin(member)

    def get_has_card_photo(self, member) -> bool:
        return bool(member.pvc_image)

    def get_photo_url(self, member) -> str | None:
        return _absolute(self, reverse("my-member-photo")) if member.photo else None

    def get_qr_token(self, member) -> str:
        return card_token(member)

    def get_card_missing(self, member) -> list[str]:
        return card_missing(member)


class MemberSelfUpdateSerializer(DisabilitySerializerMixin, serializers.ModelSerializer):
    """What a member edits themselves. Gender and date of birth come from the
    PVC: they can be added, and changed until the identity is checked. A
    disability is the member's own to give, change or take back."""

    disabilities = DisabilitiesField(required=False)

    class Meta:
        model = Member
        fields = [
            "email",
            "whatsapp_number",
            "occupation",
            "gender",
            "date_of_birth",
            "disabilities",
            "disability_other",
        ]

    def validate_date_of_birth(self, value):
        if value and value > timezone.localdate():
            raise serializers.ValidationError("A date of birth cannot be in the future.")
        return value

    def validate_whatsapp_number(self, value):
        """Empty means WhatsApp is on the member's phone number."""
        if not value:
            return ""
        number = normalize_ng_phone(value)
        if number is None:
            raise serializers.ValidationError("Enter a Nigerian mobile number.")
        return number


# --- members registered by another person ----------------------------------------


class RegisterSomeoneSerializer(serializers.Serializer):
    """Anyone signed in registers a member with a name and a phone number."""

    full_name = serializers.CharField(max_length=150)
    phone_number = serializers.CharField(max_length=20)
    consent = serializers.BooleanField(
        help_text="The person agreed to join TGCG and to TGCG keeping their name and number."
    )
    scope = ScopeField(required=False, help_text="Where the person is, when known.")


class RegisteredMemberSerializer(serializers.ModelSerializer):
    """A member in the list of those the signed-in person registered."""

    scope = ScopeField(read_only=True)
    registered_at = serializers.DateTimeField(source="created_at", read_only=True)
    missing = serializers.SerializerMethodField(
        help_text="What the member's card still needs (card_missing codes)."
    )
    can_complete = serializers.SerializerMethodField(
        help_text="Whether the person who registered them may still complete it."
    )
    signed_in = serializers.SerializerMethodField(help_text="Whether the member has signed in.")

    class Meta:
        model = Member
        fields = [
            "membership_number",
            "full_name",
            "phone_number",
            "scope",
            "verification_status",
            "membership_state",
            "registered_at",
            "missing",
            "can_complete",
            "signed_in",
        ]

    def get_missing(self, member) -> list[str]:
        return card_missing(member)

    def get_can_complete(self, member) -> bool:
        from .services import can_complete

        return can_complete(self.context["request"].user, member)

    def get_signed_in(self, member) -> bool:
        from .services import has_signed_in

        return has_signed_in(member)


class RegisteredMemberCardSerializer(MemberCardSerializer):
    """A member's details as the person who registered them completes them:
    the card's data without its QR token, and whether the number can change."""

    qr_token = None
    photo_url = serializers.SerializerMethodField()
    can_change_phone = serializers.SerializerMethodField(
        help_text="Until the member first signs in, their number (the first password) can change."
    )

    class Meta(MemberCardSerializer.Meta):
        fields = [
            *(name for name in MemberCardSerializer.Meta.fields if name != "qr_token"),
            "can_change_phone",
        ]

    def get_photo_url(self, member) -> str | None:
        if not member.photo:
            return None
        path = reverse("registered-member-photo", kwargs={"number": member.membership_number})
        return _absolute(self, path)

    def get_can_change_phone(self, member) -> bool:
        from .services import has_signed_in

        return not has_signed_in(member)


class RegisteredMemberUpdateSerializer(MemberSelfUpdateSerializer):
    """What the person who registered a member completes: the member's own
    details, and corrections to the name, phone number and place they gave."""

    full_name = serializers.CharField(max_length=150, required=False)
    phone_number = serializers.CharField(max_length=20, required=False)
    scope = ScopeField(required=False)

    class Meta(MemberSelfUpdateSerializer.Meta):
        fields = [*MemberSelfUpdateSerializer.Meta.fields, "full_name", "phone_number", "scope"]


class CardVerificationSerializer(serializers.ModelSerializer):
    """Shown to a coordinator who scans a membership card's QR code."""

    valid = serializers.SerializerMethodField(
        help_text="A current member: an active membership on an accepted record."
    )
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
            "membership_state",
            "verification_status",
            "has_pvc",
            "scope",
            "member_since",
            "photo_url",
        ]

    def get_valid(self, member) -> bool:
        return member.status not in ("rejected", "archived") and member.membership_state == "active"

    def get_photo_url(self, member) -> str | None:
        if not member.photo:
            return None
        return _absolute(self, f"{reverse('member-card-photo')}?token={self.context['token']}")


class VolunteerApplicationSerializer(serializers.ModelSerializer):
    """A member's offer of party work."""

    id = serializers.UUIDField(source="uuid", read_only=True)
    member = serializers.CharField(source="member.membership_number", read_only=True)
    member_name = serializers.CharField(source="member.full_name", read_only=True)
    member_phone = serializers.SerializerMethodField(
        help_text="Withheld (null) without viewMemberSensitiveData for the member's area."
    )
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

    def get_member_phone(self, application) -> str | None:
        member = application.member
        return member.phone_number if may_see_sensitive(self, member) else None


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
        return Member.objects.filter(within_scope_q(user, permission=Capability.ACCREDIT_AGENTS))


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

    def to_representation(self, agent):
        data = super().to_representation(agent)
        if not may_see_sensitive(self, agent.member):
            data["registered_phone_number"] = None  # a phone number is personal
        return data


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


class VerificationCheckSerializer(serializers.ModelSerializer):
    checked_by = serializers.CharField(source="checked_by.access_id", default=None, read_only=True)

    class Meta:
        model = VerificationCheck
        fields = ["kind", "result", "detail", "checked_at", "checked_by"]
        read_only_fields = fields


class VerificationSerializer(serializers.Serializer):
    """A member's identity verification: the decision and every check behind it."""

    membership_number = serializers.CharField()
    verification_status = serializers.CharField()
    verified_at = serializers.DateTimeField(allow_null=True)
    reviewed_by = serializers.CharField(source="verified_by.access_id", default=None)
    verified_automatically = serializers.SerializerMethodField()
    checks = VerificationCheckSerializer(source="verification_checks", many=True)

    def get_verified_automatically(self, member) -> bool:
        return verified_automatically(member)


class VerificationNotesSerializer(serializers.Serializer):
    notes = serializers.CharField(max_length=1000, help_text="What you checked and how.")


class StandingChangeSerializer(serializers.Serializer):
    reason = serializers.CharField(required=False, allow_blank=True, max_length=1000)


class PollingUnitChangeSerializer(serializers.ModelSerializer):
    """A request to move a member's registered polling unit."""

    id = serializers.UUIDField(source="uuid", read_only=True)
    member = serializers.CharField(source="member.membership_number", read_only=True)
    member_name = serializers.CharField(source="member.full_name", read_only=True)
    from_polling_unit = serializers.CharField(
        source="from_polling_unit.code", default=None, read_only=True
    )
    to_polling_unit = serializers.CharField(source="polling_unit.code", read_only=True)
    to_polling_unit_name = serializers.CharField(source="polling_unit.name", read_only=True)
    scope = ScopeField(read_only=True)
    decided_by = serializers.CharField(source="decided_by.access_id", default=None, read_only=True)

    class Meta:
        model = PollingUnitChangeRequest
        fields = [
            "id",
            "member",
            "member_name",
            "from_polling_unit",
            "to_polling_unit",
            "to_polling_unit_name",
            "scope",
            "reason",
            "status",
            "decided_by",
            "decided_at",
            "decision_reason",
            "created_at",
        ]
        read_only_fields = fields


class PollingUnitChangeRequestSerializer(serializers.Serializer):
    """What a member sends: the code and a photo of their updated voter card."""

    delim = serializers.CharField(max_length=32)
    pvc_image = serializers.ImageField()
    reason = serializers.CharField(required=False, allow_blank=True, max_length=1000)


class PvcScanSerializer(serializers.Serializer):
    """What a member sends from their one PVC scan: the card's photo and the
    details read off it, which they checked. A detail the card did not show
    may be left blank; the member's own then stays."""

    pvc_image = serializers.ImageField()
    full_name = serializers.CharField(max_length=150)
    vin = serializers.CharField(max_length=25)
    delim = serializers.CharField(max_length=32, help_text="The polling unit code on the card.")
    gender = serializers.ChoiceField(choices=Gender.choices, required=False, allow_blank=True)
    date_of_birth = serializers.DateField(required=False, allow_null=True)
    occupation = serializers.CharField(max_length=100, required=False, allow_blank=True)

    def validate_date_of_birth(self, value):
        if value and value > timezone.localdate():
            raise serializers.ValidationError("A date of birth cannot be in the future.")
        return value


class MyPvcUpdateSerializer(serializers.ModelSerializer):
    """How the member's own PVC scan went."""

    id = serializers.UUIDField(source="uuid", read_only=True)

    class Meta:
        model = PvcUpdate
        fields = ["id", "status", "decision_reason", "created_at", "decided_at"]
        read_only_fields = fields


def _pvc_detail(record, name, sensitive):
    """A PVC detail as a reviewer may see it: the VIN's last 4 characters, and a
    date of birth only with viewMemberSensitiveData (the card photo shows it)."""
    value = getattr(record, name)
    if value in ("", None):
        return None
    if name == "vin":
        return f"{'*' * 15}{value[-4:]}"
    if name == "date_of_birth":
        return value.isoformat() if sensitive else "withheld"
    if name == "polling_unit":
        return f"{value.code} {value.name}"
    return value


PVC_CHANGE = inline_serializer(
    "PvcChange",
    {
        "field": serializers.CharField(),
        "label": serializers.CharField(),
        "from": serializers.CharField(allow_null=True, help_text="Only while it waits."),
        "to": serializers.CharField(allow_null=True),
    },
    many=True,
)


class PvcUpdateSerializer(serializers.ModelSerializer):
    """A member's PVC scan for a coordinator's second check: what it changes,
    against the member's details while it waits."""

    id = serializers.UUIDField(source="uuid", read_only=True)
    member = serializers.CharField(source="member.membership_number", read_only=True)
    member_name = serializers.CharField(source="member.full_name", read_only=True)
    changes = serializers.SerializerMethodField()
    decided_by = serializers.CharField(source="decided_by.access_id", default=None, read_only=True)

    class Meta:
        model = PvcUpdate
        fields = [
            "id",
            "member",
            "member_name",
            "status",
            "changes",
            "decided_by",
            "decided_at",
            "decision_reason",
            "created_at",
        ]
        read_only_fields = fields

    @extend_schema_field(PVC_CHANGE)
    def get_changes(self, update) -> list:
        from .services import PV, PVC_FIELDS, pvc_changes

        member = update.member
        waiting = update.status == PV.PENDING
        sensitive = may_see_sensitive(self, member)
        names = pvc_changes(member, update) if waiting else update.changed_fields
        return [
            {
                "field": name,
                "label": PVC_FIELDS[name],
                "from": _pvc_detail(member, name, sensitive) if waiting else None,
                "to": _pvc_detail(update, name, sensitive),
            }
            for name in names
            if name in PVC_FIELDS
        ]

"""Short USSD membership and deferred app-account contracts."""

import uuid

import pytest
from django.contrib.auth.hashers import make_password
from django.core.exceptions import ValidationError
from django.db import IntegrityError
from rest_framework.test import APIClient

from apps.access.models import AuditEvent
from apps.accounts.models import User
from apps.core.choices import MembershipState, RegistrationChannel, Role, VerificationStatus
from apps.core.models import Sequence
from apps.geography.models import PollingUnit
from apps.geography.polling_units import load_polling_units
from apps.membership import registration
from apps.membership.models import Member, UssdRegistrationDraft, UssdSession
from apps.membership.serializers import (
    AssistedRegistrationSerializer,
    MemberSerializer,
    SelfRegistrationSerializer,
)

pytestmark = pytest.mark.django_db

PHONE = "+2348031234567"
OTHER_PHONE = "+2348031234568"
VIN = "90F5B0E8D0295847263"
OTHER_VIN = "90F5B0E8D0295847264"
PIN = "482915"


@pytest.fixture
def unit():
    load_polling_units(lga_codes=["KD-ZARIA"])
    return PollingUnit.objects.get(code="18-23-01-001")


def join(unit, **overrides):
    return registration.ussd_register_minimal(
        **{
            "phone_number": PHONE,
            "draft_id": uuid.uuid4(),
            "polling_unit": unit,
            "vin": VIN,
            **overrides,
        }
    )


def complete(member, **overrides):
    return registration.ussd_complete_profile(
        **{
            "member": member,
            "full_name": "Musa Bello",
            "pin_hash": make_password(PIN),
            **overrides,
        }
    )


def test_minimal_membership_needs_no_name_pin_or_account(unit):
    draft_id = uuid.uuid4()
    member = join(unit, phone_number="08031234567", draft_id=draft_id, vin=VIN.lower())
    assert member.phone_number == PHONE
    assert member.client_id == f"ussd:{draft_id}"
    assert member.full_name == member.gender == ""
    assert member.date_of_birth is None and member.user_id is None
    assert member.phone_verified_at and member.consent_at
    assert member.registration_channel == RegistrationChannel.USSD
    assert member.membership_state == MembershipState.ACTIVE
    assert member.verification_status == VerificationStatus.PENDING_REVIEW
    assert member.polling_unit == unit and member.lga.code == "KD-ZARIA"
    assert not member.can_hold_roles
    assert not User.objects.exists()
    checks = dict(member.verification_checks.values_list("kind", "result"))
    assert checks["phoneVerified"] == "passed"
    assert checks["pvcCaptured"] == "pending"
    assert checks["selfieCaptured"] == "failed"


def test_join_retries_and_existing_phone_keep_original_identity(unit):
    member = join(unit)
    again = join(unit, phone_number="08031234567", vin=OTHER_VIN)
    assert again.pk == member.pk
    assert again.client_id == member.client_id and again.vin == VIN
    assert Member.objects.count() == 1 and not User.objects.exists()
    assert AuditEvent.objects.filter(action="member.checksRecorded").count() == 1


def test_vin_already_owned_by_another_phone_is_rejected(unit):
    join(unit)
    with pytest.raises(ValidationError, match="already registered"):
        join(unit, phone_number=OTHER_PHONE)
    assert Member.objects.count() == 1 and not User.objects.exists()


def test_staff_phone_cannot_join(unit):
    User.objects.create_user(
        "STAFF", full_name="Staff Operator", phone_number=PHONE,
        role=Role.NATIONAL_ADMINISTRATOR,
    )
    with pytest.raises(ValidationError, match="staff account"):
        join(unit)
    assert not Member.objects.exists()


@pytest.mark.parametrize("vin", ["", "123", "!" * 19])
def test_minimal_membership_requires_valid_vin(unit, vin):
    with pytest.raises(ValidationError):
        join(unit, vin=vin)
    assert not Member.objects.exists() and not User.objects.exists()


def test_minimal_membership_requires_registered_unit_and_draft_uuid(unit):
    with pytest.raises(ValidationError, match="INEC register"):
        join(unit, polling_unit=None)
    with pytest.raises(ValidationError, match="Dial again"):
        join(unit, draft_id="not-a-uuid")
    assert not Member.objects.exists()


def test_uniqueness_failure_rolls_back_and_leaves_transaction_usable(unit, monkeypatch):
    def race(instance, *args, **kwargs):
        raise IntegrityError("simulated uniqueness race")

    monkeypatch.setattr(Member, "save", race)
    with pytest.raises(ValidationError, match="registered a moment ago"):
        join(unit)
    assert not Member.objects.exists() and not AuditEvent.objects.exists()
    assert not Sequence.objects.filter(name="member").exists()


@pytest.mark.parametrize("channel", [RegistrationChannel.SELF, RegistrationChannel.ASSISTED])
def test_only_ussd_members_may_have_blank_names(unit, channel):
    member = join(unit)
    member.registration_channel = channel
    with pytest.raises(ValidationError, match="full name"):
        member.full_clean()


def test_registration_and_staff_write_serializers_still_require_names():
    for serializer_type in (
        MemberSerializer, AssistedRegistrationSerializer, SelfRegistrationSerializer,
    ):
        serializer = serializer_type()
        assert serializer.fields["full_name"].required
        assert not serializer.fields["full_name"].allow_blank
        serializer = serializer_type(data={"full_name": ""}, partial=True)
        assert not serializer.is_valid()
        assert "full_name" in serializer.errors


def test_profile_completion_adds_real_name_and_member_only_login(unit):
    member = join(unit)
    membership_number = member.membership_number
    encoded = make_password(PIN)
    member = complete(member, full_name="  Musa   Bello  ", pin_hash=encoded)
    assert member.membership_number == membership_number and member.vin == VIN
    assert member.full_name == member.user.full_name == "Musa Bello"
    assert member.user.password == encoded and member.user.check_password(PIN)
    assert member.user.role == Role.MEMBER
    assert not member.user.is_staff and not member.user.is_superuser
    assert member.user.polling_unit_id == member.polling_unit_id
    assert member.verification_status == VerificationStatus.PENDING_REVIEW
    assert Member.objects.count() == User.objects.count() == 1
    event = AuditEvent.objects.get(action="member.ussdProfileCompleted")
    assert event.actor_id == member.user_id
    assert PIN not in str(event.changes) and encoded not in str(event.changes)
    response = APIClient().post(
        "/api/v1/auth/login/", {"username": PHONE, "password": PIN}, format="json"
    )
    assert response.status_code == 200
    assert response.json()["user"]["role"] == Role.MEMBER


def test_profile_retry_never_resets_existing_name_or_password(unit):
    member = complete(join(unit))
    encoded = member.user.password
    again = complete(member, full_name="Another Person", pin_hash=make_password("739284"))
    assert again.pk == member.pk and again.full_name == "Musa Bello"
    assert again.user.password == encoded
    assert User.objects.count() == 1
    assert AuditEvent.objects.filter(action="member.ussdProfileCompleted").count() == 1


@pytest.mark.parametrize("encoded", [PIN, "!unusable", "md5$", "md5$salt$", ""])
def test_profile_completion_rejects_raw_unusable_or_malformed_pin_hash(unit, encoded):
    member = join(unit)
    with pytest.raises(ValidationError, match="confirm a new app PIN"):
        complete(member, pin_hash=encoded)
    member.refresh_from_db()
    assert member.full_name == "" and member.user_id is None and not User.objects.exists()


@pytest.mark.parametrize("name", ["", "Musa", "Musa 12345", "A B"])
def test_profile_completion_requires_full_name(unit, name):
    member = join(unit)
    with pytest.raises(ValidationError, match="at least two names"):
        complete(member, full_name=name)
    assert not User.objects.exists()


def test_profile_completion_cannot_replace_recorded_identity(unit):
    member = join(unit)
    Member.objects.filter(pk=member.pk).update(full_name="Recorded Person")
    with pytest.raises(ValidationError, match="already recorded"):
        complete(member)
    member.refresh_from_db()
    assert member.full_name == "Recorded Person" and member.user_id is None


def test_profile_completion_reuses_recorded_name_without_changing_spelling(unit):
    member = join(unit)
    Member.objects.filter(pk=member.pk).update(full_name="MUSA BELLO")
    member = complete(member)
    assert member.full_name == member.user.full_name == "MUSA BELLO"


def test_profile_completion_rejects_other_channel_or_ended_membership(unit):
    member = join(unit)
    Member.objects.filter(pk=member.pk).update(registration_channel=RegistrationChannel.ASSISTED)
    with pytest.raises(ValidationError, match="set up in the app"):
        complete(member)
    Member.objects.filter(pk=member.pk).update(
        registration_channel=RegistrationChannel.USSD, membership_state=MembershipState.DEACTIVATED,
    )
    with pytest.raises(ValidationError, match="membership has ended"):
        complete(member)
    assert not User.objects.exists()


def test_profile_completion_rejects_existing_unlinked_account(unit):
    member = join(unit)
    User.objects.create_user(
        "EXISTING", full_name="Existing Operator", phone_number=PHONE,
        role=Role.NATIONAL_ADMINISTRATOR,
    )
    with pytest.raises(ValidationError, match="already has an account"):
        complete(member)
    member.refresh_from_db()
    assert member.full_name == "" and member.user_id is None and User.objects.count() == 1


def test_app_pin_setup_does_not_burn_sms_code_for_incomplete_ussd_profile(unit, monkeypatch):
    member = join(unit)
    consumed = []
    monkeypatch.setattr(registration, "consume_code", lambda *args: consumed.append(args))
    with pytest.raises(ValidationError, match="add your name and app PIN first"):
        registration.set_pin(phone_number=PHONE, code="123456", pin=PIN)
    assert not consumed and not User.objects.exists()
    member.refresh_from_db()
    assert member.full_name == "" and member.user_id is None


def test_progress_has_uuid_keys_and_can_be_deleted_without_deleting_members(unit):
    member = join(unit)
    draft = UssdRegistrationDraft.objects.create(phone_number=PHONE, vin=VIN, polling_unit=unit)
    session = UssdSession.objects.create(
        phone_number=PHONE, service_code="*384*1234#", provider_session_id="provider-session",
        draft=draft, answer_count=2, prefix_digest="a" * 64, last_response="CON Enter unit:",
    )
    assert isinstance(draft.pk, uuid.UUID) and isinstance(session.pk, uuid.UUID)
    assert draft.created_at and draft.updated_at and session.created_at and session.updated_at
    assert not any(field.name in {"text", "pin"} for field in draft._meta.fields)
    assert not any(field.name in {"text", "pin"} for field in session._meta.fields)
    draft.delete()
    session.refresh_from_db()
    assert session.draft_id is None
    assert Member.objects.filter(pk=member.pk).exists()


def test_session_uniqueness_includes_phone_and_service_code():
    UssdSession.objects.create(
        phone_number=PHONE, service_code="*384*1234#", provider_session_id="same-session",
    )
    UssdSession.objects.create(
        phone_number=OTHER_PHONE, service_code="*384*1234#", provider_session_id="same-session",
    )
    UssdSession.objects.create(
        phone_number=PHONE, service_code="*384*4321#", provider_session_id="same-session",
    )
    duplicate = UssdSession(
        phone_number=PHONE, service_code="*384*1234#", provider_session_id="same-session",
    )
    with pytest.raises(ValidationError, match="already exists"):
        duplicate.full_clean()

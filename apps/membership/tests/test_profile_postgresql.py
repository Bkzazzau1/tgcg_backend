"""Member completion must work with PostgreSQL's nullable account relation."""

import io
from datetime import date

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from PIL import Image

from apps.accounts.models import User
from apps.core.choices import GeographyLevel, RegistrationChannel, Role, VerificationStatus
from apps.geography.models import PollingUnit, State
from apps.geography.polling_units import load_polling_units
from apps.membership import services
from apps.membership.models import PvcUpdate
from conftest import PASSWORD, make_member

pytestmark = pytest.mark.django_db(transaction=True, serialized_rollback=True)

VIN = "90F5B0E8D0295847263"


@pytest.fixture(autouse=True)
def postgres(settings, tmp_path):
    if connection.vendor != "postgresql":
        pytest.skip("Real PostgreSQL nullable row locks are required.")
    settings.MEDIA_ROOT = tmp_path
    load_polling_units(lga_codes=["KD-ZARIA"])


@pytest.fixture(params=[False, True], ids=["without-account", "with-account"])
def member(request):
    unit = PollingUnit.objects.get(code="18-23-01-001")
    account = None
    if request.param:
        account = User.objects.create_user(
            "PROFILE-PG",
            PASSWORD,
            full_name="Typed Member",
            role=Role.MEMBER,
            phone_number="08031112222",
            scope_level=GeographyLevel.POLLING_UNIT,
            polling_unit=unit,
        )
    return make_member(
        full_name="Typed Member",
        registration_channel=RegistrationChannel.USSD,
        scope_level=GeographyLevel.POLLING_UNIT,
        polling_unit=unit,
        user=account,
    )


def picture(name):
    buffer = io.BytesIO()
    Image.new("RGB", (40, 40), (10, 36, 99)).save(buffer, "PNG")
    return SimpleUploadedFile(name, buffer.getvalue(), content_type="image/png")


def scan(member):
    return services.scan_own_pvc(
        member=member,
        pvc_image=picture("card.png"),
        full_name="Card Member",
        vin=VIN,
        delim="18/23/01/014",
        gender="male",
        date_of_birth=date(1990, 5, 14),
    )


def test_profile_and_selfie_completion_preserve_membership_and_account(member):
    account_id = member.user_id
    services.update_own_details(member=member, gender="female", date_of_birth=date(1992, 3, 8))
    services.set_own_selfie(member=member, upload=picture("selfie.png"))
    member.refresh_from_db()
    assert member.gender == "female" and member.date_of_birth == date(1992, 3, 8)
    assert member.photo and member.user_id == account_id
    assert member.verification_status == VerificationStatus.PENDING_REVIEW
    assert member.membership_state == "active"


def test_unverified_card_scan_applies_details_once(member):
    update = scan(member)
    assert update.status == PvcUpdate.Status.APPLIED
    member.refresh_from_db()
    assert member.full_name == "Card Member" and member.vin == VIN
    assert member.polling_unit.code == "18-23-01-014" and member.pvc_image
    assert member.verification_status == VerificationStatus.PENDING_REVIEW
    assert services.can_scan_pvc(member) is False
    if member.user_id:
        assert member.user.full_name == "Card Member"


def test_verified_card_scan_waits_for_authorized_second_review(member):
    member.verification_status = VerificationStatus.VERIFIED
    member.save(update_fields=["verification_status"])
    update = scan(member)
    member.refresh_from_db()
    assert update.status == PvcUpdate.Status.PENDING
    assert member.full_name == "Typed Member" and not member.pvc_image
    reviewer = User.objects.create_user(
        "REVIEW-PG",
        PASSWORD,
        full_name="Kaduna Reviewer",
        role=Role.STATE_COORDINATOR,
        scope_level=GeographyLevel.STATE,
        state=State.objects.get(code="KD"),
    )
    approved = services.approve_pvc_update(actor=reviewer, update=update, reason="Card matches.")
    member.refresh_from_db()
    assert approved.status == PvcUpdate.Status.APPROVED
    assert member.full_name == "Card Member" and member.pvc_image
    assert member.verification_status == VerificationStatus.VERIFIED
    assert member.verified_by_id == reviewer.pk
    assert member.polling_unit.code == "18-23-01-014"

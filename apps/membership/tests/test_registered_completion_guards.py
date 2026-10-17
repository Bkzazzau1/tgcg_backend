"""A registrar's stale view check cannot authorize a later frozen-member write."""

import io

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db.models.query import QuerySet
from django.utils import timezone
from PIL import Image

from apps.access.models import AuditEvent
from apps.access.tests.test_access import member_account
from apps.accounts.models import User
from apps.core.choices import GeographyLevel, MembershipState, VerificationStatus
from apps.geography.models import PollingUnit
from apps.geography.polling_units import load_polling_units
from apps.membership import registration, services
from apps.membership.models import Member, PvcUpdate

pytestmark = pytest.mark.django_db

VIN = "90F5B0E8D0295847263"


@pytest.fixture
def setup(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    load_polling_units(lga_codes=["KD-ZARIA"])
    creator = member_account("08030001001", name="Synthetic Registrant")
    member = registration.register_someone(
        actor=creator.user,
        full_name="Synthetic Member",
        phone_number="08030001002",
        consent=True,
    )
    return creator.user, member


def picture():
    data = io.BytesIO()
    Image.new("RGB", (40, 40), (10, 36, 99)).save(data, "PNG")
    return SimpleUploadedFile("synthetic.png", data.getvalue(), content_type="image/png")


def pending_scan(member):
    return PvcUpdate.objects.create(
        member=member,
        full_name=member.full_name,
        vin=VIN,
        pvc_image="synthetic/unopened.png",
        status=PvcUpdate.Status.PENDING,
        scope_level=GeographyLevel.POLLING_UNIT,
        polling_unit=PollingUnit.objects.get(code="18-23-01-014"),
    )


@pytest.mark.parametrize("operation", ["details", "selfie", "scan", "withdraw", "correction"])
@pytest.mark.parametrize("change", ["verified", "rejected", "suspended", "different_creator"])
def test_stale_registrar_check_cannot_change_a_member_after_permission_changes(
    setup, operation, change
):
    actor, member = setup
    scan = pending_scan(member) if operation == "withdraw" else None
    stale = Member.objects.select_related("user").get(pk=member.pk)
    services.require_completing(actor, stale)
    if change in ("verified", "rejected"):
        Member.objects.filter(pk=member.pk).update(verification_status=change)
    elif change == "suspended":
        Member.objects.filter(pk=member.pk).update(membership_state=MembershipState.SUSPENDED)
    else:
        other = member_account("08030001003", name="Other Registrant")
        Member.objects.filter(pk=member.pk).update(created_by=other.user)
    audits_before = AuditEvent.objects.count()
    with pytest.raises(PermissionDenied):
        if operation == "details":
            services.update_own_details(member=stale, actor=actor, occupation="Changed")
        elif operation == "selfie":
            services.set_own_selfie(member=stale, actor=actor, upload=picture())
        elif operation == "scan":
            services.scan_own_pvc(
                member=stale,
                actor=actor,
                pvc_image=picture(),
                full_name="Card Member",
                vin=VIN,
                delim="18/23/01/014",
            )
        elif operation == "withdraw":
            services.withdraw_pvc_update(member=stale, actor=actor, update=scan)
        else:
            services.correct_registration(member=stale, actor=actor, full_name="Changed")
    member.refresh_from_db()
    assert member.full_name == "Synthetic Member" and member.occupation == ""
    assert not member.photo and not member.pvc_image and member.vin is None
    assert AuditEvent.objects.count() == audits_before
    if scan is not None:
        scan.refresh_from_db()
        assert scan.status == PvcUpdate.Status.PENDING
    else:
        assert not member.pvc_updates.exists()


def test_member_can_still_edit_their_own_occupation_after_identity_is_checked(setup):
    _, member = setup
    Member.objects.filter(pk=member.pk).update(verification_status=VerificationStatus.VERIFIED)
    services.update_own_details(member=member, occupation="Changed by member")
    member.refresh_from_db()
    assert member.occupation == "Changed by member"
    assert member.verification_status == VerificationStatus.VERIFIED


def test_member_can_still_withdraw_their_own_pending_scan_after_identity_is_checked(setup):
    _, member = setup
    scan = pending_scan(member)
    Member.objects.filter(pk=member.pk).update(verification_status=VerificationStatus.VERIFIED)
    services.withdraw_pvc_update(member=member, update=scan)
    scan.refresh_from_db()
    assert scan.status == PvcUpdate.Status.WITHDRAWN


def test_phone_correction_reloads_account_instead_of_trusting_joined_last_login(setup, monkeypatch):
    actor, member = setup
    stale = Member.objects.select_related("user").get(pk=member.pk)
    assert stale.user.last_login is None
    signed_in_at = timezone.now()
    User.objects.filter(pk=member.user_id).update(last_login=signed_in_at)
    original_get = QuerySet.get

    def stale_member_get(queryset, *args, **kwargs):
        # Model the joined account snapshot taken before a completed first login.
        if queryset.model is Member and queryset.query.select_for_update:
            return stale
        return original_get(queryset, *args, **kwargs)

    monkeypatch.setattr(QuerySet, "get", stale_member_get)
    with pytest.raises(ValidationError, match="signed in"):
        services.correct_registration(actor=actor, member=member, phone_number="08030001004")
    member.refresh_from_db()
    account = User.objects.get(pk=member.user_id)
    assert member.phone_number == account.phone_number
    assert account.last_login == signed_in_at
    assert account.check_password(registration.first_password(member.phone_number))
    assert not account.check_password(registration.first_password("08030001004"))

"""Whoever decides an identity sees what they decide on, and nobody else does."""

import io

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from PIL import Image

from apps.access.tests.test_access import KD, appoint, client_for, custom_role, member_account
from apps.accounts.models import User
from apps.core.choices import Role, VerificationStatus
from apps.core.permissions import Capability as C
from conftest import PASSWORD, make_member

pytestmark = pytest.mark.django_db
KN = {"level": "state", "stateId": "KN"}


@pytest.fixture(autouse=True)
def media(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path


@pytest.fixture
def admin():
    return User.objects.create_user(
        "EVIDENCE-NA", PASSWORD, full_name="National Admin", role=Role.NATIONAL_ADMINISTRATOR
    )


def picture(name):
    buffer = io.BytesIO()
    Image.new("RGB", (4, 4), "white").save(buffer, "JPEG")
    return SimpleUploadedFile(name, buffer.getvalue(), content_type="image/jpeg")


def staff(admin, phone, name, powers, area=KD, verified=True):
    person = member_account(phone, name=name, verified=verified)
    role = custom_role(admin, name, powers)
    assert appoint(admin, person, role, [area]).status_code == 201
    return client_for(person.user)


@pytest.fixture
def pending():
    """A member in Kaduna awaiting review, with a selfie and a voter card photo."""
    member = make_member(
        phone="08034440001",
        full_name="Pending Person",
        verification_status=VerificationStatus.PENDING_REVIEW,
    )
    member.photo = picture("selfie.jpg")
    member.pvc_image = picture("card.jpg")
    member.save()
    return member


def urls(member):
    base = f"/api/v1/members/{member.membership_number}"
    return f"{base}/photo/", f"{base}/card-photo/"


def test_a_reviewer_without_personal_data_access_sees_both_photos(admin, pending):
    # Polling unit executives check identities but hold no personal data access.
    reviewer = staff(admin, "08034440002", "Unit Checker", [C.VERIFY_MEMBER_IDENTITY])
    selfie, card = urls(pending)
    assert reviewer.get(selfie).status_code == 200
    shown = reviewer.get(card)
    assert shown.status_code == 200
    assert shown["Cache-Control"] == "private, no-store"
    # The rest of their details stay hidden from them.
    row = reviewer.get(f"/api/v1/members/{pending.membership_number}/").json()
    assert row["sensitive_visible"] is False and row["phone_number"] is None


def test_a_reviewer_elsewhere_sees_neither(admin, pending):
    outsider = staff(admin, "08034440003", "Kano Checker", [C.VERIFY_MEMBER_IDENTITY], area=KN)
    for url in urls(pending):
        assert outsider.get(url).status_code in (403, 404)


def test_personal_data_access_shows_the_selfie_but_not_the_card(admin, pending):
    desk = staff(
        admin, "08034440004", "Member Desk", [C.REGISTER_MEMBERS, C.VIEW_MEMBER_SENSITIVE_DATA]
    )
    selfie, card = urls(pending)
    assert desk.get(selfie).status_code == 200
    assert desk.get(card).status_code == 403


def test_someone_who_only_registers_members_sees_neither(admin, pending):
    reader = staff(admin, "08034440005", "Member Reader", [C.REGISTER_MEMBERS])
    for url in urls(pending):
        assert reader.get(url).status_code == 403


def test_no_one_reviews_themselves(admin):
    person = member_account("08034440006", name="Own Reviewer")
    person.pvc_image = picture("card.jpg")
    person.save()
    role = custom_role(admin, "Own Reviewer", [C.VERIFY_MEMBER_IDENTITY])
    assert appoint(admin, person, role, [KD]).status_code == 201
    assert client_for(person.user).get(urls(person)[1]).status_code == 403


def test_a_member_with_no_card_photo_has_nothing_to_show(admin):
    member = make_member(phone="08034440007", full_name="No Card")
    reviewer = staff(admin, "08034440008", "Unit Checker Two", [C.VERIFY_MEMBER_IDENTITY])
    assert reviewer.get(urls(member)[1]).status_code == 404

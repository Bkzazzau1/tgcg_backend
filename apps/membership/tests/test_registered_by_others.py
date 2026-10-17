"""Members registered by another person with a name and a phone number
(17 October 2026): anyone signed in registers them, as many as they wish,
and completes the registration until the identity is checked."""

import io

import pytest
from PIL import Image
from rest_framework.test import APIClient

from apps.access.models import AuditEvent
from apps.access.tests.test_access import client_for, member_account
from apps.core.choices import GeographyLevel, MembershipState, Role
from apps.geography.models import Ward
from apps.geography.polling_units import load_polling_units
from apps.membership.models import Member

pytestmark = pytest.mark.django_db

URL = "/api/v1/me/registered/"
VIN = "90F5B0E8D0295847263"


@pytest.fixture(autouse=True)
def setup(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    load_polling_units(lga_codes=["KD-ZARIA"])


@pytest.fixture
def registrant():
    """An ordinary member: registering someone needs no role."""
    return member_account("08030000001", name="Ada Obi")


def image(fmt="JPEG", name="photo.jpg"):
    buffer = io.BytesIO()
    Image.new("RGB", (40, 40), (10, 36, 99)).save(buffer, fmt)
    buffer.seek(0)
    buffer.name = name
    return buffer


def register(client, phone="0805 123 4567", name="Sani Umar", **extra):
    body = {"full_name": name, "phone_number": phone, "consent": True, **extra}
    return client.post(URL, body, format="json")


def sign_in(identifier, password):
    body = {"username": identifier, "password": password}
    return APIClient().post("/api/v1/auth/login/", body, format="json")


def test_anyone_signed_in_registers_a_member_with_a_name_and_a_phone_number(registrant):
    response = register(client_for(registrant.user))
    assert response.status_code == 201, response.content
    body = response.json()
    assert body["full_name"] == "Sani Umar" and body["phone_number"] == "+2348051234567"
    assert body["scope"]["level"] == GeographyLevel.COUNTRY  # no place given
    assert body["can_complete"] is True and body["signed_in"] is False
    assert {"photo", "pvc_photo", "gender", "date_of_birth"} <= set(body["missing"])

    member = Member.objects.get(phone_number="+2348051234567")
    assert member.registration_channel == "referred" and member.created_by == registrant.user
    assert member.consent_at is not None and member.phone_verified_at is None
    # Nothing to check yet: not verified, and not waiting in the review queue.
    assert member.verification_status == "unverified"
    assert member.user.role == Role.MEMBER
    assert member.user.access_id == member.membership_number
    event = AuditEvent.objects.get(action="member.registeredByOther")
    assert event.actor == registrant.user and event.target_id == member.membership_number

    # The number, as people write it, is the first password.
    assert sign_in("08051234567", "08051234567").status_code == 200
    member.user.refresh_from_db()
    assert member.user.last_login is not None


def test_staff_register_members_too_and_may_give_the_place(lga_coordinator):
    ward = Ward.objects.filter(lga__code="KD-ZARIA").order_by("code").first()
    response = register(client_for(lga_coordinator), scope={"level": "ward", "wardId": ward.code})
    assert response.status_code == 201, response.content
    member = Member.objects.get(phone_number="+2348051234567")
    assert member.scope_level == GeographyLevel.WARD and member.ward == ward
    assert member.lga.code == "KD-ZARIA" and member.state.code == "KD"


def test_a_name_a_new_number_and_the_persons_agreement_are_needed(registrant):
    client = client_for(registrant.user)
    assert "consent" in register(client, consent=False).json()
    assert "full_name" in register(client, name="   ").json()
    assert "phone_number" in register(client, phone="12345").json()
    assert register(client).status_code == 201
    again = register(client, name="Someone Else")
    assert again.status_code == 400 and "phone_number" in again.json()
    assert APIClient().post(URL, {}, format="json").status_code == 401
    assert Member.objects.filter(registration_channel="referred").count() == 1


def test_as_many_as_they_wish_and_each_sees_only_their_own(registrant):
    client = client_for(registrant.user)
    for n in range(3):
        assert register(client, phone=f"0805123456{n}", name=f"Member {n}").status_code == 201
    other = client_for(member_account("08030000002", name="Bala Musa").user)
    assert register(other, phone="08059999999", name="Someone Else").status_code == 201

    mine = client.get(URL).json()
    assert mine["count"] == 3
    names = [member["full_name"] for member in mine["results"]]
    assert names == ["Member 2", "Member 1", "Member 0"]
    assert client.get(URL, {"search": "member 1"}).json()["count"] == 1
    by_phone = client.get(URL, {"search": "0805 123 4560"}).json()["results"]
    assert [member["full_name"] for member in by_phone] == ["Member 0"]
    theirs = f"{URL}{Member.objects.get(full_name='Someone Else').membership_number}/"
    assert client.get(theirs).status_code == 404
    assert client.patch(theirs, {"occupation": "Trader"}, format="json").status_code == 404


def test_a_suspended_member_registers_nobody(registrant):
    registrant.membership_state = MembershipState.SUSPENDED
    registrant.save()
    assert register(client_for(registrant.user)).status_code == 403
    assert not Member.objects.filter(registration_channel="referred").exists()


def test_the_person_who_registered_them_completes_the_registration(registrant):
    client = client_for(registrant.user)
    number = register(client).json()["membership_number"]
    url = f"{URL}{number}/"
    details = client.get(url).json()
    assert "qr_token" not in details and details["can_change_phone"] is True

    changed = client.patch(
        url,
        {
            "full_name": "Sani Umar Bello",
            "gender": "male",
            "date_of_birth": "1990-05-14",
            "occupation": "Trader",
        },
        format="json",
    )
    assert changed.status_code == 200, changed.content
    assert changed.json()["full_name"] == "Sani Umar Bello" and changed.json()["gender"] == "male"
    photo = client.post(f"{url}photo/", {"photo": image()}, format="multipart")
    assert photo.status_code == 200, photo.content
    assert photo.json()["photo_url"].endswith(f"/me/registered/{number}/photo/")
    assert client.get(f"{url}photo/").status_code == 200
    assert client.get(f"{url}pvc-scan/").json() == {"can_scan": True, "latest": None}
    scan = {
        "pvc_image": image("PNG", "pvc.png"),
        "full_name": "Sani Umar Bello",
        "vin": VIN,
        "delim": "18/23/01/014",
        "gender": "male",
        "date_of_birth": "1990-05-14",
    }
    scanned = client.post(f"{url}pvc-scan/", scan, format="multipart")
    assert scanned.status_code == 201, scanned.content
    assert scanned.json()["status"] == "applied"

    member = Member.objects.get(membership_number=number)
    assert member.vin == VIN and member.scope_level == GeographyLevel.POLLING_UNIT
    assert member.user.full_name == "Sani Umar Bello"
    # Now there is something to review.
    assert member.verification_status == "pendingReview"
    # Each change is recorded against the person who made it.
    actions = set(
        AuditEvent.objects.filter(target_id=number, actor=registrant.user).values_list(
            "action", flat=True
        )
    )
    assert {
        "member.registeredByOther",
        "member.registrationCorrected",
        "member.detailsChanged",
        "member.photoAdded",
        "member.pvcUpdateApplied",
    } <= actions
    # The name and the place now come from the card.
    renamed = client.patch(url, {"full_name": "Another Name"}, format="json")
    assert renamed.status_code == 400 and "full_name" in renamed.json()
    ward = Ward.objects.filter(lga__code="KD-ZARIA").order_by("code").first()
    moved = client.patch(url, {"scope": {"level": "ward", "wardId": ward.code}}, format="json")
    assert moved.status_code == 400 and "scope" in moved.json()


def test_a_place_can_be_added_later(registrant):
    client = client_for(registrant.user)
    number = register(client).json()["membership_number"]
    ward = Ward.objects.filter(lga__code="KD-ZARIA").order_by("code").first()
    placed = client.patch(
        f"{URL}{number}/", {"scope": {"level": "ward", "wardId": ward.code}}, format="json"
    )
    assert placed.status_code == 200, placed.content
    assert placed.json()["scope"]["wardId"] == ward.code
    event = AuditEvent.objects.get(action="member.registrationCorrected")
    assert event.changes == {"fields": ["place"]}


def test_the_number_changes_only_until_the_member_signs_in(registrant):
    client = client_for(registrant.user)
    number = register(client, phone="08051234567").json()["membership_number"]
    url = f"{URL}{number}/"
    fixed = client.patch(url, {"phone_number": "0805 765 4321"}, format="json")
    assert fixed.status_code == 200, fixed.content
    assert fixed.json()["phone_number"] == "+2348057654321"
    # The first password follows the number. (A refused sign-in would mark
    # this test's transaction for rollback, so the old one is checked here.)
    user = Member.objects.get(membership_number=number).user
    assert user.check_password("08057654321") and not user.check_password("08051234567")
    assert sign_in("08057654321", "08057654321").status_code == 200

    late = client.patch(url, {"phone_number": "08051112222"}, format="json")
    assert late.status_code == 400 and "phone_number" in late.json()
    assert client.get(url).json()["can_change_phone"] is False


def test_once_the_identity_is_checked_the_details_are_theirs(registrant):
    client = client_for(registrant.user)
    number = register(client).json()["membership_number"]
    Member.objects.filter(membership_number=number).update(verification_status="verified")
    url = f"{URL}{number}/"
    assert client.get(url).status_code == 403
    assert client.patch(url, {"occupation": "Trader"}, format="json").status_code == 403
    assert client.post(f"{url}photo/", {"photo": image()}, format="multipart").status_code == 403
    assert client.get(f"{url}pvc-scan/").status_code == 403
    assert client.get(URL).json()["results"][0]["can_complete"] is False

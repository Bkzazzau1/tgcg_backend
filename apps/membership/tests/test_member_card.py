"""The membership card download, and the disabilities a member may record."""

import io
import json

import pytest
from django.test import RequestFactory
from PIL import Image
from rest_framework.test import APIClient

from apps.access.models import AuthoritySource, RoleAssignment, RoleDefinition
from apps.accounts.models import User
from apps.core.choices import GeographyLevel, MembershipState, Role, VerificationStatus
from apps.geography.models import State
from apps.geography.polling_units import load_polling_units
from apps.membership import card_pdf
from apps.membership.card import card_roles
from apps.membership.models import Member
from apps.membership.serializers import CardVerificationSerializer, MemberSerializer, card_token
from conftest import PASSWORD

# Joining runs some steps outside the request transaction, as in test_registration.
pytestmark = pytest.mark.django_db(transaction=True, serialized_rollback=True)

# Names and numbers here are made up.
VIN = "90F5B0E8D0295847263"
PHONE = "08031234567"


@pytest.fixture(autouse=True)
def setup(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    settings.REGISTRATION_PHONE_CODE_REQUIRED = False
    load_polling_units(lga_codes=["KD-ZARIA"])


def image(fmt="JPEG", name="photo.jpg", size=(300, 400)):
    buffer = io.BytesIO()
    Image.new("RGB", size, (10, 36, 99)).save(buffer, fmt)
    buffer.seek(0)
    buffer.name = name
    return buffer


def join(**fields):
    """Join in the app; returns a client signed in as the new member."""
    data = {
        "phone_number": PHONE,
        "vin": VIN,
        "delim": "18/23/01/001",
        "full_name": "Musa Bello",
        "pin": "482915",
        "consent": True,
        **fields,
    }
    response = APIClient().post("/api/v1/register/", data, format="multipart")
    assert response.status_code == 201, response.content
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {response.json()['access']}")
    return client


def complete():
    return join(
        gender="male",
        date_of_birth="1990-05-14",
        photo=image(),
        pvc_image=image("PNG", "pvc.png"),
    )


# --- the card -------------------------------------------------------------------


def test_a_member_with_photo_pvc_gender_and_birth_date_downloads_the_card():
    client = complete()
    member = Member.objects.get()
    # No identity check needed.
    assert member.verification_status != VerificationStatus.VERIFIED
    assert client.get("/api/v1/me/member/").json()["card_missing"] == []

    response = client.get("/api/v1/me/member/card/")

    assert response.status_code == 200, response.content
    assert response["Content-Type"] == "application/pdf"
    assert response["Content-Disposition"] == (
        f'attachment; filename="{member.membership_number}-card.pdf"'
    )
    assert response.content.startswith(b"%PDF")
    # Two pages: the front and the back.
    assert b"/Count 2" in response.content


def test_the_card_waits_for_the_photos_gender_and_birth_date():
    client = join()

    response = client.get("/api/v1/me/member/card/")

    assert response.status_code == 409
    body = response.json()
    assert body["missing"] == ["photo", "pvc_photo", "gender", "date_of_birth"]
    assert body["detail"] == (
        "To download your card, add a photo of you, a photo of your PVC, "
        "your gender, your date of birth."
    )
    assert client.get("/api/v1/me/member/").json()["card_missing"] == body["missing"]


def test_a_suspended_membership_has_no_card():
    client = complete()
    Member.objects.update(membership_state=MembershipState.SUSPENDED)

    response = client.get("/api/v1/me/member/card/")

    assert response.status_code == 409
    assert response.json()["missing"] == ["active_membership"]


def test_only_members_have_a_card():
    staff = User.objects.create_user(
        "NA-1", PASSWORD, full_name="Admin", role=Role.NATIONAL_ADMINISTRATOR
    )
    client = APIClient()
    client.force_authenticate(staff)
    assert client.get("/api/v1/me/member/card/").status_code == 403


def test_roles_print_once_the_identity_is_checked():
    complete()
    member = Member.objects.get()
    ward = RoleDefinition.objects.get(code=Role.WARD_COORDINATOR)
    RoleAssignment.objects.create(
        member=member, role=ward, authority=AuthoritySource.NATIONAL_ADMINISTRATOR
    )
    ended = RoleAssignment.objects.create(
        member=member,
        role=RoleDefinition.objects.get(code=Role.LGA_COORDINATOR),
        authority=AuthoritySource.NATIONAL_ADMINISTRATOR,
        status="revoked",
    )
    assert ended.effective_status() != "active"

    # Holding a role needs the identity check: until then the card says Member.
    assert card_roles(member) == []
    Member.objects.update(verification_status=VerificationStatus.VERIFIED)
    member.refresh_from_db()
    assert card_roles(member) == [ward.name]
    # A long name and roles still fit the card.
    member.full_name = "Abdulrahman Muhammadu Sani Ibrahim Danjuma Ọlọ́runfẹ́mi"
    assert card_pdf.render(member).startswith(b"%PDF")


# --- disabilities -----------------------------------------------------------------


def test_joining_records_disabilities():
    join(disabilities=json.dumps(["other", "visual"]), disability_other="  Lost a hand ")

    member = Member.objects.get()
    assert member.disabilities == ["visual", "other"]
    assert member.disability_other == "Lost a hand"


@pytest.mark.parametrize(
    ("fields", "error_field"),
    [
        ({"disabilities": json.dumps(["deafness"])}, "disabilities"),
        ({"disabilities": json.dumps(["other"])}, "disability_other"),
        ({"disabilities": "not a list"}, "disabilities"),
    ],
)
def test_joining_refuses_unknown_or_unexplained_disabilities(fields, error_field):
    data = {
        "phone_number": PHONE,
        "vin": VIN,
        "delim": "18/23/01/001",
        "full_name": "Musa Bello",
        "pin": "482915",
        "consent": True,
        **fields,
    }
    response = APIClient().post("/api/v1/register/", data, format="multipart")
    assert response.status_code == 400
    assert error_field in response.json(), response.json()
    assert not Member.objects.exists()


def test_a_member_gives_changes_and_takes_back_their_disability():
    client = join()

    changed = client.patch(
        "/api/v1/me/member/",
        {"disabilities": ["hearing", "other"], "disability_other": "Epilepsy"},
        format="json",
    )
    assert changed.status_code == 200, changed.content
    me = client.get("/api/v1/me/member/").json()
    assert me["disabilities"] == ["hearing", "other"]
    assert me["disability_other"] == "Epilepsy"

    # Without "other", its words go too.
    client.patch("/api/v1/me/member/", {"disabilities": ["hearing"]}, format="json")
    member = Member.objects.get()
    assert (member.disabilities, member.disability_other) == (["hearing"], "")

    client.patch("/api/v1/me/member/", {"disabilities": []}, format="json")
    assert Member.objects.get().disabilities == []

    alone = client.patch("/api/v1/me/member/", {"disability_other": "Epilepsy"}, format="json")
    assert alone.status_code == 400


def test_disabilities_are_private_details_and_never_on_card_checks():
    join(disabilities=json.dumps(["albinism"]))
    member = Member.objects.get()
    request = RequestFactory().get("/")

    request.user = User.objects.create_user(
        "SC-KD",
        PASSWORD,
        full_name="SC-KD",
        role=Role.STATE_COORDINATOR,
        scope_level=GeographyLevel.STATE,
        state=State.objects.get(code="KD"),
    )
    seen = MemberSerializer(member, context={"request": request}).data
    assert seen["sensitive_visible"] is True
    assert seen["disabilities"] == ["albinism"]

    request.user = User.objects.create_user(
        "OBS-1", PASSWORD, full_name="OBS-1", role=Role.OBSERVER
    )
    hidden = MemberSerializer(member, context={"request": request}).data
    assert hidden["sensitive_visible"] is False
    assert hidden["disabilities"] is None
    assert hidden["disability_other"] is None

    check = CardVerificationSerializer(
        member, context={"request": request, "token": card_token(member)}
    ).data
    assert not any("disab" in key for key in check)

"""Member registration, member home, card verification, volunteering, announcements."""

import io
import re

import pytest
from PIL import Image
from rest_framework.test import APIClient

from apps.access.models import AuditEvent
from apps.accounts.models import User
from apps.accounts.verification import PhoneVerification
from apps.core.choices import GeographyLevel, Role
from apps.geography.models import Lga, PollingUnit, State
from apps.geography.polling_units import load_polling_units
from apps.membership.models import AccreditedAgent, Member, PvcUpdate
from conftest import PASSWORD

# Code-checking views run outside the request transaction (so failed attempts
# count); use real transactions so a failed request does not poison the test.
pytestmark = pytest.mark.django_db(transaction=True, serialized_rollback=True)

VIN = "90F5B0E8D0295847263"
PHONE = "08031234567"
PIN = "482915"


@pytest.fixture(autouse=True)
def setup(settings, tmp_path, monkeypatch):
    settings.MEDIA_ROOT = tmp_path
    load_polling_units(lga_codes=["KD-ZARIA"])
    outbox = []
    monkeypatch.setattr(
        "apps.accounts.verification.send_sms", lambda phone, text: outbox.append((phone, text))
    )
    return outbox


@pytest.fixture
def outbox(setup):
    return setup


def last_code(outbox):
    return re.search(r"\b(\d{6})\b", outbox[-1][1]).group(1)


def image(fmt="JPEG", name="photo.jpg"):
    buffer = io.BytesIO()
    Image.new("RGB", (40, 40), (10, 36, 99)).save(buffer, fmt)
    buffer.seek(0)
    buffer.name = name
    return buffer


def staff(access_id, **fields):
    user = User.objects.create_user(access_id, PASSWORD, full_name=access_id, **fields)
    client = APIClient()
    client.force_authenticate(user)
    return client


def kd_coordinator():
    return staff(
        "SC-KD",
        role=Role.STATE_COORDINATOR,
        scope_level=GeographyLevel.STATE,
        state=State.objects.get(code="KD"),
    )


def register(client, outbox, phone=PHONE, vin=VIN, **overrides):
    assert (
        client.post(
            "/api/v1/register/code/", {"phone_number": phone, "vin": vin}, format="json"
        ).status_code
        == 200
    )
    data = {
        "phone_number": phone,
        "code": last_code(outbox),
        "vin": vin,
        "delim": "18/23/01/001",
        "full_name": "Musa Bello",
        "gender": "male",
        "date_of_birth": "1990-05-14",
        "occupation": "Teacher",
        "pin": PIN,
        "consent": True,
        "photo": image(),
        "pvc_image": image("PNG", "pvc.png"),
        **overrides,
    }
    return client.post("/api/v1/register/", data, format="multipart")


# --- self-registration ----------------------------------------------------------


def test_self_registration_end_to_end(outbox):
    client = APIClient()
    response = register(client, outbox)
    assert response.status_code == 201, response.content
    body = response.json()
    card = body["member"]
    assert body["access"] and body["refresh"]
    assert card["membership_number"] == "TGCG-000001"
    assert card["vin"].endswith("7263") and VIN not in str(body)
    assert card["phone_number"] == "+2348031234567"
    assert card["scope"]["pollingUnitId"] == "18-23-01-001"
    assert card["scope"]["lgaId"] == "KD-ZARIA"
    assert card["polling_unit"]["name"] == "KWARBAI I/ PRI. SCH. MADARKACI"
    assert card["qr_token"]

    member = Member.objects.get()
    assert member.registration_channel == "self"
    assert member.phone_verified_at and member.consent_at
    assert member.user.role == Role.MEMBER

    login = client.post(
        "/api/v1/auth/login/", {"username": "0803 123 4567", "password": PIN}, format="json"
    )
    assert login.status_code == 200
    assert login.json()["user"]["role"] == "member"
    assert login.json()["user"]["capabilities"] == []


@pytest.mark.parametrize("delim", ["18-23-01-001", "18/23/1/1", " 18 / 23 / 01 / 001 "])
def test_delim_formats(outbox, delim):
    assert register(APIClient(), outbox, delim=delim).status_code == 201


def test_unknown_delim_rejected(outbox):
    response = register(APIClient(), outbox, delim="18/23/99/999")
    assert response.status_code == 400
    assert "delim" in response.json()


def test_delim_lookup_shows_the_unit_without_its_location():
    PollingUnit.objects.filter(code="18-23-01-001").update(
        latitude="11.085500", longitude="7.719900", mapping_status="confirmed"
    )
    found = APIClient().get("/api/v1/register/polling-unit/", {"delim": "18/23/1/1"})
    assert found.status_code == 200
    body = found.json()
    assert body["code"] == "18-23-01-001"
    assert body["lga"] and body["state"]
    assert body["navigation"] is None

    assert (
        APIClient().get("/api/v1/register/polling-unit/", {"delim": "18/23/99/999"}).status_code
        == 400
    )
    assert (
        APIClient().get("/api/v1/register/polling-unit/", {"delim": "nonsense"}).status_code == 400
    )


def test_validation_does_not_burn_the_code(outbox):
    client = APIClient()
    bad = register(client, outbox, delim="nonsense")
    assert bad.status_code == 400
    # The same code still works once the mistake is fixed.
    data = {
        "phone_number": PHONE,
        "code": last_code(outbox),
        "vin": VIN,
        "delim": "18/23/01/001",
        "full_name": "Musa Bello",
        "gender": "male",
        "pin": PIN,
        "consent": True,
        "photo": image(),
        "pvc_image": image(),
    }
    assert client.post("/api/v1/register/", data, format="multipart").status_code == 201


def test_wrong_codes_are_counted_and_lock_the_code(outbox):
    client = APIClient()
    for _ in range(5):
        assert register_with_code(client, outbox, "000000").status_code == 400
    verification = PhoneVerification.objects.get()
    assert verification.attempts == 5
    response = register_with_code(client, outbox, last_code(outbox), request_code=False)
    assert response.status_code == 400  # even the right code is now refused


def register_with_code(client, outbox, code, request_code=True):
    if request_code and not outbox:
        client.post("/api/v1/register/code/", {"phone_number": PHONE, "vin": VIN}, format="json")
    data = {
        "phone_number": PHONE,
        "code": code,
        "vin": VIN,
        "delim": "18/23/01/001",
        "full_name": "Musa Bello",
        "gender": "male",
        "pin": PIN,
        "consent": True,
        "photo": image(),
        "pvc_image": image(),
    }
    return client.post("/api/v1/register/", data, format="multipart")


def test_code_resend_is_rate_limited(outbox):
    client = APIClient()
    payload = {"phone_number": PHONE, "vin": VIN}
    assert client.post("/api/v1/register/code/", payload, format="json").status_code == 200
    assert client.post("/api/v1/register/code/", payload, format="json").status_code == 400
    assert len(outbox) == 1


@pytest.mark.parametrize(
    "overrides, field",
    [
        ({"pin": "123456"}, "pin"),
        ({"pin": "777777"}, "pin"),
        ({"pin": "12a456"}, "pin"),
        ({"consent": False}, "consent"),
        ({"vin": "TOO-SHORT"}, "vin"),
        ({"photo": io.BytesIO(b"not an image")}, "photo"),
        ({"gender": "other"}, "gender"),
    ],
)
def test_invalid_registrations_rejected(outbox, overrides, field):
    if "photo" in overrides:
        overrides["photo"].name = "selfie.jpg"
    response = register(APIClient(), outbox, **overrides)
    assert response.status_code == 400
    assert field in response.json(), response.json()
    assert not Member.objects.exists()


def test_one_member_per_pvc_and_phone(outbox):
    client = APIClient()
    assert register(client, outbox).status_code == 201
    same_vin = client.post(
        "/api/v1/register/code/", {"phone_number": "08039990000", "vin": VIN}, format="json"
    )
    assert same_vin.status_code == 400 and "vin" in same_vin.json()
    same_phone = client.post("/api/v1/register/code/", {"phone_number": PHONE}, format="json")
    assert same_phone.status_code == 400 and "phone_number" in same_phone.json()


# --- joining without SMS ----------------------------------------------------------


def join(client, **overrides):
    """Joining in the app with what USSD asks for, plus the account's name and PIN."""
    data = {
        "phone_number": PHONE,
        "vin": VIN,
        "delim": "18/23/01/001",
        "full_name": "Musa Bello",
        "pin": PIN,
        "consent": True,
        **overrides,
    }
    return client.post("/api/v1/register/", data, format="multipart")


def test_options_say_whether_joining_needs_an_sms_code(settings):
    client = APIClient()
    settings.REGISTRATION_PHONE_CODE_REQUIRED = True
    assert client.get("/api/v1/register/options/").json() == {"phone_code_required": True}
    settings.REGISTRATION_PHONE_CODE_REQUIRED = False
    assert client.get("/api/v1/register/options/").json() == {"phone_code_required": False}


def test_without_sms_members_join_with_what_ussd_asks(outbox, settings):
    settings.REGISTRATION_PHONE_CODE_REQUIRED = False
    response = join(APIClient())
    assert response.status_code == 201, response.content
    assert outbox == []
    member = Member.objects.get()
    # Nothing proved the number, so it is not recorded as confirmed.
    assert member.phone_verified_at is None and member.consent_at
    assert not member.photo and not member.pvc_image and member.gender == ""
    assert not member.polling_unit_from_list  # the code from the card
    assert member.user.role == Role.MEMBER
    assert response.json()["member"]["verification_status"] == "pendingReview"
    results = dict(member.verification_checks.values_list("kind", "result"))
    assert results["phoneVerified"] == results["pvcCaptured"] == "pending"
    assert results["selfieCaptured"] == "failed"


def test_without_sms_a_code_given_is_still_checked(outbox, settings):
    settings.REGISTRATION_PHONE_CODE_REQUIRED = False
    client = APIClient()
    wrong = register_with_code(client, outbox, "000000")
    assert wrong.status_code == 400 and "code" in wrong.json()
    assert not Member.objects.exists()
    right = register_with_code(client, outbox, last_code(outbox), request_code=False)
    assert right.status_code == 201, right.content
    assert Member.objects.get().phone_verified_at


def test_while_sms_works_joining_needs_the_code(outbox, settings):
    settings.REGISTRATION_PHONE_CODE_REQUIRED = True
    response = join(APIClient())
    assert response.status_code == 400 and "code" in response.json()
    assert not Member.objects.exists()


def test_without_sms_still_one_member_per_pvc_and_phone(outbox, settings):
    settings.REGISTRATION_PHONE_CODE_REQUIRED = False
    client = APIClient()
    assert join(client).status_code == 201
    same_vin = join(client, phone_number="08039990000")
    assert same_vin.status_code == 400 and "vin" in same_vin.json()
    same_phone = join(client, vin="90F5B0E8D0295847264")
    assert same_phone.status_code == 400 and "phone_number" in same_phone.json()
    assert Member.objects.count() == 1


# --- finding a polling unit by name --------------------------------------------------


def test_places_lead_from_the_state_to_the_polling_unit_without_map_positions():
    client = APIClient()
    states = client.get("/api/v1/register/places/states/").json()
    assert {"code": "KD", "name": State.objects.get(code="KD").name} in states
    lgas = client.get("/api/v1/register/places/lgas/", {"state": "KD"}).json()
    assert {"code": "KD-ZARIA", "name": Lga.objects.get(code="KD-ZARIA").name} in lgas
    ward = PollingUnit.objects.select_related("ward").get(code="18-23-01-001").ward
    wards = client.get("/api/v1/register/places/wards/", {"lga": "KD-ZARIA"}).json()
    assert {"code": ward.code, "name": ward.name} in wards

    units = client.get("/api/v1/register/places/polling-units/", {"ward": ward.code}).json()
    assert len(units) == PollingUnit.objects.filter(ward=ward).count()
    unit = next(u for u in units if u["code"] == "18-23-01-001")
    assert unit["name"] == "KWARBAI I/ PRI. SCH. MADARKACI"
    assert set(unit) == {"code", "name", "location"}  # no coordinates


def test_places_need_the_area_to_list():
    client = APIClient()
    missing = client.get("/api/v1/register/places/wards/")
    assert missing.status_code == 400 and "lga" in missing.json()
    assert client.get("/api/v1/register/places/lgas/", {"state": "XX"}).json() == []


def test_a_unit_found_by_name_is_marked_for_a_coordinator_to_check(outbox, settings):
    settings.REGISTRATION_PHONE_CODE_REQUIRED = False
    response = join(APIClient(), delim="", polling_unit="18-23-01-001")
    assert response.status_code == 201, response.content
    assert response.json()["member"]["polling_unit"]["code"] == "18-23-01-001"
    member = Member.objects.get()
    assert member.polling_unit_from_list
    check = member.verification_checks.get(kind="pvcCaptured")
    assert check.result == "pending" and "chosen from the list" in check.detail


def joined_without_photos(settings):
    settings.REGISTRATION_PHONE_CODE_REQUIRED = False
    response = join(APIClient())
    assert response.status_code == 201, response.content
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {response.json()['access']}")
    return client


def add_selfie(client, upload=None):
    upload = upload or image()
    return client.post("/api/v1/me/member/photo/", {"photo": upload}, format="multipart")


def test_a_member_adds_the_selfie_they_left_out_when_joining(outbox, settings):
    client = joined_without_photos(settings)
    assert client.get("/api/v1/me/member/").json()["photo_url"] is None

    selfie = add_selfie(client)
    assert selfie.status_code == 200, selfie.content
    assert selfie.json()["photo_url"] and client.get("/api/v1/me/member/photo/").status_code == 200

    member = Member.objects.get()
    results = dict(member.verification_checks.values_list("kind", "result"))
    assert results["selfieCaptured"] == "passed"
    # Photos are something to review, never a verification by themselves.
    assert member.verification_status == "pendingReview"
    added = AuditEvent.objects.get(action="member.photoAdded", target_id=member.membership_number)
    assert added.changes == {"kind": "selfie"}


def test_once_the_identity_is_checked_only_a_coordinator_changes_the_selfie(outbox, settings):
    client = joined_without_photos(settings)
    assert add_selfie(client).status_code == 200
    # Until the check, a clearer photo may replace the first.
    assert add_selfie(client).status_code == 200
    assert AuditEvent.objects.filter(action="member.photoReplaced").count() == 1

    Member.objects.update(verification_status="verified")
    again = add_selfie(client)
    assert again.status_code == 400 and "photo" in again.json()
    assert Member.objects.get().verification_status == "verified"


def test_own_selfies_must_be_images_and_only_members_add_them(outbox, settings):
    client = joined_without_photos(settings)
    missing = client.post("/api/v1/me/member/photo/", {}, format="multipart")
    assert missing.status_code == 400 and "photo" in missing.json()
    not_image = io.BytesIO(b"not an image")
    not_image.name = "selfie.jpg"
    assert add_selfie(client, not_image).status_code == 400
    assert not Member.objects.get().photo
    assert add_selfie(kd_coordinator()).status_code == 403


# --- the one PVC scan after joining ------------------------------------------------------


def scan(client, **overrides):
    data = {
        "pvc_image": image("PNG", "pvc.png"),
        "full_name": "Musa Ibrahim Bello",
        "vin": VIN,
        "delim": "18/23/01/014",
        "gender": "male",
        "date_of_birth": "1990-05-14",
        "occupation": "Trader",
        **overrides,
    }
    return client.post("/api/v1/me/member/pvc-scan/", data, format="multipart")


def test_details_typed_when_joining_are_replaced_by_one_pvc_scan(outbox, settings):
    client = joined_without_photos(settings)
    assert client.get("/api/v1/me/member/pvc-scan/").json() == {"can_scan": True, "latest": None}

    response = scan(client)
    assert response.status_code == 201, response.content
    assert response.json()["status"] == "applied"
    member = Member.objects.select_related("polling_unit", "user").get()
    assert member.full_name == member.user.full_name == "Musa Ibrahim Bello"
    assert member.gender == "male" and str(member.date_of_birth) == "1990-05-14"
    assert member.occupation == "Trader" and member.polling_unit.code == "18-23-01-014"
    assert member.pvc_image and not member.polling_unit_from_list
    checks = dict(member.verification_checks.values_list("kind", "result"))
    assert checks["pvcCaptured"] == "passed"
    event = AuditEvent.objects.get(action="member.pvcUpdateApplied")
    assert event.changes["fields"] == [
        "full_name",
        "gender",
        "date_of_birth",
        "occupation",
        "polling_unit",
    ]

    # One scan per member.
    state = client.get("/api/v1/me/member/pvc-scan/").json()
    assert state["can_scan"] is False and state["latest"]["status"] == "applied"
    assert scan(client).status_code == 400


def test_a_card_scanned_or_uploaded_when_joining_is_not_scanned_again(outbox):
    client, _ = member_client(outbox)  # joined with a photo of the PVC
    assert client.get("/api/v1/me/member/pvc-scan/").json()["can_scan"] is False
    assert scan(client).status_code == 400
    assert not PvcUpdate.objects.exists()


def test_a_checked_members_scan_waits_for_a_second_check(outbox, settings):
    client = joined_without_photos(settings)
    Member.objects.update(verification_status="verified")
    waiting = scan(client)
    assert waiting.status_code == 201 and waiting.json()["status"] == "pending"
    member = Member.objects.get()
    assert member.full_name == "Musa Bello" and not member.pvc_image  # nothing changes yet
    assert client.get("/api/v1/me/member/pvc-scan/").json()["can_scan"] is False

    reviewer = kd_coordinator()
    listed = reviewer.get("/api/v1/pvc-updates/", {"status": "pending"}).json()
    assert listed["count"] == 1
    update = listed["results"][0]
    changes = {change["field"]: change for change in update["changes"]}
    assert changes["full_name"]["from"] == "Musa Bello"
    assert changes["full_name"]["to"] == "Musa Ibrahim Bello"
    assert changes["polling_unit"]["to"].startswith("18-23-01-014 ")
    url = f"/api/v1/pvc-updates/{update['id']}/"
    assert reviewer.get(f"{url}card/").status_code == 200

    assert reviewer.post(f"{url}reject/", {}, format="json").status_code == 400  # say why
    rejected = reviewer.post(f"{url}reject/", {"reason": "The photo is blurred."}, format="json")
    assert rejected.json()["status"] == "rejected"
    # A rejected scan does not count: the member may scan again.
    state = client.get("/api/v1/me/member/pvc-scan/").json()
    assert state["can_scan"] is True
    assert state["latest"]["decision_reason"] == "The photo is blurred."

    again = scan(client).json()
    approved = reviewer.post(
        f"/api/v1/pvc-updates/{again['id']}/approve/", {"reason": "Card matches."}, format="json"
    )
    assert approved.status_code == 200, approved.content
    assert approved.json()["status"] == "approved"
    member = Member.objects.select_related("polling_unit", "verified_by").get()
    assert member.full_name == "Musa Ibrahim Bello" and member.polling_unit.code == "18-23-01-014"
    # The second check keeps the identity checked, now on this reviewer's word.
    assert member.verification_status == "verified" and member.verified_by.access_id == "SC-KD"
    assert AuditEvent.objects.filter(action="member.pvcUpdateApproved").count() == 1


def test_a_scan_cannot_take_another_members_pvc(outbox, settings):
    client = joined_without_photos(settings)
    member_client(outbox, phone="08039990000", vin="90F5B0E8D0295847264")
    taken = scan(client, vin="90F5B0E8D0295847264")
    assert taken.status_code == 400 and "vin" in taken.json()


def test_a_waiting_scan_can_be_withdrawn(outbox, settings):
    client = joined_without_photos(settings)
    Member.objects.update(verification_status="verified")
    waiting = scan(client).json()
    withdrawn = client.post(f"/api/v1/me/member/pvc-scan/{waiting['id']}/withdraw/")
    assert withdrawn.status_code == 200 and withdrawn.json()["status"] == "withdrawn"
    assert client.get("/api/v1/me/member/pvc-scan/").json()["can_scan"] is True


def test_only_coordinators_over_the_member_check_their_scan(outbox, settings):
    client = joined_without_photos(settings)
    Member.objects.update(verification_status="verified")
    waiting = scan(client).json()
    kano = staff(
        "SC-KN",
        role=Role.STATE_COORDINATOR,
        scope_level=GeographyLevel.STATE,
        state=State.objects.get(code="KN"),
    )
    assert kano.get("/api/v1/pvc-updates/").json()["count"] == 0
    approve = kano.post(f"/api/v1/pvc-updates/{waiting['id']}/approve/", {}, format="json")
    assert approve.status_code == 404
    assert PvcUpdate.objects.get().status == "pending"


def test_a_member_adds_the_gender_and_birth_date_left_out_when_joining(outbox, settings):
    client = joined_without_photos(settings)
    member = Member.objects.get()
    checks = dict(member.verification_checks.values_list("kind", "result"))
    assert checks["duplicates"] == "pending"  # no birth date to compare yet

    response = client.patch(
        "/api/v1/me/member/", {"gender": "female", "date_of_birth": "1992-03-08"}, format="json"
    )
    assert response.status_code == 200, response.content
    card = response.json()
    assert card["gender"] == "female" and card["date_of_birth"] == "1992-03-08"
    checks = dict(member.verification_checks.values_list("kind", "result"))
    assert checks["duplicates"] == "passed"
    # Which details changed, never their values.
    event = AuditEvent.objects.get(action="member.detailsChanged")
    assert event.changes == {"fields": ["date_of_birth", "gender"]}


def test_once_the_identity_is_checked_gender_and_birth_date_are_for_a_coordinator(outbox, settings):
    client = joined_without_photos(settings)
    assert client.patch("/api/v1/me/member/", {"gender": "male"}, format="json").status_code == 200
    Member.objects.update(verification_status="verified")
    changed = client.patch("/api/v1/me/member/", {"gender": "female"}, format="json")
    assert changed.status_code == 400 and "gender" in changed.json()
    # The same value again, or a detail still missing, is fine.
    assert client.patch("/api/v1/me/member/", {"gender": "male"}, format="json").status_code == 200
    added = client.patch("/api/v1/me/member/", {"date_of_birth": "1992-03-08"}, format="json")
    assert added.status_code == 200
    member = Member.objects.get()
    assert member.gender == "male" and member.verification_status == "verified"


def test_a_birth_date_cannot_be_in_the_future(outbox, settings):
    client = joined_without_photos(settings)
    response = client.patch("/api/v1/me/member/", {"date_of_birth": "2999-01-01"}, format="json")
    assert response.status_code == 400 and "date_of_birth" in response.json()


def test_a_member_changes_their_occupation(outbox, settings):
    client = joined_without_photos(settings)
    response = client.patch("/api/v1/me/member/", {"occupation": "Trader"}, format="json")
    assert response.status_code == 200 and response.json()["occupation"] == "Trader"


def test_joining_needs_the_unit_code_or_a_unit_found_by_name(outbox, settings):
    settings.REGISTRATION_PHONE_CODE_REQUIRED = False
    missing = join(APIClient(), delim="")
    assert missing.status_code == 400 and "delim" in missing.json()
    unknown = join(APIClient(), delim="", polling_unit="18-23-99-999")
    assert unknown.status_code == 400 and "polling_unit" in unknown.json()
    assert not Member.objects.exists()


def test_pin_lockout_after_five_failures(outbox, settings):
    client = APIClient()
    register(client, outbox)
    for _ in range(settings.LOGIN_MAX_FAILED_ATTEMPTS):
        client.post("/api/v1/auth/login/", {"username": PHONE, "password": "999999"}, format="json")
    locked = client.post("/api/v1/auth/login/", {"username": PHONE, "password": PIN}, format="json")
    assert locked.status_code == 401
    assert User.objects.get(phone_number="+2348031234567").locked_until is not None


# --- assisted registration and PIN setup ----------------------------------------


def assisted(client, outbox, phone="08037776666", **overrides):
    APIClient().post("/api/v1/register/code/", {"phone_number": phone}, format="json")
    data = {
        "phone_number": phone,
        "code": last_code(outbox),
        "full_name": "Hauwa Sani",
        "gender": "female",
        "consent": True,
        "photo": image(),
        "scope": '{"level": "lga", "lgaId": "KD-ZARIA"}',
        **overrides,
    }
    return client.post("/api/v1/members/register/", data, format="multipart")


def test_coordinator_registers_without_pvc_then_member_sets_pin(outbox):
    coordinator = kd_coordinator()
    response = assisted(coordinator, outbox)
    assert response.status_code == 201, response.content
    body = response.json()
    assert body["registration_channel"] == "assisted"
    assert body["has_pvc"] is False
    assert body["has_app_account"] is False
    assert body["phone_verified"] is True
    assert body["scope"]["lgaId"] == "KD-ZARIA"

    member_app = APIClient()
    assert (
        member_app.post(
            "/api/v1/register/pin-code/", {"phone_number": "08037776666"}, format="json"
        ).status_code
        == 200
    )
    session = member_app.post(
        "/api/v1/register/pin/",
        {"phone_number": "08037776666", "code": last_code(outbox), "pin": "739104"},
        format="json",
    )
    assert session.status_code == 200, session.content
    assert session.json()["member"]["membership_number"] == body["membership_number"]
    assert (
        member_app.post(
            "/api/v1/auth/login/", {"username": "08037776666", "password": "739104"}, format="json"
        ).status_code
        == 200
    )


def test_coordinator_registration_with_pvc_uses_delim(outbox):
    response = assisted(kd_coordinator(), outbox, vin=VIN, delim="18/23/01/002", scope="")
    assert response.status_code == 201, response.content
    assert response.json()["scope"]["pollingUnitId"] == "18-23-01-002"


def test_without_pvc_location_must_be_precise(outbox):
    response = assisted(kd_coordinator(), outbox, scope='{"level": "state", "stateId": "KD"}')
    assert response.status_code == 400 and "scope" in response.json()


def test_assisted_registration_permissions(outbox):
    lagos = staff(
        "SC-LA",
        role=Role.STATE_COORDINATOR,
        scope_level=GeographyLevel.STATE,
        state=State.objects.get(code="LA"),
    )
    assert assisted(lagos, outbox).status_code == 403
    observer = staff("OB-1", role=Role.OBSERVER)
    assert assisted(observer, outbox, phone="08037776667").status_code == 403


def test_pin_code_for_unknown_phone_sends_nothing(outbox):
    response = APIClient().post(
        "/api/v1/register/pin-code/", {"phone_number": "08030000000"}, format="json"
    )
    assert response.status_code == 200
    assert outbox == []


# --- member home -------------------------------------------------------------------


def member_client(outbox, phone=PHONE, vin=VIN):
    session = register(APIClient(), outbox, phone=phone, vin=vin).json()
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {session['access']}")
    return client, session["member"]


def test_member_home(outbox):
    me, card = member_client(outbox)
    member_client(outbox, phone="08031234568", vin="90F5B0E8D0295847264")

    profile = me.get("/api/v1/me/member/").json()
    assert profile["membership_number"] == card["membership_number"]
    updated = me.patch(
        "/api/v1/me/member/", {"email": "musa@example.com", "full_name": "Hacker"}, format="json"
    ).json()
    assert updated["email"] == "musa@example.com"
    assert updated["full_name"] == "Musa Bello"  # identity fields come from the PVC

    photo = me.get("/api/v1/me/member/photo/")
    assert photo.status_code == 200 and photo["Content-Type"] == "image/jpeg"

    unit = me.get("/api/v1/me/polling-unit/").json()
    assert unit["polling_unit"]["code"] == "18-23-01-001"
    assert unit["members_in_unit"] == 2
    assert unit["polling_unit"]["navigation"] is None  # not mapped yet

    units = PollingUnit.objects.filter(code="18-23-01-001")
    units.update(
        latitude="11.085500",
        longitude="7.719900",
        mapping_status="pinned",
        location_accuracy_m=12,
        location_source="coordinator",
    )
    navigation = me.get("/api/v1/me/polling-unit/").json()["polling_unit"]["navigation"]
    assert navigation == {
        "latitude": 11.0855,
        "longitude": 7.7199,
        "confirmed": False,
        "accuracy_m": 12.0,
        "source": "coordinator",
        "note": "",
    }
    units.update(mapping_status="confirmed")
    assert me.get("/api/v1/me/polling-unit/").json()["polling_unit"]["navigation"]["confirmed"]
    # Pins that disagree are not good enough for directions.
    units.update(mapping_status="conflict")
    assert me.get("/api/v1/me/polling-unit/").json()["polling_unit"]["navigation"] is None

    coordinator = kd_coordinator()
    staff_unit = coordinator.get("/api/v1/geography/polling-units/18-23-01-001/").json()
    assert staff_unit["mapping_status"] == "conflict"
    assert staff_unit["navigation"] is None

    assert coordinator.get("/api/v1/me/member/").status_code == 403


def test_card_verification(outbox):
    me, card = member_client(outbox)
    coordinator = kd_coordinator()
    verified = coordinator.get("/api/v1/members/verify/", {"token": card["qr_token"]}).json()
    assert verified["valid"] is True
    assert verified["membership_number"] == card["membership_number"]
    assert coordinator.get(verified["photo_url"]).status_code == 200

    assert coordinator.get("/api/v1/members/verify/", {"token": "forged"}).status_code == 404
    assert me.get("/api/v1/members/verify/", {"token": card["qr_token"]}).status_code == 403


def test_staff_can_view_member_photo(outbox):
    _, card = member_client(outbox)
    coordinator = kd_coordinator()
    member = coordinator.get(f"/api/v1/members/{card['membership_number']}/").json()
    assert coordinator.get(member["photo_url"]).status_code == 200


# --- volunteering -------------------------------------------------------------------


def offer(client, interests, message=""):
    return client.post(
        "/api/v1/me/volunteer/", {"interests": interests, "message": message}, format="json"
    )


def test_members_volunteer_for_party_work_not_as_agents(outbox):
    me, card = member_client(outbox)
    assert offer(me, []).status_code == 400  # at least one kind of work
    assert offer(me, ["agent"]).status_code == 400  # not a kind of party work
    sent = offer(me, ["mobilisation", "electionDay"], "Free on weekends.")
    assert sent.status_code == 201, sent.content
    assert sent.json()["interests"] == ["mobilisation", "electionDay"]
    assert offer(me, ["logistics"]).status_code == 400  # one open offer at a time

    coordinator = kd_coordinator()
    queue = coordinator.get("/api/v1/volunteers/", {"status": "pending"}).json()
    assert queue["count"] == 1
    item = queue["results"][0]
    assert item["member_name"] == "Musa Bello"
    assert item["scope"]["pollingUnitId"] == "18-23-01-001"
    assert coordinator.post(f"/api/v1/volunteers/{item['id']}/decline/", {}).status_code == 400
    accepted = coordinator.post(f"/api/v1/volunteers/{item['id']}/accept/", {}).json()
    assert accepted["status"] == "accepted"

    # Accepting an offer does not make the member an agent.
    assert not AccreditedAgent.objects.exists()
    assert "active_accreditation" not in me.get("/api/v1/me/member/").json()
    assert "agent_id" not in accepted


def test_member_withdraws_an_offer_and_sends_another(outbox):
    me, _ = member_client(outbox)
    other, _ = member_client(outbox, phone="08031234568", vin="90F5B0E8D0295847264")
    first = offer(me, ["campaign"]).json()
    assert other.post(f"/api/v1/me/volunteer/{first['id']}/withdraw/").status_code == 404
    withdrawn = me.post(f"/api/v1/me/volunteer/{first['id']}/withdraw/")
    assert withdrawn.json()["status"] == "withdrawn"
    assert me.post(f"/api/v1/me/volunteer/{first['id']}/withdraw/").status_code == 400
    assert offer(me, ["publicity"]).status_code == 201
    assert [o["status"] for o in me.get("/api/v1/me/volunteer/").json()] == [
        "pending",
        "withdrawn",
    ]


def test_members_without_a_polling_unit_can_volunteer(outbox):
    assisted(kd_coordinator(), outbox)
    member = Member.objects.get()
    user = User.objects.create_user(
        member.membership_number, "x", full_name=member.full_name, role=Role.MEMBER
    )
    member.user = user
    member.save()
    client = APIClient()
    client.force_authenticate(user)
    assert offer(client, ["logistics"]).status_code == 201


# --- announcements ---------------------------------------------------------------


def test_announcements_reach_members_in_the_area(outbox):
    me, _ = member_client(outbox)
    kd = kd_coordinator()
    assert (
        kd.post(
            "/api/v1/announcements/",
            {"title": "Kaduna rally", "body": "Saturday 10am."},
            format="json",
        ).status_code
        == 201
    )
    assert (
        kd.post(
            "/api/v1/announcements/",
            {"title": "Lagos", "body": "x", "scope": {"level": "state", "stateId": "LA"}},
            format="json",
        ).status_code
        == 403
    )
    lagos_admin = staff("NA-1", role=Role.NATIONAL_ADMINISTRATOR)
    lagos_admin.post(
        "/api/v1/announcements/",
        {"title": "Lagos only", "body": "x", "scope": {"level": "state", "stateId": "LA"}},
        format="json",
    )
    lagos_admin.post(
        "/api/v1/announcements/",
        {"title": "Zaria LGA", "body": "x", "scope": {"level": "lga", "lgaId": "KD-ZARIA"}},
        format="json",
    )
    titles = [a["title"] for a in me.get("/api/v1/me/announcements/").json()["results"]]
    assert sorted(titles) == ["Kaduna rally", "Zaria LGA"]
    assert Lga.objects.get(code="KD-ZARIA").inec_code == "18-23"


# --- identity checks at registration ------------------------------------------------


def test_registration_records_checks_and_awaits_review(outbox):
    _, card = member_client(outbox)
    assert card["verification_status"] == "pendingReview"
    member = Member.objects.get(membership_number=card["membership_number"])
    results = dict(member.verification_checks.values_list("kind", "result"))
    assert results["pvcCaptured"] == "passed" and results["phoneVerified"] == "passed"
    # What cannot be checked is recorded as such, never as passed.
    assert results["pvcValidated"] == results["liveness"] == results["faceMatch"] == "unsupported"

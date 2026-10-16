"""Identity checks and review, membership standing, personal details and
polling unit changes (docs/access-control.md §8-10)."""

import io
from datetime import date

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone
from PIL import Image
from rest_framework_simplejwt.token_blacklist.models import BlacklistedToken
from rest_framework_simplejwt.tokens import RefreshToken

from apps.access import engine
from apps.access.models import AuditEvent, RoleAssignment, RoleDefinition
from apps.access.tests.test_access import KD, KN, appoint, client_for, custom_role, member_account
from apps.accounts.models import User
from apps.core.choices import GeographyLevel, Role
from apps.core.permissions import Capability, capabilities_for
from apps.geography.models import Lga, PollingUnit, Ward
from apps.geography.scope import ScopeRef
from apps.membership import identity
from apps.membership.models import VerificationCheck, VolunteerApplication
from conftest import PASSWORD, make_member

pytestmark = pytest.mark.django_db
C = Capability


@pytest.fixture(autouse=True)
def media(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path


@pytest.fixture
def admin():
    return User.objects.create_user(
        "NA-1", PASSWORD, full_name="National Admin", role=Role.NATIONAL_ADMINISTRATOR
    )


@pytest.fixture
def kd_units():
    ward = Ward.objects.create(
        code="18-08-01", name="Ward 01", lga=Lga.objects.get(code="KD-KADUNA-NORTH")
    )
    return [
        PollingUnit.objects.create(code=f"18-08-01-00{i}", name=f"Unit {i}", ward=ward)
        for i in (1, 2)
    ]


def image(fmt="JPEG", name="card.jpg"):
    buffer = io.BytesIO()
    Image.new("RGB", (40, 40), (10, 36, 99)).save(buffer, fmt)
    buffer.seek(0)
    buffer.name = name
    return buffer


def staff_with(admin, phone, name, permissions, areas=(KD,)):
    """A verified member appointed to a role holding `permissions` in `areas`."""
    member = member_account(phone, name=name)
    role = custom_role(admin, f"{name} role", list(permissions))
    assert appoint(admin, member, role, list(areas)).status_code == 201
    return member


def fresh(user):
    """The account as a new request would load it (no cached member)."""
    return User.objects.get(pk=user.pk)


def checked_member(phone, name="Musa Bello", lga="KD-KADUNA-NORTH", **extra):
    """A member with a VIN, a selfie and a confirmed phone, after the checks ran."""
    member = make_member(
        phone=phone,
        lga=lga,
        full_name=name,
        vin=f"90F5B0E8D0{phone[-9:]}",
        phone_verified_at=timezone.now(),
        **extra,
    )
    member.photo = "members/photos/selfie.jpg"
    member.save()
    return identity.record_checks(member)


# --- identity checks -------------------------------------------------------------


def test_checks_record_only_what_was_actually_done(kd_units):
    member = make_member(
        phone="08033330001", vin="90F5B0E8D0295847001", date_of_birth=date(1990, 5, 14)
    )
    member.scope_level = GeographyLevel.POLLING_UNIT
    member.polling_unit = kd_units[0]
    member.normalize_scope()
    member.phone_verified_at = timezone.now()
    member.photo = "members/photos/selfie.jpg"
    member.pvc_image = "members/pvc/card.jpg"
    member.save()
    member = identity.record_checks(member)
    results = dict(member.verification_checks.values_list("kind", "result"))
    assert results == {
        "pvcCaptured": "passed",
        "cardMatch": "pending",  # no reader here, so the card photo was not read
        "pvcValidated": "unsupported",
        "duplicates": "passed",
        "phoneVerified": "passed",
        "selfieCaptured": "passed",
        "liveness": "unsupported",
        "faceMatch": "unsupported",
    }
    # Unsupported checks are never treated as passed: a person must review.
    assert member.verification_status == "pendingReview"
    card = member.verification_checks.get(kind="cardMatch")
    assert card.detail == "The card photo could not be read this time." and card.basis == ""
    detail = member.verification_checks.get(kind="pvcValidated").detail
    assert "No authoritative INEC source" in detail
    assert "not biometric" in member.verification_checks.get(kind="selfieCaptured").detail
    event = AuditEvent.objects.get(action="member.checksRecorded")
    assert event.changes["to"] == "pendingReview" and "vin" not in str(event.changes)


def test_a_missing_pvc_or_a_possible_duplicate_is_never_passed():
    born = date(1990, 1, 2)
    first = make_member(phone="08033330002", full_name="Ada Obi", date_of_birth=born)
    second = make_member(phone="08033330003", full_name="ada obi", date_of_birth=born)
    second = identity.record_checks(second)
    checks = {check.kind: check for check in second.verification_checks.all()}
    assert checks["pvcCaptured"].result == "failed"
    assert checks["duplicates"].result == "pending"
    # A count only: the other member may be outside the reviewer's area.
    assert checks["duplicates"].detail.startswith("1 other member")
    assert first.membership_number not in checks["duplicates"].detail
    assert checks["phoneVerified"].result == "pending"

    # Without a date of birth nothing was compared, so nothing passed.
    undated = identity.record_checks(make_member(phone="08033330004", full_name="Ada Obi"))
    assert undated.verification_checks.get(kind="duplicates").result == "pending"
    # A VIN typed in without a photo of the card is not a captured card.
    typed = make_member(phone="08033330005", vin="90F5B0E8D0295847005")
    typed.scope_level = GeographyLevel.POLLING_UNIT
    typed.polling_unit = PollingUnit.objects.create(
        code="18-08-02-001",
        name="Unit A",
        ward=Ward.objects.create(
            code="18-08-02", name="Ward 02", lga=Lga.objects.get(code="KD-KADUNA-NORTH")
        ),
    )
    typed.normalize_scope()
    typed.save()
    typed = identity.record_checks(typed)
    assert typed.verification_checks.get(kind="pvcCaptured").result == "pending"
    assert typed.verification_checks.get(kind="cardMatch").result == "pending"


# --- automatic verification ------------------------------------------------------

VIN = "90F5B0E8D0295847001"
READ_CARD = identity.read_card  # the real reader, before each test takes it offline


def card_text(name="BELLO\nMUSA", vin=VIN):
    """What a reader returns for a card photo, with the name on two lines."""
    return f"INDEPENDENT NATIONAL ELECTORAL COMMISSION\nVOTER'S CARD\n{name}\nVIN {vin}\nM 1990"


def read_as(monkeypatch, text):
    """Card photos read as `text`; returns the members whose card was read."""
    reads = []

    def read(member):
        reads.append(member.pk)
        return text

    monkeypatch.setattr(identity, "read_card", read)
    return reads


def complete(member, unit, vin=VIN):
    """The member's profile from their card: VIN, unit, card photo, selfie, gender, birth date."""
    member = placed(member, unit)
    member.vin = vin
    member.gender = "male"
    member.date_of_birth = date(1990, 5, 14)
    member.photo = "members/photos/selfie.jpg"
    member.pvc_image = "members/pvc/card.jpg"
    member.save()
    return member


def test_a_complete_profile_from_the_card_is_verified_automatically(admin, monkeypatch, kd_units):
    # The surname first, a title the card does not carry, O misread for 0 in the VIN.
    reads = read_as(monkeypatch, card_text(vin="9OF5B0E8D0295847OO1"))
    member = member_account("08033330021", name="Alhaji Musa Bello", verified=False)
    member = identity.record_checks(complete(member, kd_units[0]))
    assert reads == [member.pk]
    results = dict(member.verification_checks.values_list("kind", "result"))
    assert results["cardMatch"] == "passed"
    assert results["phoneVerified"] == "pending"  # not part of the policy
    # A policy, not proof: what cannot be checked stays unsupported.
    assert {results[kind] for kind in ("pvcValidated", "liveness", "faceMatch")} == {"unsupported"}
    assert member.verification_status == "verified" and member.verified_by is None
    event = AuditEvent.objects.get(action="member.verifiedAutomatically")
    assert event.actor is None and event.changes["policy"] == "cardSelfieNoDuplicate"
    assert event.changes["checks"]["cardMatch"] == "passed"
    card = member.verification_checks.get(kind="cardMatch")
    assert card.detail == "The name and VIN given match the text read from the card photo."
    # A keyed fingerprint of what was compared: neither the VIN nor the text read.
    assert len(card.basis) == 64 and VIN not in card.basis
    assert VIN not in str(event.changes) and "VOTER" not in str(event.changes)

    # Roles, like any verification, and the review shows nobody decided.
    role = custom_role(admin, "Ward Mobiliser", [C.SEND_BROADCAST])
    assert appoint(admin, member, role, [KD]).status_code == 201
    verifier = staff_with(admin, "08033330022", "Kaduna Verifier", [C.VERIFY_MEMBER_IDENTITY])
    seen = client_for(verifier.user).get(
        f"/api/v1/members/{member.membership_number}/verification/"
    )
    assert seen.json()["verified_automatically"] is True and seen.json()["reviewed_by"] is None
    # Checks run again keep the decision and read nothing.
    identity.record_checks(member)
    assert reads == [member.pk]


def test_the_card_photo_is_read_by_the_server_reader(monkeypatch, kd_units):
    monkeypatch.setattr(identity, "read_card", READ_CARD)
    sizes = []

    def read_text(photo):
        sizes.append(Image.open(photo).size)
        return card_text()

    monkeypatch.setattr(identity.pvc_reading, "read_text", read_text)
    member = complete(make_member(phone="08033330023"), kd_units[0])
    member.pvc_image = SimpleUploadedFile("card.jpg", image().read(), content_type="image/jpeg")
    member.save()
    member = identity.record_checks(member)
    assert sizes == [(40, 40)]  # the stored photo itself
    assert member.verification_status == "verified"


def test_anything_short_of_the_policy_goes_to_review(monkeypatch, kd_units):
    reads = read_as(monkeypatch, card_text(name="OBI\nADA"))
    stranger = identity.record_checks(complete(make_member(phone="08033330024"), kd_units[0]))
    card = stranger.verification_checks.get(kind="cardMatch")
    assert card.result == "pending" and stranger.verification_status == "pendingReview"
    assert card.detail == "The name given could not be found on the card photo."
    # The same name, VIN and photo give the same answer: the card is not read again.
    identity.record_checks(stranger)
    assert reads == [stranger.pk]

    read_as(monkeypatch, card_text(name="UMAR\nSANI"))  # another VIN on the card
    other_card = make_member(phone="08033330025", full_name="Sani Umar")
    other_card = complete(other_card, kd_units[0], vin="90F5B0E8D0295847099")
    detail = identity.record_checks(other_card).verification_checks.get(kind="cardMatch").detail
    assert detail == "The VIN given could not be found on the card photo."

    no_gender = make_member(phone="08033330026", full_name="Ada Obi")
    no_gender = complete(no_gender, kd_units[1], vin="90F5B0E8D0295847026")
    no_gender.gender = ""
    no_gender.save()
    read_as(monkeypatch, card_text(name="OBI\nADA", vin=no_gender.vin))
    no_gender = identity.record_checks(no_gender)
    results = dict(no_gender.verification_checks.values_list("kind", "result"))
    assert results["cardMatch"] == results["duplicates"] == "passed"
    assert no_gender.verification_status == "pendingReview"

    # The unit chosen from the list rather than read from the card.
    listed = make_member(phone="08033330027", full_name="Hauwa Sani")
    listed = complete(listed, kd_units[0], vin="90F5B0E8D0295847027")
    listed.polling_unit_from_list = True
    listed.save()
    read_as(monkeypatch, card_text(name="SANI\nHAUWA", vin=listed.vin))
    assert identity.record_checks(listed).verification_status == "pendingReview"

    # A coordinator's rejection stands, and nothing is read for it.
    rejected = make_member(phone="08033330028", full_name="Bala Idris")
    rejected = complete(rejected, kd_units[1], vin="90F5B0E8D0295847028")
    rejected.verification_status = "rejected"
    rejected.save()
    reads = read_as(monkeypatch, card_text(name="IDRIS\nBALA", vin=rejected.vin))
    assert identity.record_checks(rejected).verification_status == "rejected" and reads == []


def test_a_corrected_name_is_compared_with_the_card_again(admin, monkeypatch, kd_units):
    from apps.membership import services

    read_as(monkeypatch, card_text())
    member = identity.record_checks(complete(make_member(phone="08033330029"), kd_units[0]))
    assert member.verification_status == "verified"
    verifier = staff_with(
        admin, "08033330030", "Kaduna Verifier", [C.MANAGE_MEMBERSHIP, C.VERIFY_MEMBER_IDENTITY]
    )
    member = services.update_member(
        actor=verifier.user, member=member, reason="Wrong name.", full_name="Musa Bello Kaura"
    )
    assert member.verification_status == "pendingReview"
    assert not identity.card_compared(member)  # the comparison was of the old name

    reads = read_as(monkeypatch, card_text())  # the card has no Kaura
    member = identity.record_checks(member)
    assert reads == [member.pk] and member.verification_status == "pendingReview"
    read_as(monkeypatch, card_text(name="BELLO KAURA\nMUSA"))
    assert identity.record_checks(member).verification_status == "pendingReview"  # not re-read

    member.pvc_image = "members/pvc/rescanned.jpg"  # a new card photo is compared afresh
    member.save()
    assert identity.record_checks(member).verification_status == "verified"


def test_members_awaiting_a_decision_are_checked_again_by_command(monkeypatch, kd_units):
    from io import StringIO

    from django.core.management import call_command

    waiting = complete(make_member(phone="08033330031"), kd_units[0])
    waiting = identity.record_checks(waiting)  # no reader: the card was not read
    assert waiting.verification_status == "pendingReview"
    verified = make_member(phone="08033330032", verification_status="verified")

    reads = read_as(monkeypatch, card_text())
    events = AuditEvent.objects.count()
    out = StringIO()
    call_command("record_verification_checks", "--awaiting", "--dry-run", stdout=out)
    assert out.getvalue().splitlines() == [
        "1 member(s) selected; 1 await a decision.",
        "1 card photo(s) would be read; 1 of those members would be verified automatically "
        "if their card matches.",
        "Dry run: nothing was recorded and no card was read.",
    ]
    assert reads == [] and AuditEvent.objects.count() == events

    out = StringIO()
    call_command("record_verification_checks", "--awaiting", stdout=out)
    assert out.getvalue().splitlines() == [
        "Recorded checks for 1 member(s).",
        "Read 1 card photo(s); 0 could not be read this time.",
        "1 member(s) verified automatically.",
    ]
    assert reads == [waiting.pk]
    waiting.refresh_from_db()
    assert waiting.verification_status == "verified"
    verified.refresh_from_db()
    assert not verified.verification_checks.exists()  # a decided member is not selected


# --- review ----------------------------------------------------------------------


def test_coordinators_review_identities_in_their_area(admin):
    verifier = staff_with(admin, "08033330010", "Kaduna Verifier", [C.VERIFY_MEMBER_IDENTITY])
    member = checked_member("08033330011", name="Kaduna Member")
    client = client_for(verifier.user)
    url = f"/api/v1/members/{member.membership_number}/verification/"
    seen = client.get(url)
    assert seen.status_code == 200
    assert seen.json()["verification_status"] == "pendingReview"
    assert {check["kind"] for check in seen.json()["checks"]} == set(VerificationCheck.Kind.values)

    assert client.post(f"{url}approve/", {}, format="json").status_code == 400  # say what you did
    notes = {"notes": "Compared the PVC and the person at the ward office."}
    approved = client.post(f"{url}approve/", notes, format="json")
    assert approved.status_code == 200, approved.content
    assert approved.json()["verification_status"] == "verified"
    assert approved.json()["reviewed_by"] == verifier.user.access_id
    event = AuditEvent.objects.get(action="member.verified")
    assert event.reason == notes["notes"] and event.actor == verifier.user

    kano = checked_member("08033330012", lga="KN-AJINGI")
    kano_url = f"/api/v1/members/{kano.membership_number}/verification/"
    assert client.get(kano_url).status_code == 404
    own = f"/api/v1/members/{verifier.membership_number}/verification/reject/"
    assert client.post(own, {"reason": "x"}, format="json").status_code == 403

    no_pvc = make_member(phone="08033330013")
    identity.record_checks(no_pvc)
    refused = client.post(
        f"/api/v1/members/{no_pvc.membership_number}/verification/approve/",
        {"notes": "Looks fine"},
        format="json",
    )
    assert refused.status_code == 400  # a failed check is resolved first


def coordinator(access_id, role, **scope):
    """A client for a coordinator's account: a fixed role over one area."""
    user = User.objects.create_user(access_id, PASSWORD, full_name=access_id, role=role, **scope)
    return client_for(user)


def placed(member, unit):
    """The member, registered at `unit` instead."""
    member.scope_level = GeographyLevel.POLLING_UNIT
    member.polling_unit = unit
    member.normalize_scope()
    member.save()
    return member


def test_state_lga_and_ward_coordinators_verify_identities_in_their_area(kd_units):
    """Decided 13 October 2026, so the agents in their area can start."""
    ward = kd_units[0].ward

    def approve(client, member):
        url = f"/api/v1/members/{member.membership_number}/verification/approve/"
        return client.post(url, {"notes": "Saw the PVC and the person."}, format="json")

    state = coordinator(
        "SC-KD", Role.STATE_COORDINATOR, scope_level=GeographyLevel.STATE, state=ward.lga.state
    )
    lga = coordinator("LC-KD", Role.LGA_COORDINATOR, scope_level=GeographyLevel.LGA, lga=ward.lga)
    ward_coordinator = coordinator(
        "WC-KD", Role.WARD_COORDINATOR, scope_level=GeographyLevel.WARD, ward=ward
    )
    for n, client in enumerate((state, lga, ward_coordinator)):
        member = placed(checked_member(f"0803333005{n}", name=f"Ward Member {n}"), kd_units[0])
        approved = approve(client, member)
        assert approved.status_code == 200, approved.content
        assert approved.json()["verification_status"] == "verified"

    # Each only in their own area: the ward ends at the ward, the state at Kaduna.
    lga_level = checked_member("08033330060", name="Kaduna North Member")
    assert approve(ward_coordinator, lga_level).status_code == 404
    assert approve(lga, lga_level).status_code == 200
    kano = checked_member("08033330061", lga="KN-AJINGI")
    assert approve(state, kano).status_code == 404

    zonal = coordinator(
        "ZC-NW",
        Role.ZONAL_COORDINATOR,
        scope_level=GeographyLevel.GEOPOLITICAL_ZONE,
        zone=ward.lga.state.zone,
    )
    waiting = placed(checked_member("08033330062", name="Ward Member 3"), kd_units[0])
    assert approve(zonal, waiting).status_code == 403
    waiting.refresh_from_db()
    assert waiting.verification_status == "pendingReview"


def test_nobody_decides_the_identity_of_someone_whose_role_reaches_beyond_them(admin, kd_units):
    # Rejecting an identity stops every appointment at once, so a ward
    # coordinator cannot stop an LGA role holder who happens to live in the ward.
    ward = kd_units[0].ward
    organiser = placed(member_account("08033330070", name="LGA Organiser"), kd_units[0])
    role = custom_role(admin, "LGA Organiser", [C.SEND_BROADCAST], levels=("lga",))
    lga_area = {"level": "lga", "lgaId": "KD-KADUNA-NORTH"}
    assert appoint(admin, organiser, role, [lga_area]).status_code == 201

    url = f"/api/v1/members/{organiser.membership_number}/verification/"
    reason = {"reason": "The PVC belongs to someone else."}
    ward_coordinator = coordinator(
        "WC-KD", Role.WARD_COORDINATOR, scope_level=GeographyLevel.WARD, ward=ward
    )
    refused = ward_coordinator.post(f"{url}reject/", reason, format="json")
    assert refused.status_code == 403
    assert "role outside your area" in refused.json()["detail"]
    assert ward_coordinator.get(url).status_code == 403
    organiser.refresh_from_db()
    assert organiser.verification_status == "verified"
    assert C.SEND_BROADCAST in capabilities_for(fresh(organiser.user))

    # Whoever covers the whole LGA decides.
    lga = coordinator("LC-KD", Role.LGA_COORDINATOR, scope_level=GeographyLevel.LGA, lga=ward.lga)
    rejected = lga.post(f"{url}reject/", reason, format="json")
    assert rejected.status_code == 200, rejected.content
    assert C.SEND_BROADCAST not in capabilities_for(fresh(organiser.user))


def test_rejecting_an_identity_stops_its_appointments(admin):
    verifier = staff_with(admin, "08033330020", "Kaduna Verifier", [C.VERIFY_MEMBER_IDENTITY])
    holder = staff_with(admin, "08033330021", "Role Holder", [C.SEND_BROADCAST])
    assert C.SEND_BROADCAST in capabilities_for(holder.user)
    rejected = client_for(verifier.user).post(
        f"/api/v1/members/{holder.membership_number}/verification/reject/",
        {"reason": "The PVC belongs to someone else."},
        format="json",
    )
    assert rejected.json()["verification_status"] == "rejected"
    assert capabilities_for(fresh(holder.user)) == frozenset()
    assert RoleAssignment.objects.filter(member=holder).count() == 1  # history kept


# --- standing --------------------------------------------------------------------


def test_suspension_blocks_access_at_once_and_deactivation_ends_sign_in(admin):
    officer = staff_with(admin, "08033330030", "Member Officer", [C.SUSPEND_MEMBERS])
    holder = staff_with(admin, "08033330031", "Role Holder", [C.SEND_BROADCAST])
    refresh = RefreshToken.for_user(holder.user)
    client = client_for(officer.user)
    url = f"/api/v1/members/{holder.membership_number}/"

    assert client.post(f"{url}suspend/", {}, format="json").status_code == 400  # say why
    suspended = client.post(f"{url}suspend/", {"reason": "Under investigation"}, format="json")
    assert suspended.status_code == 200, suspended.content
    assert suspended.json()["membership_state"] == "suspended"
    assert capabilities_for(fresh(holder.user)) == frozenset()
    assert BlacklistedToken.objects.filter(token__jti=refresh["jti"]).exists()

    restored = client.post(f"{url}restore/", {"reason": "Cleared"}, format="json")
    assert restored.json()["membership_state"] == "active"
    assert C.SEND_BROADCAST in capabilities_for(fresh(holder.user))  # still a valid appointment

    deactivated = client.post(f"{url}deactivate/", {"reason": "Left the party"}, format="json")
    assert deactivated.json()["membership_state"] == "deactivated"
    holder.user.refresh_from_db()
    assert not holder.user.is_active
    assert RoleAssignment.objects.filter(member=holder).exists()
    client.post(f"{url}restore/", {}, format="json")
    holder.user.refresh_from_db()
    assert holder.user.is_active
    history = [event["action"] for event in client.get(f"{url}history/").json()]
    assert history == [
        "member.restored",
        "member.deactivated",
        "member.restored",
        "member.suspended",
    ]

    own = f"/api/v1/members/{officer.membership_number}/suspend/"
    assert client.post(own, {"reason": "x"}, format="json").status_code == 403
    kano = member_account("08033330032", lga="KN-AJINGI")
    kano_url = f"/api/v1/members/{kano.membership_number}/suspend/"
    assert client.post(kano_url, {"reason": "x"}, format="json").status_code == 404


def test_administrators_are_outside_ordinary_member_actions(admin):
    successor = member_account("08033330040", name="Successor")
    na_role = RoleDefinition.objects.get(code=Role.NATIONAL_ADMINISTRATOR)
    assert appoint(admin, successor, na_role, [{"level": "country"}]).status_code == 201
    officer = staff_with(
        admin,
        "08033330041",
        "Member Officer",
        [C.SUSPEND_MEMBERS, C.VERIFY_MEMBER_IDENTITY],
    )
    client = client_for(officer.user)
    url = f"/api/v1/members/{successor.membership_number}/"
    assert client.post(f"{url}suspend/", {"reason": "x"}, format="json").status_code == 403
    assert client.post(f"{url}deactivate/", {"reason": "x"}, format="json").status_code == 403
    reject = f"{url}verification/reject/"
    assert client.post(reject, {"reason": "x"}, format="json").status_code == 403
    assert engine.is_national_administrator(successor.user)


# --- personal details ------------------------------------------------------------


def test_personal_details_need_their_own_permission(admin):
    reader = staff_with(admin, "08033330050", "Member Reader", [C.REGISTER_MEMBERS])
    desk = staff_with(
        admin, "08033330051", "Member Desk", [C.REGISTER_MEMBERS, C.VIEW_MEMBER_SENSITIVE_DATA]
    )
    member = make_member(phone="08033330052", full_name="Private Person")
    member.photo = SimpleUploadedFile("selfie.jpg", image().read(), content_type="image/jpeg")
    member.save()
    VolunteerApplication.objects.create(member=member, interests=["campaign"])

    hidden = client_for(reader.user)
    row = hidden.get("/api/v1/members/", {"search": "Private"}).json()["results"][0]
    assert row["sensitive_visible"] is False
    assert row["phone_number"] is None and row["photo_url"] is None and row["vin"] is None
    photo = f"/api/v1/members/{member.membership_number}/photo/"
    assert hidden.get(photo).status_code == 403
    assert hidden.get("/api/v1/members/", {"phone": "08033330052"}).json()["count"] == 0
    offers = hidden.get("/api/v1/volunteers/").json()["results"]
    assert offers[0]["member_phone"] is None

    shown = client_for(desk.user)
    found = shown.get("/api/v1/members/", {"phone": "08033330052"}).json()
    assert found["count"] == 1 and found["results"][0]["phone_number"] == "+2348033330052"
    assert shown.get(photo).status_code == 200
    assert shown.get("/api/v1/volunteers/").json()["results"][0]["member_phone"]


# --- registered polling unit -----------------------------------------------------


def test_polling_unit_changes_are_reviewed_at_the_new_unit(admin, kd_units):
    member = member_account("08033330060", name="Moving Member")
    member.scope_level = GeographyLevel.POLLING_UNIT
    member.polling_unit = kd_units[0]
    member.polling_unit_from_list = True  # found by name when joining
    member.normalize_scope()
    member.save()
    held = custom_role(admin, "Kaduna Media", [C.SEND_BROADCAST])
    assert appoint(admin, member, held, [KD]).status_code == 201

    me = client_for(member.user)
    changes = "/api/v1/me/polling-unit/changes/"
    same = me.post(changes, {"delim": "18/08/01/001", "pvc_image": image()}, format="multipart")
    assert same.status_code == 400  # already registered there
    asked = me.post(
        changes,
        {"delim": "18/08/01/002", "pvc_image": image(), "reason": "INEC transfer"},
        format="multipart",
    )
    assert asked.status_code == 201, asked.content
    change = asked.json()
    assert change["status"] == "pending" and change["to_polling_unit"] == "18-08-01-002"
    again = me.post(changes, {"delim": "18/08/01/002", "pvc_image": image()}, format="multipart")
    assert again.status_code == 400  # one at a time

    kano = staff_with(
        admin, "08033330061", "Kano Reviewer", [C.REVIEW_POLLING_UNIT_CHANGES], areas=(KN,)
    )
    assert client_for(kano.user).get("/api/v1/polling-unit-changes/").json()["count"] == 0
    reviewer = staff_with(admin, "08033330062", "Kaduna Reviewer", [C.REVIEW_POLLING_UNIT_CHANGES])
    client = client_for(reviewer.user)
    url = f"/api/v1/polling-unit-changes/{change['id']}/"
    assert client.get(f"{url}card/").status_code == 200
    approved = client.post(f"{url}approve/", {"reason": "Card checked"}, format="json")
    assert approved.status_code == 200 and approved.json()["status"] == "approved"
    member.refresh_from_db()
    assert member.polling_unit_id == kd_units[1].pk
    assert not member.polling_unit_from_list  # it now comes from the updated card
    # The registered polling unit moved; the appointment did not.
    assert RoleAssignment.objects.get(member=member).status == "active"
    assert engine.allows(member.user, C.SEND_BROADCAST, ScopeRef.resolve("state", "KD"))

    back = me.post(changes, {"delim": "18/08/01/001", "pvc_image": image()}, format="multipart")
    withdrawn = me.post(f"{changes}{back.json()['id']}/withdraw/")
    assert withdrawn.json()["status"] == "withdrawn"
    third = me.post(changes, {"delim": "18/08/01/001", "pvc_image": image()}, format="multipart")
    reject = f"/api/v1/polling-unit-changes/{third.json()['id']}/reject/"
    assert client.post(reject, {}, format="json").status_code == 400  # say why
    rejected = client.post(reject, {"reason": "The card photo is unreadable."}, format="json")
    assert rejected.json()["status"] == "rejected"
    card = me.get("/api/v1/me/member/").json()
    assert card["membership_state"] == "active" and card["verification_status"] == "verified"


def test_edits_cannot_bypass_transfers_or_a_verified_identity(admin, kd_units):
    manager = staff_with(admin, "08033330070", "Member Manager", [C.MANAGE_MEMBERSHIP])
    member = checked_member("08033330071", name="Ada Obi")
    member.verification_status = "verified"
    member.save()
    client = client_for(manager.user)
    url = f"/api/v1/members/{member.membership_number}/"
    moved = client.patch(
        url, {"scope": {"level": "pollingUnit", "pollingUnitId": kd_units[1].code}}, format="json"
    )
    assert moved.status_code == 400  # a PVC holder moves through a change request

    # Sending a verified identity back to review takes the power to review it.
    assert client.patch(url, {"full_name": "Adaeze Obi"}, format="json").status_code == 403
    reviewer = staff_with(
        admin,
        "08033330072",
        "Member Reviewer",
        [C.MANAGE_MEMBERSHIP, C.VERIFY_MEMBER_IDENTITY],
    )
    trusted = client_for(reviewer.user)
    unexplained = trusted.patch(url, {"full_name": "Adaeze Obi"}, format="json")
    assert unexplained.status_code == 400  # and a reason
    renamed = trusted.patch(
        url, {"full_name": "Adaeze Obi", "reason": "Name as on the PVC"}, format="json"
    )
    assert renamed.status_code == 200, renamed.content
    member.refresh_from_db()
    assert member.verification_status == "pendingReview"  # the identity is checked again
    assert member.verified_by is None
    event = AuditEvent.objects.get(action="member.updated")
    assert event.changes == {"fields": ["full_name"], "moved": False, "back_to_review": True}
    assert event.reason == "Name as on the PVC"


def test_an_administrators_identity_is_not_edited_away(admin):
    successor = member_account("08033330073", name="Successor")
    na_role = RoleDefinition.objects.get(code=Role.NATIONAL_ADMINISTRATOR)
    assert appoint(admin, successor, na_role, [{"level": "country"}]).status_code == 201
    other = User.objects.create_user(
        "NA-2", PASSWORD, full_name="Second Admin", role=Role.NATIONAL_ADMINISTRATOR
    )
    renamed = client_for(other).patch(
        f"/api/v1/members/{successor.membership_number}/",
        {"full_name": "Someone Else", "reason": "x"},
        format="json",
    )
    assert renamed.status_code == 403
    assert engine.is_national_administrator(fresh(successor.user))


def test_nobody_acts_on_whoever_appointed_them(admin):
    boss_role = custom_role(admin, "Kaduna Membership Lead", [C.ASSIGN_ROLES, C.SUSPEND_MEMBERS])
    boss = member_account("08033330074", name="Boss")
    assert appoint(admin, boss, boss_role, [KD]).status_code == 201
    deputy_role = custom_role(admin, "Kaduna Membership Officer", [C.SUSPEND_MEMBERS])
    deputy = member_account("08033330075", name="Deputy")
    assert appoint(boss.user, deputy, deputy_role, [KD]).status_code == 201
    suspend = client_for(deputy.user).post(
        f"/api/v1/members/{boss.membership_number}/suspend/", {"reason": "x"}, format="json"
    )
    assert suspend.status_code == 403
    assert fresh(boss.user).member.membership_state == "active"


def test_suspended_agents_share_no_location_and_ended_memberships_stay_ended(kd_units, monkeypatch):
    from apps.membership import registration
    from apps.membership.serializers import CardVerificationSerializer
    from apps.tracking import services as tracking

    agent = User.objects.create_user(
        "AG-KD-1",
        PASSWORD,
        full_name="Agent",
        role=Role.POLLING_UNIT_AGENT,
        scope_level=GeographyLevel.POLLING_UNIT,
        polling_unit=kd_units[0],
    )
    member = make_member(phone="08033330076", full_name="Agent")
    member.user = agent
    member.membership_state = "suspended"
    member.save()
    gps = {"latitude": 10.5, "longitude": 7.4, "accuracy_m": 8, "captured_at": timezone.now()}
    with pytest.raises(PermissionDenied):
        tracking.check_in(agent=fresh(agent), gps=gps)
    with pytest.raises(PermissionDenied):
        tracking.start_duty(user=fresh(agent))

    ended = make_member(phone="08033330077", full_name="Former Member")
    ended.membership_state = "deactivated"
    ended.save()
    monkeypatch.setattr(registration, "consume_code", lambda *args: None)  # a right code
    with pytest.raises(ValidationError) as ended_error:
        registration.set_pin(phone_number="08033330077", code="000000", pin="482915")
    assert "has ended" in str(ended_error.value)
    assert not User.objects.filter(member__phone_number="+2348033330077").exists()
    card = CardVerificationSerializer(ended, context={"token": "x"}).data
    assert card["valid"] is False and card["membership_state"] == "deactivated"


def test_member_history_is_for_those_who_decide(admin):
    reader = staff_with(admin, "08033330078", "Member Reader", [C.REGISTER_MEMBERS])
    member = checked_member("08033330079", name="Kaduna Member")
    url = f"/api/v1/members/{member.membership_number}/history/"
    assert client_for(reader.user).get(url).status_code == 403
    officer = staff_with(admin, "08033330080", "Member Officer", [C.SUSPEND_MEMBERS])
    assert client_for(officer.user).get(url).status_code == 200


def test_suspended_members_cannot_volunteer_and_see_their_own_phone(admin):
    member = member_account("08033330081", name="Volunteer")
    me = client_for(member.user)
    offered = me.post("/api/v1/me/volunteer/", {"interests": ["campaign"]}, format="json")
    assert offered.status_code == 201
    assert offered.json()["member_phone"] == "+2348033330081"  # their own
    me.post(f"/api/v1/me/volunteer/{offered.json()['id']}/withdraw/")
    member.membership_state = "suspended"
    member.save()
    again = client_for(fresh(member.user)).post(
        "/api/v1/me/volunteer/", {"interests": ["campaign"]}, format="json"
    )
    assert again.status_code == 400


def test_agent_phone_numbers_need_the_personal_details_permission(admin, kd_units):
    accreditor = staff_with(admin, "08033330080", "Agent Desk", [C.ACCREDIT_AGENTS])
    member = checked_member("08033330081", name="Agent To Be")
    created = client_for(accreditor.user).post(
        "/api/v1/agents/",
        {
            "member": member.membership_number,
            "role": "pollingUnitAgent",
            "scope": {"level": "pollingUnit", "pollingUnitId": kd_units[0].code},
            "registered_phone_number": "08033330082",
        },
        format="json",
    )
    assert created.status_code == 201, created.content
    assert created.json()["registered_phone_number"] is None
    trusted = staff_with(
        admin, "08033330083", "Agent Lead", [C.ACCREDIT_AGENTS, C.VIEW_MEMBER_SENSITIVE_DATA]
    )
    agent = client_for(trusted.user).get(f"/api/v1/agents/{created.json()['agent_id']}/").json()
    assert agent["registered_phone_number"] == "+2348033330082"

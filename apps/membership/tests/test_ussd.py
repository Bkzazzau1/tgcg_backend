"""Short USSD joining and durable, private progress across provider sessions."""

import io
import logging
from datetime import timedelta

import pytest
from django.contrib.auth.hashers import check_password
from django.core.management import call_command
from django.test import Client
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.core.choices import GeographyLevel, RegistrationChannel, Role, VerificationStatus
from apps.geography.models import State
from apps.geography.polling_units import load_polling_units
from apps.membership import ussd
from apps.membership.models import Member, UssdRegistrationDraft, UssdSession
from apps.membership.serializers import (
    AssistedRegistrationSerializer,
    MemberSerializer,
    SelfRegistrationSerializer,
)
from conftest import PASSWORD

pytestmark = pytest.mark.django_db

TOKEN = "t" * 40
CODE = "*384*1234#"
PHONE = "+2348031234567"
OTHER_PHONE = "+2348039998888"
PIN = "482915"
VIN = "90F5B0E8D0295847263"
OTHER_VIN = "90F5B0E8D0295847264"
UNIT = "18-23-01-001"
JOIN = ["1", VIN, UNIT, "1"]


@pytest.fixture(autouse=True)
def setup(settings):
    settings.USSD_CALLBACK_TOKEN = TOKEN
    settings.USSD_SERVICE_CODE = CODE
    settings.USSD_DRAFT_TTL_DAYS = 30
    load_polling_units(lga_codes=["KD-ZARIA"])


def post(text="", *, session="ATUid_1", phone=PHONE, token=TOKEN, code=CODE):
    return Client().post(
        f"/api/v1/ussd/{token}/",
        {"sessionId": session, "serviceCode": code, "phoneNumber": phone, "text": text},
    )


def dial(*answers, session="ATUid_1", phone=PHONE):
    response = post("*".join(answers), session=session, phone=phone)
    assert response.status_code == 200
    assert response["Content-Type"].startswith("text/plain")
    body = response.content.decode()
    assert body[:4] in ("CON ", "END ")
    assert len(body) - 4 <= ussd.SCREEN_LIMIT and body.isascii(), body
    return body


def draft(phone=PHONE):
    return UssdRegistrationDraft.objects.get(phone_number=phone)


def join(*, phone=PHONE, vin=VIN, session="ATUid_join"):
    answers = ["1", vin, UNIT, "1"]
    for length in range(1, len(answers) + 1):
        response = dial(*answers[:length], session=session, phone=phone)
    assert response.startswith("END ")
    return Member.objects.get(phone_number=phone)


def complete_profile(*, session="ATUid_profile", name="musa BELLO", pin=PIN):
    answers = ["1", name, pin, pin]
    for length in range(1, len(answers) + 1):
        response = dial(*answers[:length], session=session)
    assert response.startswith("END ")
    return Member.objects.get(phone_number=PHONE)


def progress_snapshot(phone=PHONE):
    saved = draft(phone)
    return (
        saved.pk, saved.kind, saved.step, saved.vin, saved.polling_unit_id,
        saved.unit_parts, saved.full_name, saved.pin_hash, saved.active_session_key,
    )


def test_opening_and_membership_lookup_do_not_store_empty_progress():
    assert dial().startswith("CON Welcome to TGCG")
    assert dial("2").startswith("END ")
    assert not UssdRegistrationDraft.objects.exists()
    assert not UssdSession.objects.exists()
    assert not Member.objects.exists()


def test_quick_join_requires_only_vin_unit_and_agreement():
    assert dial("1").startswith("CON ")
    assert draft().step == "vin"
    assert "VIN" in dial("1")
    assert "19" in dial("1")
    assert dial("1", VIN).startswith("CON ")
    assert draft().step == "unit" and draft().vin == VIN
    assert "Polling unit" in dial("1", VIN, UNIT)
    assert draft().step == "confirm"
    assert not Member.objects.exists()

    done = dial(*JOIN)
    member = Member.objects.get(phone_number=PHONE)
    assert done.startswith("END ") and member.membership_number in done
    assert member.registration_channel == RegistrationChannel.USSD
    assert member.full_name == ""
    assert member.user_id is None and not User.objects.exists()
    assert member.vin == VIN
    assert member.polling_unit.code == UNIT and member.lga.code == "KD-ZARIA"
    assert member.phone_verified_at and member.consent_at
    assert not member.photo and not member.pvc_image
    assert member.verification_status == VerificationStatus.PENDING_REVIEW
    assert not UssdRegistrationDraft.objects.exists()
    checks = {check.kind: check.result for check in member.verification_checks.all()}
    assert checks["phoneVerified"] == "passed"
    assert checks["pvcCaptured"] == "pending"
    assert checks["selfieCaptured"] == "failed"


@pytest.mark.parametrize("unit", ["18-23-01-001", "18/23/01/001", "18*23*01*001"])
def test_full_polling_unit_code_formats(unit):
    dial("1")
    dial("1", VIN)
    assert dial("1", VIN, unit).startswith("CON ")
    assert draft().step == "confirm"
    done = dial("1", VIN, unit, "1")
    assert done.startswith("END ")
    assert Member.objects.get().polling_unit.code == UNIT


@pytest.mark.parametrize(
    ("answers", "step", "remaining"),
    [
        (["1"], "vin", [VIN, UNIT, "1"]),
        (["1", VIN], "unit", [UNIT, "1"]),
        (["1", VIN, "18"], "unit", ["23", "01", "001", "1"]),
        (["1", VIN, "18", "23"], "unit", ["01", "001", "1"]),
        (["1", VIN, "18", "23", "01"], "unit", ["001", "1"]),
        (["1", VIN, UNIT], "confirm", ["1"]),
    ],
)
def test_redial_resumes_every_join_checkpoint(answers, step, remaining):
    for length in range(1, len(answers) + 1):
        dial(*answers[:length], session="expired")
    before = draft()
    assert before.step == step
    saved_pk = before.pk
    saved_vin = before.vin
    saved_parts = before.unit_parts

    screen = dial("1", session="redial")
    assert screen.startswith("CON ")
    resumed = draft()
    assert resumed.pk == saved_pk and resumed.step == step
    assert resumed.vin == saved_vin and resumed.unit_parts == saved_parts
    assert UssdSession.objects.get(provider_session_id="redial").pk == resumed.active_session_key

    for length in range(1, len(remaining) + 1):
        screen = dial("1", *remaining[:length], session="redial")
    assert screen.startswith("END ")
    assert Member.objects.get().vin == VIN
    assert Member.objects.get().polling_unit.code == UNIT


def test_local_and_international_phone_spellings_resume_the_same_draft():
    dial("1", phone="08031234567", session="local")
    dial("1", VIN, phone="08031234567", session="local")
    first = draft()
    dial("1", phone=PHONE, session="international")
    assert UssdRegistrationDraft.objects.count() == 1
    assert draft().pk == first.pk and draft().step == "unit"


def test_other_phones_cannot_resume_or_change_saved_answers():
    dial("1")
    dial("1", VIN)
    original = progress_snapshot()
    dial("1", phone=OTHER_PHONE, session="other")
    assert draft(OTHER_PHONE).step == "vin" and draft(OTHER_PHONE).vin == ""
    dial("1", OTHER_VIN, phone=OTHER_PHONE, session="other")
    assert draft(OTHER_PHONE).vin == OTHER_VIN
    assert progress_snapshot() == original
    assert UssdRegistrationDraft.objects.count() == 2


def test_callback_retries_and_shorter_prefix_do_not_consume_answers_again():
    dial("1")
    newest = dial("1", VIN)
    before = progress_snapshot()
    record = UssdSession.objects.get(provider_session_id="ATUid_1")
    before_count = record.answer_count
    assert dial("1", VIN) == newest
    assert dial("1") == newest
    record.refresh_from_db()
    assert record.answer_count == before_count
    assert progress_snapshot() == before
    assert not Member.objects.exists()


def test_changed_prefix_is_refused_without_overwriting_progress():
    dial("1")
    dial("1", VIN)
    before = progress_snapshot()
    assert dial("1", OTHER_VIN).startswith("END ")
    assert progress_snapshot() == before
    assert not Member.objects.exists()


def test_old_provider_session_cannot_mutate_a_resumed_draft():
    dial("1", session="old")
    dial("1", VIN, session="old")
    dial("1", session="new")
    before = progress_snapshot()
    assert dial("1", VIN, UNIT, session="old").startswith("END ")
    assert progress_snapshot() == before
    assert draft().active_session_key == UssdSession.objects.get(provider_session_id="new").pk


def test_old_flow_or_unknown_nonempty_session_is_not_reinterpreted():
    assert dial("1", "Musa Bello", "18*23*01*001", session="unknown").startswith("END ")
    assert dial("7", session="unknown2").startswith("END ")
    assert not Member.objects.exists()
    assert not UssdRegistrationDraft.objects.exists()
    assert not UssdSession.objects.exists()


def test_final_callback_retries_return_the_same_membership_once():
    member = join(session="final")
    first = dial(*JOIN, session="final")
    assert dial(*JOIN, session="final") == first
    assert Member.objects.count() == 1 and not User.objects.exists()
    assert Member.objects.get().pk == member.pk
    assert not UssdRegistrationDraft.objects.exists()


def test_required_vin_is_normalized_and_invalid_values_can_be_corrected():
    dial("1")
    assert dial("1", "ABC").startswith("CON ")
    assert draft().step == "vin" and draft().vin == ""
    dial("1", "ABC", VIN.lower())
    assert draft().vin == VIN and draft().step == "unit"
    dial("1", "ABC", VIN.lower(), UNIT)
    dial("1", "ABC", VIN.lower(), UNIT, "1")
    assert Member.objects.get().vin == VIN


def test_wrong_unit_and_rejected_confirmation_can_be_corrected():
    dial("1")
    dial("1", VIN)
    assert dial("1", VIN, "18-23-01-999").startswith("CON ")
    assert draft().step == "unit" and draft().polling_unit_id is None
    dial("1", VIN, "18-23-01-999", UNIT)
    assert draft().step == "confirm"
    dial("1", VIN, "18-23-01-999", UNIT, "2")
    assert draft().step == "unit" and draft().polling_unit_id is None
    assert draft().vin == VIN
    dial("1", VIN, "18-23-01-999", UNIT, "2", UNIT)
    dial("1", VIN, "18-23-01-999", UNIT, "2", UNIT, "1")
    assert Member.objects.get().polling_unit.code == UNIT


@pytest.mark.parametrize("checkpoint", [["1"], ["1", VIN], ["1", VIN, UNIT]])
def test_zero_cancels_unfinished_join_at_any_stage(checkpoint):
    for length in range(1, len(checkpoint) + 1):
        dial(*checkpoint[:length])
    assert dial(*checkpoint, "0").startswith("END ")
    assert not UssdRegistrationDraft.objects.exists()
    assert not Member.objects.exists()
    assert not User.objects.exists()
    dial("1", session="start-over")
    assert draft().step == "vin"


def test_duplicate_vin_and_staff_phone_cannot_join():
    join()
    dial("1", phone=OTHER_PHONE, session="duplicate")
    assert dial("1", VIN, phone=OTHER_PHONE, session="duplicate").startswith("CON ")
    assert draft(OTHER_PHONE).step == "vin"
    assert Member.objects.count() == 1

    User.objects.create_user(
        "SC-KD", PASSWORD, full_name="State Coordinator", phone_number="+2348035550000",
        role=Role.STATE_COORDINATOR, scope_level=GeographyLevel.STATE,
        state=State.objects.get(code="KD"),
    )
    assert dial("1", phone="+2348035550000", session="staff").startswith("END ")
    assert not UssdRegistrationDraft.objects.filter(phone_number="+2348035550000").exists()


def test_returning_member_can_complete_profile_and_sign_in():
    member = join()
    assert dial("1", session="profile").startswith("CON ")
    assert draft().kind == "profile" and draft().step == "name"
    dial("1", "musa BELLO", session="profile")
    assert draft().full_name == "Musa Bello" and draft().step == "pin"
    dial("1", "musa BELLO", PIN, session="profile")
    assert draft().step == "pin_confirm"
    assert check_password(PIN, draft().pin_hash)
    assert draft().pin_hash != PIN
    assert Member.objects.get().user_id is None

    done = dial("1", "musa BELLO", PIN, PIN, session="profile")
    member.refresh_from_db()
    assert done.startswith("END ") and member.membership_number in done
    assert member.full_name == "Musa Bello" and member.user.role == Role.MEMBER
    assert member.user.check_password(PIN)
    assert not UssdRegistrationDraft.objects.exists()
    login = APIClient().post(
        "/api/v1/auth/login/", {"username": "08031234567", "password": PIN}, format="json"
    )
    assert login.status_code == 200 and login.json()["user"]["role"] == "member"


@pytest.mark.parametrize(
    ("answers", "step", "remaining"),
    [
        (["1"], "name", ["Musa Bello", PIN, PIN]),
        (["1", "Musa Bello"], "pin", [PIN, PIN]),
        (["1", "Musa Bello", PIN], "pin_confirm", [PIN]),
    ],
)
def test_profile_progress_resumes_in_a_new_session(answers, step, remaining):
    join()
    for length in range(1, len(answers) + 1):
        dial(*answers[:length], session="profile-old")
    old = draft()
    assert old.step == step
    saved_name, saved_hash = old.full_name, old.pin_hash
    dial("1", session="profile-new")
    assert draft().step == step
    assert (draft().full_name, draft().pin_hash) == (saved_name, saved_hash)
    for length in range(1, len(remaining) + 1):
        done = dial("1", *remaining[:length], session="profile-new")
    assert done.startswith("END ")
    assert Member.objects.get().user.check_password(PIN)


def test_profile_rejects_short_name_weak_pin_and_mismatch():
    join()
    dial("1", session="profile")
    dial("1", "M", session="profile")
    assert draft().step == "name"
    dial("1", "M", "Musa Bello", session="profile")
    assert draft().step == "pin"
    dial("1", "M", "Musa Bello", "123456", session="profile")
    assert draft().step == "pin" and draft().pin_hash == ""
    dial("1", "M", "Musa Bello", "123456", PIN, session="profile")
    dial("1", "M", "Musa Bello", "123456", PIN, "482916", session="profile")
    assert draft().step == "pin" and draft().pin_hash == ""
    assert Member.objects.get().user_id is None


def test_cancelling_profile_never_deletes_membership():
    member = join()
    dial("1", session="profile")
    dial("1", "Musa Bello", session="profile")
    assert dial("1", "Musa Bello", "0", session="profile").startswith("END ")
    assert Member.objects.get().pk == member.pk
    assert Member.objects.get().user_id is None
    assert not UssdRegistrationDraft.objects.exists()


def test_completed_profile_retries_and_redials_do_not_reset_password():
    join()
    complete_profile()
    member = Member.objects.get()
    saved_user, saved_hash = member.user_id, member.user.password
    final = dial("1", "musa BELLO", PIN, PIN, session="ATUid_profile")
    assert dial("1", "musa BELLO", PIN, PIN, session="ATUid_profile") == final
    assert dial("1", session="already-complete").startswith("END ")
    member.refresh_from_db()
    assert Member.objects.count() == 1 and User.objects.count() == 1
    assert member.user_id == saved_user and member.user.password == saved_hash
    assert not UssdRegistrationDraft.objects.exists()


@pytest.mark.parametrize(
    "serializer_type",
    [MemberSerializer, SelfRegistrationSerializer, AssistedRegistrationSerializer],
)
def test_blank_name_remains_invalid_for_normal_api_registration(serializer_type):
    serializer = serializer_type(data={"full_name": ""})
    assert not serializer.is_valid()
    assert "full_name" in serializer.errors


def test_raw_pin_and_cumulative_answers_are_never_stored():
    join()
    dial("1", session="profile")
    dial("1", "Musa Bello", session="profile")
    dial("1", "Musa Bello", PIN, session="profile")
    assert check_password(PIN, draft().pin_hash)
    raw_answers = f"1*Musa Bello*{PIN}"
    for model in (UssdRegistrationDraft, UssdSession):
        for saved in model.objects.all():
            for field in model._meta.concrete_fields:
                value = getattr(saved, field.attname)
                if isinstance(value, str):
                    assert PIN not in value
                    assert raw_answers not in value
    assert UssdSession.objects.get(provider_session_id="profile").prefix_digest


def test_membership_lookup_works_without_a_profile():
    member = join()
    screen = dial("2", session="lookup")
    assert screen.startswith("END ") and member.membership_number in screen
    assert "active" in screen.lower()
    assert not UssdRegistrationDraft.objects.exists()


def test_callback_access_and_provider_identity_guards(settings):
    assert post(token="wrong" * 8).status_code == 404
    assert Client().get(f"/api/v1/ussd/{TOKEN}/").status_code == 405
    assert post(code="*123#").content.startswith(b"END ")
    assert post(session="").content.startswith(b"END ")
    assert post(phone="+447700900123").content.startswith(b"END ")
    settings.USSD_CALLBACK_TOKEN = ""
    assert post().status_code == 404
    assert not UssdRegistrationDraft.objects.exists()
    assert not UssdSession.objects.exists()


def test_nothing_typed_reaches_application_logs(caplog, monkeypatch):
    caplog.set_level(logging.DEBUG)
    join()
    complete_profile()
    assert PIN not in caplog.text and VIN not in caplog.text

    def broken(**kwargs):
        raise RuntimeError(f"failed for {kwargs['text']}")

    monkeypatch.setattr(ussd, "respond", broken)
    screen = dial("1", "Musa Bello", PIN, session="broken")
    assert screen.startswith("END ")
    assert "USSD request failed: RuntimeError" in caplog.text
    assert PIN not in caplog.text and "8031234567" not in caplog.text


def test_cleanup_removes_only_stale_progress_and_preserves_accounts():
    member = join()
    member = complete_profile()
    old_time = timezone.now() - timedelta(days=31)
    recent_time = timezone.now() - timedelta(days=29)
    expired = UssdRegistrationDraft.objects.create(
        phone_number=OTHER_PHONE, kind="join", step="unit", vin=OTHER_VIN
    )
    recent = UssdRegistrationDraft.objects.create(
        phone_number="+2348039998887", kind="join", step="vin"
    )
    UssdRegistrationDraft.objects.filter(pk=expired.pk).update(updated_at=old_time)
    UssdRegistrationDraft.objects.filter(pk=recent.pk).update(updated_at=recent_time)
    stale_session = UssdSession.objects.create(
        phone_number=OTHER_PHONE, service_code=CODE, provider_session_id="stale", draft=expired
    )
    recent_session = UssdSession.objects.create(
        phone_number=recent.phone_number, service_code=CODE,
        provider_session_id="recent", draft=recent,
    )
    UssdSession.objects.filter(pk=stale_session.pk).update(updated_at=old_time)
    UssdSession.objects.filter(pk=recent_session.pk).update(updated_at=recent_time)

    output = io.StringIO()
    call_command("purge_ussd_progress", stdout=output)
    assert not UssdRegistrationDraft.objects.filter(pk=expired.pk).exists()
    assert not UssdSession.objects.filter(pk=stale_session.pk).exists()
    assert UssdRegistrationDraft.objects.filter(pk=recent.pk).exists()
    assert UssdSession.objects.filter(pk=recent_session.pk).exists()
    assert Member.objects.get().pk == member.pk
    assert User.objects.get().pk == member.user_id
    assert User.objects.get().check_password(PIN)
    assert PIN not in output.getvalue() and OTHER_PHONE not in output.getvalue()


def test_cleanup_uses_configured_retention(settings):
    settings.USSD_DRAFT_TTL_DAYS = 7
    saved = UssdRegistrationDraft.objects.create(
        phone_number=PHONE, kind="join", step="vin"
    )
    UssdRegistrationDraft.objects.filter(pk=saved.pk).update(
        updated_at=timezone.now() - timedelta(days=8)
    )
    call_command("purge_ussd_progress", stdout=io.StringIO())
    assert not UssdRegistrationDraft.objects.exists()


@pytest.mark.parametrize(
    ("valid_parts", "invalid", "corrected"),
    [
        ([], "99", "18"),
        (["18"], "77", "23"),
        (["18", "23"], "99", "01"),
        (["18", "23", "01"], "999", "001"),
        (["18"], "letters", "23"),
        (["18"], "00", "23"),
    ],
)
def test_invalid_partial_unit_number_keeps_validated_parts(valid_parts, invalid, corrected):
    dial("1")
    dial("1", VIN)
    answers = ["1", VIN]
    for part in valid_parts:
        answers.append(part)
        dial(*answers)
    before = list(draft().unit_parts)
    answers.append(invalid)
    assert dial(*answers).startswith("CON ")
    assert draft().unit_parts == before and draft().step == "unit"
    answers.append(corrected)
    assert dial(*answers).startswith("CON ")
    assert len(draft().unit_parts) == len(before) + 1
    if len(valid_parts) == 3:
        assert draft().step == "confirm"
    else:
        assert draft().step == "unit"


def test_star_separated_unit_cannot_include_agreement_before_notice_is_shown():
    dial("1")
    dial("1", VIN)
    # This is one answer to the unit question, not an answer to the notice.
    entered = "18*23*01*001*1"
    screen = dial("1", VIN, entered)
    assert screen.startswith("CON ") and "Agree" in screen
    assert draft().step == "confirm"
    assert not Member.objects.exists()
    # A later callback supplies consent after the notice was displayed.
    assert dial("1", VIN, entered, "1").startswith("END ")
    assert Member.objects.count() == 1


def test_vin_answer_cannot_silently_answer_unshown_unit_and_consent_questions():
    dial("1")
    entered = f"{VIN}*18*23*01*001*1"
    assert dial("1", entered).startswith("CON ")
    assert draft().vin == VIN and draft().step == "unit"
    assert draft().polling_unit_id is None
    assert not Member.objects.exists()


def test_name_answer_cannot_silently_choose_and_confirm_an_app_pin():
    join()
    dial("1", session="profile")
    assert dial("1", f"Musa Bello*{PIN}*{PIN}", session="profile").startswith("CON ")
    assert draft().full_name == "Musa Bello" and draft().step == "pin"
    assert draft().pin_hash == ""
    assert Member.objects.get().user_id is None


def test_long_geography_names_and_confirmation_error_fit_one_screen():
    from apps.geography.models import PollingUnit

    unit = PollingUnit.objects.select_related("ward__lga").get(code=UNIT)
    for place in (unit, unit.ward, unit.ward.lga):
        limit = place._meta.get_field("name").max_length
        place.name = "W" * limit
        place.save(update_fields=["name"])
    dial("1")
    dial("1", VIN)
    normal = dial("1", VIN, UNIT)
    assert "1. Agree" in normal and "0. Cancel" in normal
    with_error = dial("1", VIN, UNIT, "9")
    assert "1. Agree" in with_error and "0. Cancel" in with_error
    assert draft().step == "confirm"
    assert not Member.objects.exists()


def test_expired_draft_refuses_old_callback_and_starts_fresh_on_redial():
    dial("1", session="expired")
    dial("1", VIN, session="expired")
    previous_pk = draft().pk
    UssdRegistrationDraft.objects.filter(pk=previous_pk).update(
        updated_at=timezone.now() - timedelta(days=31)
    )
    assert dial("1", VIN, UNIT, session="expired").startswith("END ")
    assert not Member.objects.exists()
    dial("1", session="fresh")
    assert draft().pk != previous_pk
    assert draft().step == "vin" and draft().vin == ""


def test_cleanup_uses_last_activity_and_detaches_recent_session_from_stale_draft():
    old_time = timezone.now() - timedelta(days=31)
    active = UssdRegistrationDraft.objects.create(
        phone_number=PHONE, kind="join", step="vin"
    )
    expired = UssdRegistrationDraft.objects.create(
        phone_number=OTHER_PHONE, kind="join", step="unit", vin=OTHER_VIN
    )
    UssdRegistrationDraft.objects.filter(pk=active.pk).update(created_at=old_time)
    UssdRegistrationDraft.objects.filter(pk=expired.pk).update(updated_at=old_time)
    recent_session = UssdSession.objects.create(
        phone_number=OTHER_PHONE, service_code=CODE,
        provider_session_id="recent-with-old-draft", draft=expired,
    )
    call_command("purge_ussd_progress", stdout=io.StringIO())
    assert UssdRegistrationDraft.objects.filter(pk=active.pk).exists()
    assert not UssdRegistrationDraft.objects.filter(pk=expired.pk).exists()
    recent_session.refresh_from_db()
    assert recent_session.draft_id is None


@pytest.mark.parametrize("days", [0, -1, "invalid"])
def test_cleanup_refuses_invalid_retention_without_deleting_progress(settings, days):
    from django.core.management.base import CommandError

    settings.USSD_DRAFT_TTL_DAYS = days
    saved = UssdRegistrationDraft.objects.create(
        phone_number=PHONE, kind="join", step="vin"
    )
    with pytest.raises(CommandError):
        call_command("purge_ussd_progress", stdout=io.StringIO())
    assert UssdRegistrationDraft.objects.filter(pk=saved.pk).exists()


@pytest.mark.parametrize("kind", ["join", "profile"])
def test_new_dial_zero_cancels_phone_progress_without_joining_or_deleting_member(kind):
    if kind == "profile":
        existing = join()
        dial("1", session="working")
        dial("1", "Musa Bello", session="working")
    else:
        existing = None
        dial("1", session="working")
        dial("1", VIN, session="working")
    assert UssdRegistrationDraft.objects.exists()
    assert dial("0", session="fresh-cancel").startswith("END ")
    assert not UssdRegistrationDraft.objects.exists()
    assert not UssdSession.objects.filter(provider_session_id="fresh-cancel").exists()
    assert Member.objects.count() == (1 if existing else 0)
    if existing:
        assert Member.objects.get().pk == existing.pk
        assert Member.objects.get().user_id is None
    assert not User.objects.exists()
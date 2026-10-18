"""Scoped analysis and drill-downs share exact boundaries and profile predicates."""

from datetime import UTC, datetime, timedelta

import pytest

from apps.access.tests.test_access import appoint, client_for, custom_role, member_account
from apps.accounts.models import User
from apps.core.choices import GeographyLevel, RegistrationChannel, Role, VerificationStatus
from apps.core.permissions import Capability
from apps.geography.models import Lga, PollingUnit, Ward
from apps.membership import analysis
from apps.membership.models import Member
from conftest import PASSWORD, make_member

pytestmark = pytest.mark.django_db
ANALYSIS = "/api/v1/members/analysis/"
MEMBERS = "/api/v1/members/"
NOW = datetime(2026, 10, 18, 12, tzinfo=UTC)


@pytest.fixture
def admin():
    return User.objects.create_user(
        "ANALYSIS-NA", PASSWORD, full_name="National Admin", role=Role.NATIONAL_ADMINISTRATOR
    )


@pytest.fixture
def frozen_time(monkeypatch):
    monkeypatch.setattr(analysis.timezone, "now", lambda: NOW)
    return NOW


def _analysis(client, **params):
    response = client.get(ANALYSIS, params)
    assert response.status_code == 200, response.content
    return response.json()


def _rows(client, **params):
    response = client.get(MEMBERS, params)
    assert response.status_code == 200, response.content
    assert response.json()["count"] <= 25
    return response.json()["results"]


def test_adjacent_registration_windows_keep_exact_boundaries(admin, frozen_time):
    offsets = [0, 7, 14, 30, 60]
    for index, days in enumerate(offsets):
        member = make_member(phone=f"080355510{index:02d}")
        Member.objects.filter(pk=member.pk).update(created_at=NOW - timedelta(days=days))
    # Just below each boundary belongs to the older window.
    for index, days in enumerate([7, 14, 30, 60], start=5):
        member = make_member(phone=f"080355510{index:02d}")
        Member.objects.filter(pk=member.pk).update(
            created_at=NOW - timedelta(days=days, microseconds=1)
        )
    counts = _analysis(client_for(admin))["members"]
    assert counts["total"] == 9
    assert counts["new_7_days"] == 2
    assert counts["previous_7_days"] == 2  # [14 days ago, 7 days ago)
    assert counts["new_30_days"] == 6
    assert counts["previous_30_days"] == 2  # [60 days ago, 30 days ago)


def test_account_partitions_and_filters_use_recorded_login_boundaries(admin, frozen_time):
    no_account = make_member(phone="08035552000")
    never = member_account("08035552001")
    recent7 = member_account("08035552002")
    recent30 = member_account("08035552003")
    older = member_account("08035552004")
    User.objects.filter(pk=recent7.user_id).update(last_login=NOW - timedelta(days=7))
    User.objects.filter(pk=recent30.user_id).update(last_login=NOW - timedelta(days=30))
    User.objects.filter(pk=older.user_id).update(
        last_login=NOW - timedelta(days=30, microseconds=1)
    )
    client = client_for(admin)
    counts = _analysis(client)["members"]
    assert counts["accounts"] == {
        "without_account": 1,
        "never_signed_in": 1,
        "last_sign_in_7_days": 1,
        "last_sign_in_30_days": 2,
        "last_sign_in_older": 1,
    }
    account = counts["accounts"]
    assert (
        sum(
            account[key]
            for key in (
                "without_account",
                "never_signed_in",
                "last_sign_in_30_days",
                "last_sign_in_older",
            )
        )
        == counts["total"]
    )
    expected = {
        "noAppAccount": {no_account.membership_number},
        "accountNeverSignedIn": {never.membership_number},
        "signedIn7Days": {recent7.membership_number},
        "signedIn30Days": {recent7.membership_number, recent30.membership_number},
        "lastSignInOlder": {older.membership_number},
        "neverSignedIn": {no_account.membership_number, never.membership_number},
    }
    for show, numbers in expected.items():
        assert {row["membership_number"] for row in _rows(client, show=show)} == numbers
    # A long-running process must move a login out of the recent bucket.
    from django.utils import timezone

    next_instant = NOW + timedelta(microseconds=1)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(timezone, "now", lambda: next_instant)
        assert not _rows(client, show="signedIn7Days")
        assert len(_rows(client, show="signedIn30Days")) == 1
        assert len(_rows(client, show="lastSignInOlder")) == 2


def test_verification_sources_match_member_rows_and_exclude_unverified(admin, frozen_time):
    automatic = make_member(phone="08035553000", verification_status=VerificationStatus.VERIFIED)
    imported_automatic = make_member(
        phone="08035553001",
        verification_status=VerificationStatus.VERIFIED,
        registration_channel=RegistrationChannel.IMPORT,
    )
    chapter = make_member(
        phone="08035553002",
        verification_status=VerificationStatus.VERIFIED,
        registration_channel=RegistrationChannel.IMPORT,
        verified_by=admin,
    )
    person = make_member(
        phone="08035553003", verification_status=VerificationStatus.VERIFIED, verified_by=admin
    )
    make_member(phone="08035553004", verified_by=admin)
    client = client_for(admin)
    counts = _analysis(client)["members"]["verification_sources"]
    assert counts == {"automatic": 2, "chapter": 1, "person": 1}
    expected = {
        "automatic": {automatic.membership_number, imported_automatic.membership_number},
        "chapter": {chapter.membership_number},
        "person": {person.membership_number},
    }
    for source, numbers in expected.items():
        rows = _rows(client, show=analysis.VERIFICATION_FILTERS[source])
        assert {row["membership_number"] for row in rows} == numbers
        assert all(row["verified_how"] == source for row in rows)
        assert len(rows) == counts[source]


@pytest.fixture
def varied_profiles(admin):
    ward = Ward.objects.create(
        code="KD-KN-DETAIL-W01", name="Detail Ward", lga=Lga.objects.get(code="KD-KADUNA-NORTH")
    )
    unit = PollingUnit.objects.create(code="KD-DETAIL-PU001", name="Detail Unit", ward=ward)
    full = make_member(
        phone="08035554000",
        full_name="Complete Member",
        vin="90F5B01234567890001",
        gender="female",
        date_of_birth="1990-01-01",
        pvc_image="members/pvc/card.jpg",
        photo="members/photos/selfie.jpg",
        verification_status=VerificationStatus.VERIFIED,
        polling_unit_from_list=True,
        verified_by=admin,
    )
    full.scope_level = GeographyLevel.POLLING_UNIT
    full.polling_unit = unit
    full.full_clean()
    full.save()
    partial = make_member(phone="08035554001", vin="90F5B01234567890002", gender="male")
    minimal = make_member(
        phone="08035554002", full_name="", registration_channel=RegistrationChannel.USSD
    )
    minimal.scope_level = GeographyLevel.STATE
    minimal.full_clean()
    minimal.save()
    unplaced = make_member(phone="08035554003")
    unplaced.scope_level = GeographyLevel.COUNTRY
    unplaced.full_clean()
    unplaced.save()
    outside = make_member(phone="08035554004", lga="KN-AJINGI")
    return full, partial, minimal, unplaced, outside


def test_quality_drill_downs_agree_with_scoped_counts(admin, varied_profiles, frozen_time):
    client = client_for(admin)
    counts = _analysis(client, level="state", code="KD")["members"]
    assert counts["total"] == 3
    assert counts["quality"] == {
        "missing_name": 1,
        "missing_vin": 1,
        "missing_card_photo": 2,
        "missing_selfie": 2,
        "missing_gender": 1,
        "missing_birth_date": 2,
        "missing_state": 0,
        "missing_lga": 1,
        "missing_ward": 2,
        "missing_polling_unit": 2,
        "polling_unit_from_list": 1,
    }
    assert counts["fully_verified"] == 1
    # Identity completeness has its existing five fields; name/account/placement do not alter it.
    assert counts["profile"]["vin"] == 2 and counts["profile"]["selfie"] == 1
    for group, filters in (
        ("quality", analysis.QUALITY_FILTERS),
        ("accounts", analysis.ACCOUNT_FILTERS),
        ("verification_sources", analysis.VERIFICATION_FILTERS),
    ):
        for key, show in filters.items():
            rows = _rows(client, show=show, state__code="KD")
            assert len(rows) == counts[group][key], (group, key)
            assert all(row["scope"]["stateId"] == "KD" for row in rows)
    national = _analysis(client)
    assert national["members"]["quality"]["missing_state"] == 1
    assert len(_rows(client, show="missingState")) == 1


def test_unplaced_members_follow_the_selected_child_and_not_exco_sums(
    admin, varied_profiles, frozen_time
):
    client = client_for(admin)
    national = _analysis(client)
    assert national["coverage"]["unplaced_members"] == 1
    assert sum(part["members"] for part in national["parts"]) + 1 == 5
    assert _analysis(client, by="geopoliticalZone")["coverage"]["unplaced_members"] == 1
    state = _analysis(client, level="state", code="KD")
    assert state["coverage"]["unplaced_members"] == 1
    lga = _analysis(client, level="lga", code="KD-KADUNA-NORTH")
    assert lga["coverage"]["unplaced_members"] == 1
    full = varied_profiles[0]
    leaf = _analysis(client, level="pollingUnit", code=full.polling_unit.code)
    assert leaf["by"] is None and leaf["parts"] == []
    assert leaf["coverage"]["unplaced_members"] == 0
    # One appointment covers two states: additive exco child totals exceed the overall count.
    role = custom_role(admin, "Detail State Officer", [Capability.VIEW_GEOGRAPHY])
    assert (
        appoint(
            admin,
            full,
            role,
            [{"level": "state", "stateId": "KD"}, {"level": "state", "stateId": "KN"}],
        ).status_code
        == 201
    )
    national = _analysis(client)
    assert national["exco"]["appointments"] == 1
    assert sum(part["exco"] for part in national["parts"]) == 2
    assert national["coverage"]["unplaced_members"] == 1


def test_analysis_and_new_filters_cannot_escape_readers_scope(admin, varied_profiles):
    operator = member_account("08035555000")
    role = custom_role(admin, "Detail Kaduna Reader", [Capability.MANAGE_MEMBERSHIP])
    assert appoint(admin, operator, role, [{"level": "state", "stateId": "KD"}]).status_code == 201
    client = client_for(operator.user)
    body = _analysis(client, level="state", code="KD")
    assert body["members"]["total"] == 4  # three profiles and the operator
    assert client.get(ANALYSIS).status_code == 403
    assert client.get(ANALYSIS, {"level": "state", "code": "KN"}).status_code == 403
    for show in (
        *analysis.QUALITY_FILTERS.values(),
        *analysis.ACCOUNT_FILTERS.values(),
        *analysis.VERIFICATION_FILTERS.values(),
    ):
        assert not _rows(client, show=show, state__code="KN")
        assert all(row["scope"]["stateId"] == "KD" for row in _rows(client, show=show))


def test_empty_analysis_returns_zero_detail_counts_and_unknown_filter_is_empty(admin):
    client = client_for(admin)
    body = _analysis(client)
    members = body["members"]
    assert members["total"] == members["previous_7_days"] == members["previous_30_days"] == 0
    for group in ("accounts", "quality", "verification_sources"):
        assert members[group] and not any(members[group].values())
    assert body["coverage"]["unplaced_members"] == 0
    assert not _rows(client, show="unknownDetail")


def test_read_only_profile_fields_obey_existing_sensitive_access(admin):
    subject = make_member(
        phone="08035556000",
        whatsapp_number="+2348035556999",
        verified_at=NOW,
        polling_unit_from_list=True,
    )
    operator = member_account("08035556001")
    blind = custom_role(admin, "Detail Membership Operator", [Capability.MANAGE_MEMBERSHIP])
    area = [{"level": "state", "stateId": "KD"}]
    assert appoint(admin, operator, blind, area).status_code == 201
    url = f"{MEMBERS}{subject.membership_number}/"
    client = client_for(operator.user)
    redacted = client.get(url).json()
    assert redacted["whatsapp_number"] is None and redacted["sensitive_visible"] is False
    assert datetime.fromisoformat(redacted["verified_at"]) == NOW
    assert redacted["polling_unit_from_list"] is True
    visible = client_for(admin).get(url).json()
    assert visible["whatsapp_number"] == "+2348035556999"
    assert visible["sensitive_visible"] is True
    response = client.patch(
        url,
        {
            "occupation": "Teacher",
            "whatsapp_number": "+2348035556888",
            "verified_at": "2000-01-01T00:00:00Z",
            "polling_unit_from_list": False,
        },
        format="json",
    )
    assert response.status_code == 200, response.content
    subject.refresh_from_db()
    assert subject.occupation == "Teacher"
    assert subject.whatsapp_number == "+2348035556999"
    assert subject.verified_at == NOW and subject.polling_unit_from_list is True
    # A scoped holder of the existing private-data capability gets the same field.
    private = custom_role(admin, "Detail Private Reader", [Capability.VIEW_MEMBER_SENSITIVE_DATA])
    assert appoint(admin, operator, private, area).status_code == 201
    assert client_for(operator.user).get(url).json()["whatsapp_number"] == "+2348035556999"


def test_legacy_empty_vin_has_the_same_gap_in_summary_and_member_rows(
    admin, varied_profiles, frozen_time
):
    full = varied_profiles[0]
    # Old imports may predate the model's conversion of blank VIN to NULL.
    Member.objects.filter(pk=full.pk).update(vin="")
    client = client_for(admin)
    counts = _analysis(client, level="state", code="KD")["members"]
    assert counts["quality"]["missing_vin"] == 2
    assert counts["profile"]["vin"] == 1
    assert counts["fully_verified"] == 0
    rows = _rows(client, show="missingVin", state__code="KD")
    assert len(rows) == 2
    row = next(row for row in rows if row["membership_number"] == full.membership_number)
    assert row["gaps"] == ["vin"]
    assert not _rows(client, show="fullyVerified", state__code="KD")

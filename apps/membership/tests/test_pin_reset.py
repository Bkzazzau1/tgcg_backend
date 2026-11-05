"""A National Administrator resets a member's PIN (5 November 2026): it
becomes their phone number (or VIN or NIN), every session ends, any lockout
lifts, the member is told and must choose their own PIN at the next sign-in.
Every name and number here is made up."""

from datetime import timedelta

import pytest
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.token_blacklist.models import BlacklistedToken
from rest_framework_simplejwt.tokens import RefreshToken

from apps.access import engine
from apps.access.models import AssignmentStatus, AuditEvent, RoleAssignment, RoleDefinition
from apps.access.tests.test_access import appoint, client_for, member_account
from apps.accounts.models import User
from apps.core.choices import Role
from apps.core.permissions import Capability, capabilities_for
from apps.membership.models import Member
from conftest import PASSWORD

pytestmark = pytest.mark.django_db


@pytest.fixture
def admin():
    return User.objects.create_user(
        "NA-1", PASSWORD, full_name="National Admin", role=Role.NATIONAL_ADMINISTRATOR
    )


def fresh(user):
    """The account as a new request would load it."""
    return User.objects.get(pk=user.pk)


def reset(actor, member):
    return client_for(actor).post(f"/api/v1/members/{member.membership_number}/reset-pin/")


def with_phone(phone="08033330101", name="Musa Bello"):
    member = member_account(phone, name=name)
    User.objects.filter(pk=member.user_id).update(phone_number=member.phone_number)
    member.user.refresh_from_db()
    return member


def test_the_pin_becomes_the_phone_and_their_own_comes_first(admin):
    member = with_phone()
    user = member.user
    User.objects.filter(pk=user.pk).update(
        failed_login_attempts=3, locked_until=timezone.now() + timedelta(minutes=10)
    )
    old_session = RefreshToken.for_user(user)

    answer = reset(admin, member)
    assert answer.status_code == 200, answer.content
    assert answer.json() == {"first_password": "phone number"}
    user.refresh_from_db()
    assert user.check_password("08033330101") and user.must_change_password
    assert (user.failed_login_attempts, user.locked_until) == (0, None)
    assert BlacklistedToken.objects.filter(token__jti=old_session["jti"]).exists()
    event = AuditEvent.objects.get(action="account.pin_reset")
    assert event.target_id == member.membership_number
    assert event.changes == {"first_password": "phone number", "sessions": "ended"}
    assert "08033330101" not in str(event.changes)
    inbox = client_for(user).get("/api/v1/me/messages/").json()
    assert inbox[0]["title"] == "Your PIN was reset"

    # Signing in with the phone, then their own PIN before anything else.
    client = APIClient()
    login = client.post(
        "/api/v1/auth/login/",
        {"username": "0803 333 0101", "password": "08033330101"},
        format="json",
    )
    assert login.status_code == 200, login.content
    assert login.json()["user"]["must_change_pin"] is True
    assert login.json()["user"]["first_pin"] == "phone number"
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {login.json()['access']}")
    assert client.get("/api/v1/me/member/").status_code == 403
    changed = client.post(
        "/api/v1/auth/password/",
        {"current_password": "08033330101", "new_password": "482915"},
        format="json",
    )
    assert changed.status_code == 200, changed.content
    user.refresh_from_db()
    assert not user.must_change_password and user.check_password("482915")
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {changed.json()['access']}")
    assert client.get("/api/v1/auth/me/").json()["first_pin"] is None


def test_without_a_phone_it_becomes_their_vin_or_nin(admin):
    vin_member = with_phone("08033330102", name="Aisha Sani")
    Member.objects.filter(pk=vin_member.pk).update(phone_number=None, vin="90F5B0E8D0295847263")
    User.objects.filter(pk=vin_member.user_id).update(phone_number=None)
    assert reset(admin, vin_member).json() == {"first_password": "VIN"}
    assert User.objects.get(pk=vin_member.user_id).check_password("90F5B0E8D0295847263")

    nin_member = with_phone("08033330103", name="Hauwa Lawal")
    Member.objects.filter(pk=nin_member.pk).update(phone_number=None, nin="12345678901")
    User.objects.filter(pk=nin_member.user_id).update(phone_number=None)
    assert reset(admin, nin_member).json() == {"first_password": "NIN"}
    assert User.objects.get(pk=nin_member.user_id).check_password("12345678901")


def test_only_a_national_administrator_resets_and_never_another_or_their_own(admin):
    member = with_phone()
    # Protected: held by National Administrators only, never given by a role.
    assert Capability.RESET_MEMBER_PINS in capabilities_for(admin)
    coordinator = with_phone("08033330104", name="Bala Umar")
    assert Capability.RESET_MEMBER_PINS not in capabilities_for(coordinator.user)
    assert reset(coordinator.user, member).status_code == 403
    assert not User.objects.get(pk=member.user_id).must_change_password

    other_admin = User.objects.create_user(
        "NA-2", PASSWORD, full_name="Other Admin", role=Role.NATIONAL_ADMINISTRATOR
    )
    held = with_phone("08033330105", name="Grace Obi")
    Member.objects.filter(pk=held.pk).update(user=other_admin)
    assert engine.is_national_administrator(other_admin)
    assert reset(admin, Member.objects.get(pk=held.pk)).status_code == 403
    assert User.objects.get(pk=other_admin.pk).check_password(PASSWORD)

    own = with_phone("08033330106", name="Ibrahim Ali")
    Member.objects.filter(pk=own.pk).update(user=admin)
    refused = reset(admin, Member.objects.get(pk=own.pk))
    assert refused.status_code in (400, 403)
    assert User.objects.get(pk=admin.pk).check_password(PASSWORD)


def test_no_administrator_account_is_reset_suspended_or_not(admin):
    """A suspended administrator is still one: resetting them would let one
    administrator act as two."""
    second = User.objects.create_user(
        "NA-2", PASSWORD, full_name="Second Admin", role=Role.NATIONAL_ADMINISTRATOR
    )
    appointed = with_phone("08033330109", name="Next Admin")
    na_role = RoleDefinition.objects.get(code=Role.NATIONAL_ADMINISTRATOR)
    made = appoint(admin, appointed, na_role, [{"level": "country"}])
    assert made.status_code == 201, made.content
    RoleAssignment.objects.filter(member=appointed).update(status=AssignmentStatus.SUSPENDED)
    assert not engine.is_national_administrator(fresh(appointed.user))
    assert reset(admin, appointed).status_code == 403
    RoleAssignment.objects.filter(member=appointed).update(
        status=AssignmentStatus.ACTIVE, starts_at=timezone.now() + timedelta(days=3)
    )
    assert reset(second, appointed).status_code == 403  # not started yet
    assert User.objects.get(pk=appointed.user_id).check_password(PASSWORD)


def test_staff_and_site_administration_accounts_are_not_reset(admin):
    observer = with_phone("08033330110", name="Field Observer")
    User.objects.filter(pk=observer.user_id).update(role=Role.OBSERVER)
    assert reset(admin, observer).status_code == 400  # a staff password, not a PIN
    superuser = with_phone("08033330111", name="Site Owner")
    User.objects.filter(pk=superuser.user_id).update(is_staff=True, is_superuser=True)
    assert reset(admin, superuser).status_code == 403
    assert not AuditEvent.objects.filter(action="account.pin_reset").exists()


def test_abroad_the_pin_is_the_number_with_its_country_code(admin):
    member = with_phone("08033330112", name="Ngozi Okafor")
    Member.objects.filter(pk=member.pk).update(phone_number="+447700900123")
    User.objects.filter(pk=member.user_id).update(phone_number="+447700900123")
    answer = reset(admin, member)
    assert answer.json() == {"first_password": "phone number with its country code"}
    login = APIClient().post(
        "/api/v1/auth/login/",
        {"username": "+44 7700 900123", "password": "447700900123"},
        format="json",
    )
    assert login.status_code == 200, login.content
    assert login.json()["user"]["first_pin"] == "phone number with its country code"


def test_an_ended_membership_or_no_account_is_not_reset(admin):
    ended = with_phone("08033330107", name="Zainab Yusuf")
    Member.objects.filter(pk=ended.pk).update(membership_state="deactivated")
    assert reset(admin, ended).status_code == 400

    no_account = with_phone("08033330108", name="Halima Musa")
    Member.objects.filter(pk=no_account.pk).update(user=None)
    answer = reset(admin, no_account)
    assert answer.status_code == 400
    assert not AuditEvent.objects.filter(action="account.pin_reset").exists()

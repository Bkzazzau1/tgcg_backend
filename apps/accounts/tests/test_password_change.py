"""Changing one's own PIN or password while signed in (POST /auth/password/)."""

import pytest
from rest_framework.test import APIClient

from apps.access.models import AuditEvent
from apps.accounts.models import User
from apps.core.choices import Role
from conftest import PASSWORD

# Sign-in runs outside the request transaction (so failed attempts count); use
# real transactions so a refused sign-in does not poison the test.
pytestmark = pytest.mark.django_db(transaction=True, serialized_rollback=True)

PHONE = "08030000001"


def login(username, password):
    return APIClient().post(
        "/api/v1/auth/login/", {"username": username, "password": password}, format="json"
    )


def change(client, current, new):
    return client.post(
        "/api/v1/auth/password/",
        {"current_password": current, "new_password": new},
        format="json",
    )


def signed_in(username, password):
    session = login(username, password).json()
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {session['access']}")
    return client, session


def test_a_member_replaces_their_first_password_with_a_pin_and_other_sessions_end():
    User.objects.create_user(
        "TGCG-000001", PHONE, full_name="Ada Bello", role=Role.MEMBER, phone_number="+2348030000001"
    )
    client, _ = signed_in(PHONE, PHONE)
    _, elsewhere = signed_in(PHONE, PHONE)  # e.g. someone else holding the list

    weak = change(client, PHONE, "123456")
    assert weak.status_code == 400 and "new_password" in weak.json()
    wrong = change(client, "00000000000", "482915")
    assert wrong.status_code == 400 and "current_password" in wrong.json()

    changed = change(client, PHONE, "482915")
    assert changed.status_code == 200, changed.content
    tokens = changed.json()
    assert login(PHONE, PHONE).status_code == 401
    assert login(PHONE, "482915").status_code == 200
    # Every earlier session ends; this device carries on with its new tokens.
    refresh = "/api/v1/auth/refresh/"
    stale = APIClient().post(refresh, {"refresh": elsewhere["refresh"]}, format="json")
    assert stale.status_code == 401
    assert (
        APIClient().post(refresh, {"refresh": tokens["refresh"]}, format="json").status_code == 200
    )
    assert AuditEvent.objects.filter(action="account.passwordChanged").count() == 1


def test_staff_change_their_password_under_the_password_rules():
    User.objects.create_user("NA-1", PASSWORD, full_name="Admin", role=Role.NATIONAL_ADMINISTRATOR)
    client, _ = signed_in("NA-1", PASSWORD)
    assert change(client, PASSWORD, "short").status_code == 400
    assert change(client, PASSWORD, PASSWORD).status_code == 400  # not the same again
    assert change(client, PASSWORD, "A-Much-Longer-Pass-2028").status_code == 200
    assert login("NA-1", "A-Much-Longer-Pass-2028").status_code == 200


def test_changing_needs_a_session():
    assert change(APIClient(), PHONE, "482915").status_code == 401

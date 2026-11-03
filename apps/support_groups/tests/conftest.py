"""Fixtures for the support group tests: a National Administrator, a leader
and their approved group."""

import pytest

from apps.access.tests.test_access import client_for, member_account
from apps.accounts.models import User
from apps.core.choices import Role
from apps.geography.polling_units import load_polling_units
from apps.support_groups.models import SupportGroup
from conftest import PASSWORD

GROUPS = "/api/v1/support-groups/"
MINE = "/api/v1/me/support-groups/"


@pytest.fixture(autouse=True)
def quiet(settings):
    settings.SUMMARY_CACHE_SECONDS = 0
    load_polling_units(lga_codes=["KD-ZARIA"])


@pytest.fixture
def admin():
    return User.objects.create_user(
        "NA-1", PASSWORD, full_name="National Admin", role=Role.NATIONAL_ADMINISTRATOR
    )


@pytest.fixture
def leader():
    return member_account("08035550001", name="Halima Sani")


def details(**extra):
    return {
        "organization_name": "Arewa Youth for Progress",
        "organization_coverage": "state",
        "organization_state": "KD",
        "organization_email": "info@ayp.ng",
        "organization_officials": [
            {"name": "Bala Musa", "position": "Secretary", "phone": "0803 555 0009"}
        ],
        **extra,
    }


def register(user, **extra):
    return client_for(user).post(f"{MINE}register/", details(**extra), format="json")


@pytest.fixture
def group(admin, leader):
    response = register(leader.user)
    assert response.status_code == 201, response.content
    found = SupportGroup.objects.get(uuid=response.json()["id"])
    assert client_for(admin).post(f"{GROUPS}{found.uuid}/approve/").status_code == 200
    found.refresh_from_db()
    return found

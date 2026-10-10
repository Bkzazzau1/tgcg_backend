import pytest
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.core.choices import GeographyLevel, Role
from apps.geography.models import Lga

PASSWORD = "Strong-Pass-2027!"


@pytest.fixture
def api_client():
    return APIClient()


@pytest.fixture
def make_user(db):
    def _make(access_id="TGCG-NA-0001", **extra):
        extra.setdefault("full_name", "Test Operator")
        return User.objects.create_user(access_id, PASSWORD, **extra)

    return _make


@pytest.fixture
def lga_coordinator(make_user):
    return make_user(
        "TGCG-KD-0001",
        full_name="Amina Kaduna",
        phone_number="08031234567",
        role=Role.LGA_COORDINATOR,
        scope_level=GeographyLevel.LGA,
        lga=Lga.objects.get(code="KD-KADUNA-NORTH"),
    )


@pytest.fixture
def auth_client(api_client, make_user):
    api_client.force_authenticate(make_user())
    return api_client

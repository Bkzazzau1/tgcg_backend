"""Fixtures shared by the field tests: polling units in Kaduna North and the
people who report, coordinate and watch there."""

import pytest
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.core.choices import GeographyLevel, Role
from apps.core.permissions import FIELD_ROLES
from apps.geography.models import Lga, PollingUnit, State, Ward
from conftest import PASSWORD, verified_member


@pytest.fixture(autouse=True)
def media_root(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path


@pytest.fixture
def pus(db):
    ward = Ward.objects.create(
        code="KD-KADUNA-NORTH-W01", name="Ward 01", lga=Lga.objects.get(code="KD-KADUNA-NORTH")
    )
    return [
        PollingUnit.objects.create(code=f"KD-KN-W01-PU00{i}", name=f"Unit {i}", ward=ward)
        for i in (1, 2)
    ]


def login(**fields):
    user = User.objects.create_user(password=PASSWORD, **fields)
    if user.role in FIELD_ROLES:
        verified_member(user)  # agents and observers act only once verified
    client = APIClient()
    client.force_authenticate(user)
    return client


@pytest.fixture
def agent(pus):
    return login(
        access_id="AG-1",
        full_name="Agent One",
        role=Role.POLLING_UNIT_AGENT,
        scope_level=GeographyLevel.POLLING_UNIT,
        polling_unit=pus[0],
    )


@pytest.fixture
def other_agent(pus):
    return login(
        access_id="AG-2",
        full_name="Agent Two",
        role=Role.POLLING_UNIT_AGENT,
        scope_level=GeographyLevel.POLLING_UNIT,
        polling_unit=pus[1],
    )


@pytest.fixture
def coordinator(db):
    return login(
        access_id="LC-KN",
        full_name="Kaduna North Coordinator",
        role=Role.LGA_COORDINATOR,
        scope_level=GeographyLevel.LGA,
        lga=Lga.objects.get(code="KD-KADUNA-NORTH"),
    )


@pytest.fixture
def lagos(db):
    return login(
        access_id="SC-LA",
        full_name="Lagos Coordinator",
        role=Role.STATE_COORDINATOR,
        scope_level=GeographyLevel.STATE,
        state=State.objects.get(code="LA"),
    )


@pytest.fixture
def director(db):
    return login(access_id="SRD-1", full_name="Director", role=Role.SITUATION_ROOM_DIRECTOR)

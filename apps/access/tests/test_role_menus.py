"""Each fixed role holds what the agreed role grid gives it, and the server
enforces it (docs/access-control.md, "Role menus")."""

import pytest
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.core.choices import GeographyLevel, Role
from apps.core.permissions import ROLE_CAPABILITIES
from apps.core.permissions import Capability as C
from apps.geography.models import Ward
from apps.geography.polling_units import load_polling_units
from conftest import PASSWORD

pytestmark = pytest.mark.django_db

LIVE, ALERTS, ANALYTICS = C.VIEW_LIVE_OPERATIONS, C.VIEW_ALERTS, C.VIEW_ANALYTICS
MEDIA, AUDIT, SITUATION = C.VIEW_MEDIA_INTELLIGENCE, C.VIEW_AUDIT, C.VIEW_SITUATION_ROOM
EVIDENCE, FORUM = C.VIEW_EVIDENCE, C.VIEW_DISCUSSION_ROOM
SCREENS = {LIVE, ALERTS, ANALYTICS, C.MANAGE_GEOGRAPHY, MEDIA, AUDIT, SITUATION, EVIDENCE, FORUM}

# The screen-defining permissions of each role, from the grid agreed on
# 10 October 2026. The National Administrator holds everything.
GRID = {
    Role.NATIONAL_COLLATION_OFFICER: {LIVE, ALERTS, SITUATION, EVIDENCE, FORUM},
    Role.SITUATION_ROOM_DIRECTOR: {LIVE, ALERTS, ANALYTICS, MEDIA, SITUATION, EVIDENCE, FORUM},
    Role.ZONAL_COORDINATOR: {LIVE, ALERTS, ANALYTICS, MEDIA, SITUATION, EVIDENCE, FORUM},
    Role.STATE_COORDINATOR: {LIVE, ALERTS, ANALYTICS, MEDIA, SITUATION, EVIDENCE, FORUM},
    Role.LGA_COORDINATOR: {LIVE, ALERTS, SITUATION, EVIDENCE, FORUM},
    Role.WARD_COORDINATOR: {LIVE, SITUATION, EVIDENCE, FORUM},
    Role.POLLING_UNIT_AGENT: {EVIDENCE, FORUM},
    Role.OBSERVER: {EVIDENCE, FORUM},
    Role.LEGAL_OFFICER: {MEDIA, AUDIT, SITUATION, EVIDENCE, FORUM},
    Role.TECHNICAL_SUPPORT: {AUDIT, FORUM},
    Role.READ_ONLY_EXECUTIVE: {LIVE, ANALYTICS, MEDIA, SITUATION},
}


@pytest.mark.parametrize("role", list(GRID))
def test_each_role_holds_the_screens_the_grid_gives_it(role):
    assert {code for code in SCREENS if code in ROLE_CAPABILITIES[role]} == GRID[role]


def test_only_the_national_administrator_manages_geography():
    holders = {role for role, codes in ROLE_CAPABILITIES.items() if C.MANAGE_GEOGRAPHY in codes}
    assert holders == {Role.NATIONAL_ADMINISTRATOR}


def staff(access_id, role, **scope):
    user = User.objects.create_user(access_id, PASSWORD, full_name=access_id, role=role, **scope)
    client = APIClient()
    client.force_authenticate(user)
    return client


def test_technical_support_sees_no_election_data():
    director = staff("SRD-1", Role.SITUATION_ROOM_DIRECTOR)
    reported = director.post(
        "/api/v1/incidents/",
        {"title": "Ballot box snatched", "category": "Violence / threat", "severity": "critical"},
        format="json",
    )
    assert reported.status_code == 201, reported.content
    assert director.get("/api/v1/incidents/").json()["count"] == 1

    support = staff("TS-1", Role.TECHNICAL_SUPPORT)
    assert support.get("/api/v1/incidents/").json()["count"] == 0
    assert support.get("/api/v1/collation/").status_code == 403
    assert support.get("/api/v1/tracking/live/").status_code == 403
    # Their own work still opens: the audit log of system events.
    assert support.get("/api/v1/access/audit/").status_code == 200


def test_live_agents_and_coverage_need_live_operations():
    load_polling_units(lga_codes=["KD-ZARIA"])
    ward = staff(
        "WC-1",
        Role.WARD_COORDINATOR,
        scope_level=GeographyLevel.WARD,
        ward=Ward.objects.get(code="18-23-01"),
    )
    assert ward.get("/api/v1/tracking/coverage/").status_code == 200
    assert staff("EX-1", Role.READ_ONLY_EXECUTIVE).get("/api/v1/tracking/live/").status_code == 200

    legal = staff("LO-1", Role.LEGAL_OFFICER)
    assert legal.get("/api/v1/tracking/live/").status_code == 403
    assert legal.get("/api/v1/tracking/coverage/").status_code == 403


def test_the_audit_log_is_for_administrators_legal_and_support():
    for access_id, role in (
        ("NCO-1", Role.NATIONAL_COLLATION_OFFICER),
        ("SRD-1", Role.SITUATION_ROOM_DIRECTOR),
        ("EX-1", Role.READ_ONLY_EXECUTIVE),
    ):
        assert staff(access_id, role).get("/api/v1/access/audit/").status_code == 403, role
    assert staff("LO-1", Role.LEGAL_OFFICER).get("/api/v1/access/audit/").status_code == 200

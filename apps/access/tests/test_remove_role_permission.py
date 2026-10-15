"""manage.py remove_role_permission: as Role Management would, audited."""

import io

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from apps.access.models import AuditEvent, RoleCategory, RoleDefinition
from apps.access.services import create_role
from apps.accounts.models import User
from apps.core.choices import GeographyLevel, Role
from apps.core.permissions import Capability as C
from apps.geography.models import State
from conftest import PASSWORD

pytestmark = pytest.mark.django_db


@pytest.fixture
def admin():
    return User.objects.create_user(
        "NA-1", PASSWORD, full_name="National Admin", role=Role.NATIONAL_ADMINISTRATOR
    )


@pytest.fixture
def executive_role(admin):
    return create_role(
        actor=admin,
        name="PU Coordinator",
        description="Polling unit executive.",
        category=RoleCategory.FIELD,
        permitted_levels=[GeographyLevel.POLLING_UNIT],
        permissions=sorted({C.VIEW_INCIDENTS, C.VIEW_SITUATION_ROOM}),
    )


def run(*flags, by="NA-1"):
    out = io.StringIO()
    call_command(
        "remove_role_permission",
        C.VIEW_SITUATION_ROOM,
        "--role",
        "pu coordinator",
        "--role",
        "Ward Coordinator",
        "--role",
        "No Such Role",
        "--by",
        by,
        *flags,
        stdout=out,
    )
    return out.getvalue()


def test_a_dry_run_reports_and_changes_nothing(executive_role):
    report = run()
    assert "PU Coordinator: loses it" in report
    assert "Ward Coordinator: a system role, set in code" in report
    assert "No Such Role: no such role" in report
    executive_role.refresh_from_db()
    assert C.VIEW_SITUATION_ROOM in executive_role.permissions


def test_the_permission_goes_and_the_change_is_audited(admin, executive_role):
    assert "Removed viewSituationRoom from 1 role(s)." in run("--commit")
    executive_role.refresh_from_db()
    assert executive_role.permissions == [C.VIEW_INCIDENTS]
    event = AuditEvent.objects.get(action="role.updated", target_id=executive_role.code)
    assert event.actor == admin
    # A system role keeps the permissions the code gives it.
    ward = RoleDefinition.objects.get(code=Role.WARD_COORDINATOR.value)
    assert C.VIEW_INCIDENTS in ward.permissions
    # Running again changes nothing.
    assert "PU Coordinator: does not have it" in run("--commit")


def test_only_a_national_administrator_may(executive_role):
    User.objects.create_user(
        "SC-1",
        PASSWORD,
        full_name="Coordinator",
        role=Role.STATE_COORDINATOR,
        scope_level=GeographyLevel.STATE,
        state=State.objects.get(code="KD"),
    )
    with pytest.raises(CommandError):
        run("--commit", by="SC-1")
    executive_role.refresh_from_db()
    assert C.VIEW_SITUATION_ROOM in executive_role.permissions

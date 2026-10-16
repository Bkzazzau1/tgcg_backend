"""Field-worker coverage follows each appointment's effective permissions."""

from datetime import timedelta
from types import SimpleNamespace

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from apps.access import engine
from apps.access.models import (
    AssignmentStatus,
    AuthoritySource,
    RoleAssignment,
    RoleAssignmentArea,
    RoleDefinition,
    RoleStatus,
)
from apps.accounts.models import User
from apps.core.choices import GeographyLevel, MembershipState, Role, VerificationStatus
from apps.core.permissions import Capability as C
from apps.geography.models import Lga, PollingUnit, Ward
from apps.geography.scope import ScopeRef
from apps.membership.models import Member
from apps.tracking.services import field_workers
from conftest import verified_member

pytestmark = pytest.mark.django_db


@pytest.fixture
def appointment():
    admin = User.objects.create_user(
        "FIELD-TEST-ADMIN", full_name="Test Administrator", role=Role.NATIONAL_ADMINISTRATOR
    )
    ward = Ward.objects.create(
        code="TEST-FIELD-WARD", name="Synthetic ward", lga=Lga.objects.get(code="KD-ZARIA")
    )
    unit = PollingUnit.objects.create(code="TEST-FIELD-UNIT-1", name="Test unit 1", ward=ward)
    other_unit = PollingUnit.objects.create(
        code="TEST-FIELD-UNIT-2", name="Test unit 2", ward=ward
    )
    user = User.objects.create_user(
        "FIELD-TEST-MEMBER", full_name="Test Member", role=Role.MEMBER,
        scope_level=GeographyLevel.POLLING_UNIT, polling_unit=unit,
    )
    member = verified_member(user)
    role = RoleDefinition(
        code="test-field-role", name="Test Field Role",
        permitted_levels=[GeographyLevel.POLLING_UNIT],
        permissions=sorted([C.FIELD_DUTY, C.CREATE_INCIDENT]),
    )
    role.full_clean()
    role.save()
    assignment = RoleAssignment(
        member=member, role=role, granted_by=admin, authority=AuthoritySource.RECOVERY
    )
    assignment.full_clean()
    assignment.save()
    area = RoleAssignmentArea(
        assignment=assignment, scope_level=GeographyLevel.POLLING_UNIT, polling_unit=unit
    )
    area.full_clean()
    area.save()
    return SimpleNamespace(
        admin=admin, user=user, member=member, role=role, assignment=assignment,
        area=area, unit=unit, other_unit=other_unit,
    )


@pytest.mark.parametrize(
    ("restricted", "allowed"),
    [
        (None, True),
        ([], False),
        ([C.CREATE_INCIDENT], False),
        ([C.FIELD_DUTY], True),
        ([C.FIELD_DUTY, C.CREATE_INCIDENT], True),
    ],
)
def test_worker_selection_matches_restricted_appointment_permissions(
    appointment, restricted, allowed
):
    assignment = appointment.assignment
    assignment.restricted_permissions = restricted
    assignment.full_clean()
    assignment.save()
    assert engine.allows(appointment.user, C.FIELD_DUTY) is allowed
    selected = field_workers(ScopeRef.national())
    assert (appointment.user.pk in selected) is allowed
    if allowed:
        assert selected[appointment.user.pk] == appointment.unit.pk


def test_empty_restriction_removes_member_from_coverage_api(appointment):
    client = APIClient()
    client.force_authenticate(appointment.admin)
    before = client.get("/api/v1/tracking/coverage/")
    assert before.status_code == 200
    assert before.json()["agents"] == 1
    assignment = appointment.assignment
    assignment.restricted_permissions = []
    assignment.save(update_fields=["restricted_permissions", "updated_at"])
    after = client.get("/api/v1/tracking/coverage/")
    assert after.status_code == 200
    assert after.json()["agents"] == 0


def test_another_appointment_cannot_supply_permission_to_a_restricted_area(appointment):
    restricted = appointment.assignment
    restricted.restricted_permissions = [C.CREATE_INCIDENT]
    restricted.save(update_fields=["restricted_permissions", "updated_at"])
    permitted = RoleAssignment.objects.create(
        member=appointment.member, role=appointment.role, restricted_permissions=[C.FIELD_DUTY],
        granted_by=appointment.admin, authority=AuthoritySource.RECOVERY,
    )
    area = RoleAssignmentArea(
        assignment=permitted, scope_level=GeographyLevel.POLLING_UNIT,
        polling_unit=appointment.other_unit,
    )
    area.full_clean()
    area.save()
    restricted_scope = ScopeRef.resolve(GeographyLevel.POLLING_UNIT, appointment.unit.code)
    allowed_scope = ScopeRef.resolve(GeographyLevel.POLLING_UNIT, appointment.other_unit.code)
    assert not engine.allows(appointment.user, C.FIELD_DUTY, restricted_scope)
    assert engine.allows(appointment.user, C.FIELD_DUTY, allowed_scope)
    assert appointment.user.pk not in field_workers(restricted_scope)
    assert field_workers(allowed_scope) == {appointment.user.pk: appointment.other_unit.pk}
    assert field_workers(ScopeRef.national()) == {appointment.user.pk: appointment.other_unit.pk}


def test_restricted_appointment_does_not_remove_an_independent_legacy_grant(appointment):
    user = appointment.user
    user.role = Role.POLLING_UNIT_AGENT
    user.full_clean()
    user.save()
    assignment = appointment.assignment
    assignment.restricted_permissions = []
    assignment.save(update_fields=["restricted_permissions", "updated_at"])
    area = appointment.area
    area.polling_unit = appointment.other_unit
    area.full_clean()
    area.save()
    assert engine.allows(user, C.FIELD_DUTY)
    assert field_workers(ScopeRef.national()) == {user.pk: appointment.unit.pk}
    restricted_scope = ScopeRef.resolve(GeographyLevel.POLLING_UNIT, appointment.other_unit.code)
    assert not engine.allows(user, C.FIELD_DUTY, restricted_scope)
    assert user.pk not in field_workers(restricted_scope)


def test_removing_role_permission_overrides_an_older_assignment_subset(appointment):
    assignment = appointment.assignment
    assignment.restricted_permissions = [C.FIELD_DUTY]
    assignment.save(update_fields=["restricted_permissions", "updated_at"])
    role = appointment.role
    role.permissions = [C.CREATE_INCIDENT]
    role.full_clean()
    role.save()
    assert not engine.allows(appointment.user, C.FIELD_DUTY)
    assert appointment.user.pk not in field_workers(ScopeRef.national())


@pytest.mark.parametrize(
    "blocked_by",
    [
        "inactive_account", "suspended_membership", "ended_membership", "unverified_identity",
        "pending_identity", "suspended_assignment", "expired_assignment", "future_assignment",
        "archived_role",
    ],
)
def test_worker_selection_retains_existing_eligibility_guards(appointment, blocked_by):
    now = timezone.now()
    if blocked_by == "inactive_account":
        User.objects.filter(pk=appointment.user.pk).update(is_active=False)
    elif blocked_by == "suspended_membership":
        Member.objects.filter(pk=appointment.member.pk).update(
            membership_state=MembershipState.SUSPENDED
        )
    elif blocked_by == "ended_membership":
        Member.objects.filter(pk=appointment.member.pk).update(
            membership_state=MembershipState.DEACTIVATED
        )
    elif blocked_by == "unverified_identity":
        Member.objects.filter(pk=appointment.member.pk).update(
            verification_status=VerificationStatus.UNVERIFIED
        )
    elif blocked_by == "pending_identity":
        Member.objects.filter(pk=appointment.member.pk).update(
            verification_status=VerificationStatus.PENDING_REVIEW
        )
    elif blocked_by == "suspended_assignment":
        RoleAssignment.objects.filter(pk=appointment.assignment.pk).update(
            status=AssignmentStatus.SUSPENDED
        )
    elif blocked_by == "expired_assignment":
        RoleAssignment.objects.filter(pk=appointment.assignment.pk).update(
            starts_at=now - timedelta(days=2), ends_at=now - timedelta(days=1)
        )
    elif blocked_by == "future_assignment":
        RoleAssignment.objects.filter(pk=appointment.assignment.pk).update(
            starts_at=now + timedelta(days=1)
        )
    else:
        RoleDefinition.objects.filter(pk=appointment.role.pk).update(status=RoleStatus.ARCHIVED)
    user = User.objects.get(pk=appointment.user.pk)
    engine.clear_cache()
    assert not engine.allows(user, C.FIELD_DUTY)
    assert user.pk not in field_workers(ScopeRef.national())

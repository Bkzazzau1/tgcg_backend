"""Dynamic roles, appointments and the authorization engine (docs/access-control.md)."""

from datetime import timedelta

import pytest
from django.db import connection
from django.utils import timezone
from rest_framework.test import APIClient

from apps.access import audit, engine
from apps.access.models import AuditEvent, RoleAssignment, RoleDefinition
from apps.accounts.models import User
from apps.core.choices import (
    GeographyLevel,
    MembershipState,
    Role,
    VerificationStatus,
)
from apps.core.permissions import Capability, capabilities_for
from apps.field.models import Incident
from apps.geography.models import Lga, State
from apps.geography.scope import ScopeRef, within_scope_q
from conftest import PASSWORD, make_member

pytestmark = pytest.mark.django_db
C = Capability
KD = {"level": "state", "stateId": "KD"}
KN = {"level": "state", "stateId": "KN"}


def client_for(user):
    client = APIClient()
    client.force_authenticate(user)
    return client


def member_account(phone, name="Musa Bello", verified=True, lga="KD-KADUNA-NORTH"):
    member = make_member(phone=phone, lga=lga, full_name=name)
    member.verification_status = (
        VerificationStatus.VERIFIED if verified else VerificationStatus.UNVERIFIED
    )
    member.save()
    member.user = User.objects.create_user(
        member.membership_number,
        PASSWORD,
        full_name=name,
        role=Role.MEMBER,
        scope_level=GeographyLevel.LGA,
        lga=member.lga,
    )
    member.save()
    return member


def custom_role(admin, name, permissions, levels=("state",), **extra):
    response = client_for(admin).post(
        "/api/v1/access/roles/",
        {"name": name, "permissions": permissions, "permitted_levels": list(levels), **extra},
        format="json",
    )
    assert response.status_code == 201, response.content
    return RoleDefinition.objects.get(code=response.json()["code"])


def appoint(actor, member, role, areas, **extra):
    return client_for(actor).post(
        "/api/v1/access/assignments/",
        {"member": member.membership_number, "role": role.code, "areas": areas, **extra},
        format="json",
    )


def kd():
    return ScopeRef.resolve("state", "KD")


def kn():
    return ScopeRef.resolve("state", "KN")


# --- membership and eligibility --------------------------------------------------


def test_member_without_a_role_has_no_operational_permissions():
    member = member_account("08031110001")
    assert capabilities_for(member.user) == frozenset()
    me = client_for(member.user).get("/api/v1/auth/me/").json()
    assert me["capabilities"] == [] and me["access"] == []


def test_unverified_member_cannot_receive_a_role(admin):
    member = member_account("08031110002", verified=False)
    role = custom_role(admin, "Kaduna Media Director", [C.SEND_BROADCAST])
    response = appoint(admin, member, role, [KD])
    assert response.status_code == 400
    assert "verified" in response.json()["member"][0]


def test_verified_member_receives_a_role_and_its_permissions(admin):
    member = member_account("08031110003")
    role = custom_role(admin, "Kaduna Media Director", [C.SEND_BROADCAST, C.VIEW_GEOGRAPHY])
    response = appoint(admin, member, role, [KD])
    assert response.status_code == 201, response.content
    body = response.json()
    assert body["state"] == "active" and body["authority"] == "nationalAdministrator"
    assert capabilities_for(member.user) == {C.SEND_BROADCAST, C.VIEW_GEOGRAPHY}
    me = client_for(member.user).get("/api/v1/auth/me/").json()
    assert me["access"][0]["role"] == role.code
    assert me["access"][0]["areas"][0]["stateId"] == "KD"


# --- several roles and areas -----------------------------------------------------


def test_one_member_holds_several_roles_and_one_role_in_several_areas(admin):
    member = member_account("08031110004")
    media = custom_role(admin, "State Media Director", [C.SEND_BROADCAST])
    incidents = custom_role(admin, "Incident Desk", [C.VIEW_INCIDENTS])
    assert appoint(admin, member, media, [KD, KN]).status_code == 201
    assert appoint(admin, member, incidents, [KN]).status_code == 201
    grants = engine.active_grants(member.user)
    assert len(grants) == 2
    assert engine.allows(member.user, C.SEND_BROADCAST, kd())
    assert engine.allows(member.user, C.SEND_BROADCAST, kn())


def test_each_appointment_keeps_its_own_area(admin):
    """Publish in Kaduna + view in Kano never becomes publish in Kano."""
    member = member_account("08031110005")
    publish = custom_role(admin, "Kaduna Publisher", [C.SEND_BROADCAST, C.VIEW_INCIDENTS])
    view = custom_role(admin, "Kano Viewer", [C.VIEW_INCIDENTS])
    assert appoint(admin, member, publish, [KD]).status_code == 201
    assert appoint(admin, member, view, [KN]).status_code == 201
    user = member.user
    assert engine.allows(user, C.SEND_BROADCAST, kd())
    assert not engine.allows(user, C.SEND_BROADCAST, kn())
    assert engine.allows(user, C.VIEW_INCIDENTS, kn())

    # Direct API calls cannot cross the line either.
    client = client_for(user)
    kano = client.post(
        "/api/v1/announcements/",
        {"title": "Kano rally", "body": "Saturday.", "scope": KN},
        format="json",
    )
    assert kano.status_code == 403
    kaduna = client.post(
        "/api/v1/announcements/",
        {"title": "Kaduna rally", "body": "Saturday.", "scope": KD},
        format="json",
    )
    assert kaduna.status_code == 201, kaduna.content


def test_geographic_reach_holds_for_lists_and_records(admin):
    member = member_account("08031110006")
    role = custom_role(admin, "Kano Incident Desk", [C.VIEW_INCIDENTS])
    assert appoint(admin, member, role, [KN]).status_code == 201
    reporter = User.objects.create_user("REP-1", PASSWORD, full_name="Reporter")
    kano_lga = Lga.objects.filter(state__code="KN").first()
    kaduna_lga = Lga.objects.get(code="KD-KADUNA-NORTH")
    for lga in (kano_lga, kaduna_lga):
        Incident.objects.create(
            reference=f"INC-{lga.code}",
            title="Ballot snatching",
            category="violence",
            severity="high",
            reporter=reporter,
            scope_level=GeographyLevel.LGA,
            lga=lga,
            state=lga.state,
            zone=lga.state.zone,
        )
    listing = client_for(member.user).get("/api/v1/incidents/").json()
    assert [row["reference"] for row in listing["results"]] == [f"INC-{kano_lga.code}"]
    hidden = client_for(member.user).get("/api/v1/incidents/INC-KD-KADUNA-NORTH/")
    assert hidden.status_code == 404


def test_scope_helpers_need_the_permission_for_users(admin):
    with pytest.raises(TypeError):
        within_scope_q(admin)


# --- restrictions and authority ---------------------------------------------------


def test_restrictions_only_reduce_permissions(admin):
    member = member_account("08031110007")
    role = custom_role(admin, "Announcements Officer", [C.SEND_BROADCAST, C.VIEW_COMMUNICATIONS])
    widened = appoint(admin, member, role, [KD], restricted_permissions=[C.VIEW_INCIDENTS])
    assert widened.status_code == 400
    narrowed = appoint(admin, member, role, [KD], restricted_permissions=[C.VIEW_COMMUNICATIONS])
    assert narrowed.status_code == 201
    assert capabilities_for(member.user) == {C.VIEW_COMMUNICATIONS}


def coordinator_with(admin, phone, permissions, areas, name="Kaduna Coordinator"):
    member = member_account(phone, name=name)
    role = custom_role(admin, f"{name} role", permissions)
    assert appoint(admin, member, role, areas).status_code == 201
    return member


def test_role_creators_cannot_exceed_their_authority(admin):
    creator = coordinator_with(
        admin, "08031110008", [C.MANAGE_ROLES, C.SEND_BROADCAST, C.VIEW_INCIDENTS], [KD]
    )
    client = client_for(creator.user)
    beyond = client.post(
        "/api/v1/access/roles/",
        {"name": "Too Strong", "permissions": [C.MANAGE_COLLATION], "permitted_levels": ["lga"]},
        format="json",
    )
    assert beyond.status_code == 403
    passing_admin = client.post(
        "/api/v1/access/roles/",
        {"name": "Sub Admin", "permissions": [C.MANAGE_ROLES], "permitted_levels": ["lga"]},
        format="json",
    )
    assert passing_admin.status_code == 403  # passing on admin powers is delegation
    within = client.post(
        "/api/v1/access/roles/",
        {"name": "LGA Media", "permissions": [C.SEND_BROADCAST], "permitted_levels": ["lga"]},
        format="json",
    )
    assert within.status_code == 201, within.content
    # Only the creator (or a National Administrator) can change it.
    other = coordinator_with(
        admin, "08031110009", [C.MANAGE_ROLES, C.SEND_BROADCAST], [KD], name="Other Coordinator"
    )
    code = within.json()["code"]
    assert (
        client_for(other.user)
        .patch(f"/api/v1/access/roles/{code}/", {"description": "x"}, format="json")
        .status_code
        == 403
    )
    assert (
        client.patch(
            f"/api/v1/access/roles/{code}/", {"description": "Media in an LGA"}, format="json"
        ).status_code
        == 200
    )


def test_coordinators_appoint_within_their_area_but_never_themselves(admin):
    coordinator = coordinator_with(admin, "08031110010", [C.ASSIGN_ROLES, C.SEND_BROADCAST], [KD])
    role = custom_role(admin, "LGA Media Officer", [C.SEND_BROADCAST], levels=("lga",))
    member = member_account("08031110011", name="Ada Obi")
    kaduna_lga = {"level": "lga", "lgaId": "KD-KADUNA-NORTH"}
    kano_lga = {"level": "lga", "lgaId": "KN-AJINGI"}
    assert appoint(coordinator.user, member, role, [kano_lga]).status_code == 403
    assert appoint(coordinator.user, coordinator, role, [kaduna_lga]).status_code == 403
    ok = appoint(coordinator.user, member, role, [kaduna_lga])
    assert ok.status_code == 201, ok.content
    assert ok.json()["authority"] == "assignment"
    # The role's level rules still apply.
    assert appoint(coordinator.user, member, role, [KD]).status_code == 400


def test_only_a_national_administrator_appoints_another(admin):
    coordinator = coordinator_with(admin, "08031110012", [C.ASSIGN_ROLES], [KD])
    member = member_account("08031110013", name="Next Admin")
    na_role = RoleDefinition.objects.get(code=Role.NATIONAL_ADMINISTRATOR)
    national = [{"level": "country"}]
    assert appoint(coordinator.user, member, na_role, national).status_code == 403
    response = appoint(admin, member, na_role, national)
    assert response.status_code == 201, response.content
    assert engine.is_national_administrator(member.user)


def test_custom_roles_cannot_take_the_protected_name_or_powers(admin):
    client = client_for(admin)
    named = client.post(
        "/api/v1/access/roles/",
        {
            "name": "National  Administrator!",
            "permissions": [C.VIEW_GEOGRAPHY],
            "permitted_levels": ["country"],
        },
        format="json",
    )
    assert named.status_code == 400
    powered = client.post(
        "/api/v1/access/roles/",
        {
            "name": "Admin Helper",
            "permissions": [C.APPOINT_NATIONAL_ADMINISTRATOR],
            "permitted_levels": ["country"],
        },
        format="json",
    )
    assert powered.status_code == 400


# --- lifecycle --------------------------------------------------------------------


def test_archiving_a_role_removes_its_access_at_once(admin):
    member = member_account("08031110014")
    role = custom_role(admin, "Short Lived", [C.SEND_BROADCAST])
    assert appoint(admin, member, role, [KD]).status_code == 201
    assert C.SEND_BROADCAST in capabilities_for(member.user)
    client = client_for(admin)
    assert client.post(f"/api/v1/access/roles/{role.code}/archive/", {}).status_code == 400
    archived = client.post(
        f"/api/v1/access/roles/{role.code}/archive/", {"reason": "Merged"}, format="json"
    )
    assert archived.status_code == 200
    assert capabilities_for(member.user) == frozenset()
    assert RoleAssignment.objects.get(role=role).status == "active"  # history kept
    assert appoint(admin, member_account("08031110015"), role, [KD]).status_code == 400
    client.post(f"/api/v1/access/roles/{role.code}/restore/", {}, format="json")
    assert C.SEND_BROADCAST in capabilities_for(member.user)


def test_expiry_and_scheduling_work_without_a_background_job(admin):
    member = member_account("08031110016")
    role = custom_role(admin, "Election Week", [C.SEND_BROADCAST])
    later = timezone.now() + timedelta(days=2)
    scheduled = appoint(admin, member, role, [KD], starts_at=later.isoformat())
    assert scheduled.json()["state"] == "scheduled"
    assert capabilities_for(member.user) == frozenset()
    assignment = RoleAssignment.objects.get(member=member)
    RoleAssignment.objects.filter(pk=assignment.pk).update(
        starts_at=timezone.now() - timedelta(days=3), ends_at=timezone.now() - timedelta(days=1)
    )
    assignment.refresh_from_db()
    assert assignment.effective_status() == "expired"
    assert capabilities_for(member.user) == frozenset()


def test_suspending_one_appointment_leaves_the_others(admin):
    member = member_account("08031110017")
    media = custom_role(admin, "Media", [C.SEND_BROADCAST])
    desk = custom_role(admin, "Desk", [C.VIEW_INCIDENTS])
    first = appoint(admin, member, media, [KD]).json()["id"]
    appoint(admin, member, desk, [KD])
    client = client_for(admin)
    assert client.post(f"/api/v1/access/assignments/{first}/suspend/", {}).status_code == 400
    suspended = client.post(
        f"/api/v1/access/assignments/{first}/suspend/", {"reason": "Review"}, format="json"
    )
    assert suspended.json()["state"] == "suspended"
    assert capabilities_for(member.user) == {C.VIEW_INCIDENTS}
    client.post(f"/api/v1/access/assignments/{first}/reactivate/", {}, format="json")
    assert capabilities_for(member.user) == {C.SEND_BROADCAST, C.VIEW_INCIDENTS}


def test_membership_suspension_blocks_everything_and_restoring_keeps_history(admin):
    member = member_account("08031110018")
    media = custom_role(admin, "Media", [C.SEND_BROADCAST])
    desk = custom_role(admin, "Desk", [C.VIEW_INCIDENTS])
    appoint(admin, member, media, [KD])
    revoked = appoint(admin, member, desk, [KD]).json()["id"]
    client_for(admin).post(
        f"/api/v1/access/assignments/{revoked}/revoke/", {"reason": "Moved"}, format="json"
    )
    member.membership_state = MembershipState.SUSPENDED
    member.save()
    assert capabilities_for(member.user) == frozenset()
    member.membership_state = MembershipState.ACTIVE
    member.save()
    assert capabilities_for(member.user) == {C.SEND_BROADCAST}  # revoked stays revoked


def test_membership_suspension_also_blocks_a_legacy_role(admin):
    legacy = User.objects.create_user(
        "SC-KD",
        PASSWORD,
        full_name="Kaduna",
        role=Role.STATE_COORDINATOR,
        scope_level=GeographyLevel.STATE,
        state=State.objects.get(code="KD"),
    )
    member = make_member(phone="08031110019")
    member.user = legacy
    member.membership_state = MembershipState.SUSPENDED
    member.save()
    assert capabilities_for(legacy) == frozenset()


def test_members_decline_or_resign_their_own_appointments(admin):
    member = member_account("08031110020")
    role = custom_role(admin, "Media", [C.SEND_BROADCAST])
    first = appoint(admin, member, role, [KD]).json()["id"]
    second = appoint(admin, member, role, [KN]).json()["id"]
    own = client_for(member.user)
    assert own.post(f"/api/v1/me/assignments/{first}/decline/", {}).json()["state"] == "declined"
    assert own.post(f"/api/v1/me/assignments/{second}/accept/").status_code == 200
    assert own.post(f"/api/v1/me/assignments/{second}/decline/", {}).status_code == 400
    resigned = own.post(
        f"/api/v1/me/assignments/{second}/resign/", {"reason": "New job"}, format="json"
    )
    assert resigned.json()["state"] == "resigned"
    assert capabilities_for(member.user) == frozenset()
    # Declined and resigned appointments are never silently reactivated.
    assert (
        client_for(admin)
        .post(f"/api/v1/access/assignments/{second}/reactivate/", {}, format="json")
        .status_code
        == 400
    )
    # Someone else's appointment is not reachable.
    stranger = member_account("08031110021", name="Stranger")
    assert (
        client_for(stranger.user).post(f"/api/v1/me/assignments/{first}/resign/", {}).status_code
        == 404
    )


def test_the_last_national_administrator_cannot_step_down(admin):
    successor = member_account("08031110022", name="Successor")
    na_role = RoleDefinition.objects.get(code=Role.NATIONAL_ADMINISTRATOR)
    appointed = appoint(admin, successor, na_role, [{"level": "country"}]).json()["id"]
    # Once the legacy administrator is gone, the successor is the only one left.
    admin.is_active = False
    admin.save()
    response = client_for(successor.user).post(
        f"/api/v1/me/assignments/{appointed}/resign/", {}, format="json"
    )
    assert response.status_code == 400
    assert engine.is_national_administrator(successor.user)


def test_administrator_appointments_need_the_protected_workflow(admin):
    successor = member_account("08031110023", name="Successor")
    na_role = RoleDefinition.objects.get(code=Role.NATIONAL_ADMINISTRATOR)
    appointed = appoint(admin, successor, na_role, [{"level": "country"}]).json()["id"]
    response = client_for(admin).post(
        f"/api/v1/access/assignments/{appointed}/suspend/", {"reason": "x"}, format="json"
    )
    assert response.status_code == 403


# --- audit ------------------------------------------------------------------------


def test_security_changes_are_audited_in_a_tamper_evident_chain(admin):
    member = member_account("08031110024")
    role = custom_role(admin, "Media", [C.SEND_BROADCAST])
    assignment = appoint(admin, member, role, [KD]).json()["id"]
    client = client_for(admin)
    client.post(
        f"/api/v1/access/assignments/{assignment}/suspend/", {"reason": "Check"}, format="json"
    )
    actions = list(AuditEvent.objects.order_by("id").values_list("action", flat=True))
    assert actions == ["role.created", "assignment.created", "assignment.suspended"]
    history = client.get(f"/api/v1/access/assignments/{assignment}/history/").json()
    assert [event["action"] for event in history] == ["assignment.suspended", "assignment.created"]
    assert "phone_number" not in str(history)
    assert client.get("/api/v1/access/audit/verify/").json() == {
        "intact": True,
        "first_broken_event": None,
    }

    # Editing a row behind the ORM's back breaks the chain.
    with connection.cursor() as cursor:
        cursor.execute(
            "UPDATE access_auditevent SET reason = 'nothing to see' WHERE action = %s",
            ["assignment.suspended"],
        )
    assert audit.verify_chain() is not None
    with pytest.raises(Exception):  # noqa: B017 - ValidationError from the queryset guard
        AuditEvent.objects.all().delete()


def test_audit_log_is_for_national_viewers_only(admin):
    member = member_account("08031110025")
    role = custom_role(admin, "Kaduna Auditor", [C.VIEW_AUDIT])
    appoint(admin, member, role, [KD])
    assert client_for(member.user).get("/api/v1/access/audit/").json()["count"] == 0
    assert client_for(admin).get("/api/v1/access/audit/").json()["count"] >= 2


def test_the_profile_names_the_member_behind_an_account(admin):
    member = member_account("08031110026")
    me = client_for(member.user).get("/api/v1/auth/me/").json()
    assert me["membership_number"] == member.membership_number
    assert client_for(admin).get("/api/v1/auth/me/").json()["membership_number"] is None

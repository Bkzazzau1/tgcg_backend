"""Delegation, supervision and protected administrator actions (docs/access-control.md)."""

import io
from datetime import timedelta

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone

from apps.access import engine
from apps.access.models import AuditEvent, DelegationGrant, RoleAssignment, RoleDefinition
from apps.access.tests.test_access import (
    KD,
    KN,
    appoint,
    client_for,
    custom_role,
    member_account,
)
from apps.accounts.models import User
from apps.core.choices import Role
from apps.core.permissions import Capability, capabilities_for
from apps.geography.models import Lga, Ward
from apps.geography.scope import ScopeRef
from conftest import PASSWORD

pytestmark = pytest.mark.django_db
C = Capability
KD_LGA = {"level": "lga", "lgaId": "KD-KADUNA-NORTH"}
KN_LGA = {"level": "lga", "lgaId": "KN-AJINGI"}
NATIONAL = [{"level": "country"}]
MEDIA = "communications"
POWERS = [C.ASSIGN_ROLES, C.DELEGATE_AUTHORITY, C.SEND_BROADCAST]


def delegate(actor, member, **values):
    body = {
        "recipient": member.membership_number,
        "permissions": [C.ASSIGN_ROLES, C.SEND_BROADCAST],
        "categories": [MEDIA],
        "levels": ["lga"],
        "areas": [KD],
        **values,
    }
    return client_for(actor).post("/api/v1/access/delegations/", body, format="json")


def media_role(admin, name, permissions=(C.SEND_BROADCAST,), levels=("lga",), **extra):
    return custom_role(admin, name, list(permissions), levels=levels, category=MEDIA, **extra)


# --- delegation ------------------------------------------------------------------


def test_delegated_coordinator_appoints_only_within_the_grant(admin):
    coordinator = member_account("08032220001", name="Delegated Coordinator")
    granted = delegate(admin, coordinator)
    assert granted.status_code == 201, granted.content
    assert granted.json()["source"] == "nationalAdministrator"
    # A delegation passes on powers, never operational permissions.
    assert capabilities_for(coordinator.user) == {C.ASSIGN_ROLES, C.VIEW_ROLES}
    assert not engine.allows(coordinator.user, C.SEND_BROADCAST, ScopeRef.resolve("state", "KD"))

    officer = media_role(admin, "LGA Media Officer")
    ok = appoint(coordinator.user, member_account("08032220002", name="Ada Obi"), officer, [KD_LGA])
    assert ok.status_code == 201, ok.content
    assert ok.json()["authority"] == "delegation"
    made = RoleAssignment.objects.get(uuid=ok.json()["id"])
    assert str(made.authority_grant.uuid) == granted.json()["id"]

    other = member_account("08032220003", name="Bala Musa")
    assert appoint(coordinator.user, other, officer, [KN_LGA]).status_code == 403  # area
    ward = Ward.objects.create(
        code="KD-KN-W99", name="Ward 99", lga=Lga.objects.get(code="KD-KADUNA-NORTH")
    )
    ward_role = media_role(admin, "Ward Media Officer", levels=("ward",))
    in_ward = [{"level": "ward", "wardId": ward.code}]
    assert appoint(coordinator.user, other, ward_role, in_ward).status_code == 403  # level
    field_role = custom_role(
        admin, "Field Helper", [C.SEND_BROADCAST], levels=("lga",), category="field"
    )
    assert appoint(coordinator.user, other, field_role, [KD_LGA]).status_code == 403  # category
    wider = media_role(admin, "Media and Incidents", [C.SEND_BROADCAST, C.VIEW_INCIDENTS])
    assert appoint(coordinator.user, other, wider, [KD_LGA]).status_code == 403  # permissions


def test_delegated_coordinator_creates_roles_within_the_grant(admin):
    coordinator = member_account("08032220004")
    granted = delegate(
        admin, coordinator, permissions=[C.MANAGE_ROLES, C.SEND_BROADCAST], levels=["lga", "ward"]
    )
    assert granted.status_code == 201, granted.content
    client = client_for(coordinator.user)

    def create(name, permissions, levels, category=MEDIA):
        body = {
            "name": name,
            "permissions": permissions,
            "permitted_levels": levels,
            "category": category,
        }
        return client.post("/api/v1/access/roles/", body, format="json").status_code

    assert create("LGA Media", [C.SEND_BROADCAST], ["lga"]) == 201
    assert create("LGA Field", [C.SEND_BROADCAST], ["lga"], category="field") == 403
    assert create("State Media", [C.SEND_BROADCAST], ["state"]) == 403
    assert create("Incident Media", [C.VIEW_INCIDENTS], ["lga"]) == 403


def test_redelegation_stays_inside_its_source_and_depth(admin):
    first = member_account("08032220005", name="First Coordinator")
    second = member_account("08032220006", name="Second Coordinator")
    third = member_account("08032220007", name="Third Coordinator")
    assert (
        delegate(admin, first, permissions=POWERS, can_redelegate=True, depth=2).status_code == 201
    )

    # Nothing to oneself, and nothing wider than the source.
    assert delegate(first.user, first).status_code == 403
    assert delegate(first.user, second, areas=[KN]).status_code == 403
    assert (
        delegate(first.user, second, permissions=[C.ASSIGN_ROLES, C.VIEW_INCIDENTS]).status_code
        == 403
    )
    assert delegate(first.user, second, categories=["field"]).status_code == 403
    assert delegate(first.user, second, levels=["state"]).status_code == 403
    deep = delegate(first.user, second, permissions=POWERS, can_redelegate=True, depth=2)
    assert deep.status_code == 403

    narrower = delegate(
        first.user, second, permissions=POWERS, can_redelegate=True, depth=1, areas=[KD_LGA]
    )
    assert narrower.status_code == 201, narrower.content
    assert narrower.json()["source"] == "delegation" and narrower.json()["depth"] == 1

    # The depth runs out one level further down.
    again = delegate(
        second.user, third, permissions=POWERS, can_redelegate=True, depth=1, areas=[KD_LGA]
    )
    assert again.status_code == 403
    last = delegate(second.user, third, areas=[KD_LGA])
    assert last.status_code == 201, last.content
    assert delegate(third.user, member_account("08032220008"), areas=[KD_LGA]).status_code == 403


def test_authority_never_flows_back_to_where_it_came_from(admin):
    first = member_account("08032220009", name="First Coordinator")
    second = member_account("08032220010", name="Second Coordinator")
    assert (
        delegate(admin, first, permissions=POWERS, can_redelegate=True, depth=2).status_code == 201
    )
    assert (
        delegate(first.user, second, permissions=POWERS, can_redelegate=True, depth=1).status_code
        == 201
    )
    officer = media_role(admin, "LGA Media Officer")
    # The second cannot hand authority back up to the first, by grant or by role.
    assert delegate(second.user, first).status_code == 403
    assert appoint(second.user, first, officer, [KD_LGA]).status_code == 403


def test_revoking_a_delegation_leaves_grants_made_from_it(admin):
    first = member_account("08032220011", name="First Coordinator")
    second = member_account("08032220012", name="Second Coordinator")
    upper = delegate(admin, first, permissions=POWERS, can_redelegate=True, depth=1).json()["id"]
    lower = delegate(first.user, second).json()["id"]
    officer = media_role(admin, "LGA Media Officer")

    revoked = client_for(admin).post(
        f"/api/v1/access/delegations/{upper}/revoke/", {"reason": "Reorganised"}, format="json"
    )
    assert revoked.status_code == 200 and revoked.json()["state"] == "revoked"
    assert capabilities_for(first.user) == frozenset()
    assert appoint(first.user, member_account("08032220013"), officer, [KD_LGA]).status_code == 403
    # The grant made from it stays valid until it is revoked or expires itself.
    assert DelegationGrant.objects.get(uuid=lower).is_effective()
    kept = appoint(second.user, member_account("08032220014", name="Kept"), officer, [KD_LGA])
    assert kept.status_code == 201, kept.content

    # Its grantor may still withdraw it; a stranger cannot even see it.
    url = f"/api/v1/access/delegations/{lower}/revoke/"
    stranger = member_account("08032220015", name="Stranger")
    assert client_for(stranger.user).post(url, {"reason": "x"}, format="json").status_code == 404
    assert client_for(first.user).post(url, {}, format="json").status_code == 400  # say why
    assert client_for(first.user).post(url, {"reason": "Done"}, format="json").status_code == 200
    assert appoint(second.user, member_account("08032220016"), officer, [KD_LGA]).status_code == 403
    history = client_for(admin).get(f"/api/v1/access/delegations/{lower}/history/").json()
    assert [event["action"] for event in history] == ["delegation.revoked", "delegation.created"]


def test_delegated_authority_cannot_escape_its_depth_or_term_through_roles(admin):
    coordinator = member_account("08032220017")
    ends = timezone.now() + timedelta(days=10)
    powers = [C.ASSIGN_ROLES, C.MANAGE_ROLES, C.DELEGATE_AUTHORITY, C.SEND_BROADCAST]
    granted = delegate(
        admin,
        coordinator,
        permissions=powers,
        can_redelegate=True,
        depth=1,
        ends_at=ends.isoformat(),
    )
    assert granted.status_code == 201, granted.content
    client = client_for(coordinator.user)
    shape = {"permitted_levels": ["lga"], "category": MEDIA}
    sub_delegator = client.post(
        "/api/v1/access/roles/",
        {"name": "Sub Delegator", "permissions": [C.ASSIGN_ROLES, C.DELEGATE_AUTHORITY], **shape},
        format="json",
    )
    assert sub_delegator.status_code == 403  # further delegation only by a counted grant
    appointer = client.post(
        "/api/v1/access/roles/",
        {"name": "LGA Appointer", "permissions": [C.ASSIGN_ROLES, C.SEND_BROADCAST], **shape},
        format="json",
    )
    assert appointer.status_code == 201, appointer.content
    role = RoleDefinition.objects.get(code=appointer.json()["code"])

    # Passing on a power may not outlast the authority it comes from.
    member = member_account("08032220018", name="Ada Obi")
    before_end = (ends - timedelta(days=1)).isoformat()
    after_end = (ends + timedelta(days=1)).isoformat()
    assert appoint(coordinator.user, member, role, [KD_LGA]).status_code == 403
    assert appoint(coordinator.user, member, role, [KD_LGA], ends_at=after_end).status_code == 403
    within = appoint(coordinator.user, member, role, [KD_LGA], ends_at=before_end)
    assert within.status_code == 201, within.content
    other = member_account("08032220019", name="Bala Musa")
    assert delegate(coordinator.user, other, areas=[KD_LGA]).status_code == 403
    assert delegate(coordinator.user, other, areas=[KD_LGA], ends_at=before_end).status_code == 201


# --- supervision -----------------------------------------------------------------


def test_supervisors_suspend_and_reactivate_only_what_they_supervise(admin):
    director_role = media_role(admin, "State Media Director", levels=("state",))
    officer_role = media_role(admin, "LGA Media Officer", supervising_role=director_role.code)
    director = member_account("08032220020", name="Media Director")
    assert appoint(admin, director, director_role, [KD]).status_code == 201
    kaduna_officer = member_account("08032220021", name="Kaduna Officer")
    kaduna = appoint(admin, kaduna_officer, officer_role, [KD_LGA]).json()["id"]
    kano_officer = member_account("08032220022", name="Kano Officer")
    kano = appoint(admin, kano_officer, officer_role, [KN_LGA]).json()["id"]

    client = client_for(director.user)
    listed = client.get("/api/v1/access/assignments/").json()
    assert [row["id"] for row in listed["results"]] == [kaduna]
    suspended = client.post(
        f"/api/v1/access/assignments/{kaduna}/suspend/", {"reason": "Absent"}, format="json"
    )
    assert suspended.status_code == 200 and suspended.json()["state"] == "suspended"
    assert capabilities_for(kaduna_officer.user) == frozenset()
    event = AuditEvent.objects.filter(action="assignment.suspended").latest("id")
    assert event.provenance["authority"] == "supervisor"
    assert event.provenance["role"] == director_role.code
    reactivated = client.post(f"/api/v1/access/assignments/{kaduna}/reactivate/", {}, format="json")
    assert reactivated.json()["state"] == "active"

    # Supervision gives nothing else: no revoking, nothing outside the director's state.
    revoke = client.post(
        f"/api/v1/access/assignments/{kaduna}/revoke/", {"reason": "x"}, format="json"
    )
    assert revoke.status_code == 403
    outside = client.post(
        f"/api/v1/access/assignments/{kano}/suspend/", {"reason": "x"}, format="json"
    )
    assert outside.status_code == 404
    nobody = member_account("08032220023", name="Nobody")
    assert client_for(nobody.user).get("/api/v1/access/assignments/").status_code == 403


def test_supervisors_with_role_powers_may_edit_the_roles_below_them(admin):
    director_role = media_role(
        admin, "National Media Director", [C.SEND_BROADCAST, C.MANAGE_ROLES], levels=("country",)
    )
    officer_role = media_role(
        admin, "State Media Officer", levels=("state",), supervising_role=director_role.code
    )
    director = member_account("08032220024", name="Media Director")
    assert appoint(admin, director, director_role, NATIONAL).status_code == 201
    clerk_role = media_role(
        admin, "Roles Clerk", [C.SEND_BROADCAST, C.MANAGE_ROLES], levels=("country",)
    )
    clerk = member_account("08032220025", name="Roles Clerk")
    assert appoint(admin, clerk, clerk_role, NATIONAL).status_code == 201

    url = f"/api/v1/access/roles/{officer_role.code}/"
    assert client_for(clerk.user).patch(url, {"description": "x"}, format="json").status_code == 403
    edited = client_for(director.user).patch(
        url, {"description": "Media in a state"}, format="json"
    )
    assert edited.status_code == 200
    # Never beyond the editor's own authority...
    wider = {"permissions": [C.SEND_BROADCAST, C.VIEW_INCIDENTS]}
    assert client_for(director.user).patch(url, wider, format="json").status_code == 403
    # ...and nobody becomes an editor by naming their own role as its supervisor.
    hijack = {"supervising_role": clerk_role.code}
    assert client_for(clerk.user).patch(url, hijack, format="json").status_code == 403


def test_holders_cannot_widen_a_role_they_hold(admin):
    """Powers held in one area must not reach another through a role's definition."""
    creator = member_account("08032220030", name="Kaduna Creator")
    creator_role = media_role(
        admin,
        "Kaduna Roles Officer",
        [C.MANAGE_ROLES, C.SEND_BROADCAST, C.VIEW_INCIDENTS],
        levels=("state",),
    )
    assert appoint(admin, creator, creator_role, [KD]).status_code == 201
    made = client_for(creator.user).post(
        "/api/v1/access/roles/",
        {
            "name": "Desk Officer",
            "permissions": [C.SEND_BROADCAST],
            "permitted_levels": ["state"],
            "category": MEDIA,
        },
        format="json",
    )
    assert made.status_code == 201, made.content
    desk = RoleDefinition.objects.get(code=made.json()["code"])
    assert appoint(admin, creator, desk, [KN]).status_code == 201  # held in Kano
    url = f"/api/v1/access/roles/{desk.code}/"
    widen = {"permissions": [C.SEND_BROADCAST, C.VIEW_INCIDENTS]}
    assert client_for(creator.user).patch(url, widen, format="json").status_code == 403
    assert not engine.allows(creator.user, C.VIEW_INCIDENTS, ScopeRef.resolve("state", "KN"))
    described = {"description": "Front desk"}
    assert client_for(creator.user).patch(url, described, format="json").status_code == 200


# --- National Administrators -----------------------------------------------------


def administrator(access_id, name):
    return User.objects.create_user(
        access_id, PASSWORD, full_name=name, role=Role.NATIONAL_ADMINISTRATOR
    )


def request_action(actor, target, kind="suspend", reason="Misuse of access"):
    return client_for(actor).post(
        "/api/v1/access/admin-actions/",
        {"target": target.access_id, "kind": kind, "reason": reason},
        format="json",
    )


def test_suspending_an_administrator_takes_two_administrators(admin):
    second = administrator("NA-2", "Second Admin")
    target = administrator("NA-3", "Third Admin")
    assert request_action(admin, admin).status_code == 403  # never against oneself
    assert request_action(member_account("08032220026").user, target).status_code == 403

    made = request_action(admin, target)
    assert made.status_code == 201, made.content
    body = made.json()
    assert body["status"] == "pending"
    assert [approval["approver"] for approval in body["approvals"]] == ["NA-1"]
    assert engine.is_national_administrator(target)
    url = f"/api/v1/access/admin-actions/{body['id']}/approve/"
    assert client_for(admin).post(url).status_code == 400  # the requester has approved already
    assert client_for(target).post(url).status_code == 403  # the one concerned never approves
    assert request_action(second, target).status_code == 400  # one pending request at a time

    approved = client_for(second).post(url)
    assert approved.status_code == 200, approved.content
    assert approved.json()["status"] == "executed"
    target.refresh_from_db()
    assert not target.is_active and not engine.is_national_administrator(target)
    actions = list(
        AuditEvent.objects.filter(target_type="administrator")
        .order_by("id")
        .values_list("action", flat=True)
    )
    assert actions == [
        "administrator.action_requested",
        "administrator.action_approved",
        "administrator.suspended",
    ]

    listing = client_for(admin).get("/api/v1/access/administrators/").json()
    states = {row["access_id"]: row["state"] for row in listing}
    assert states == {"NA-1": "active", "NA-2": "active", "NA-3": "suspended"}
    reinstated = client_for(second).post(
        "/api/v1/access/administrators/NA-3/reinstate/", {"reason": "Cleared"}, format="json"
    )
    assert reinstated.status_code == 200 and reinstated.json()["state"] == "active"
    target.refresh_from_db()
    assert engine.is_national_administrator(target)


def test_only_current_administrators_approvals_count(admin):
    second = administrator("NA-2", "Second Admin")
    third = administrator("NA-3", "Third Admin")
    target = administrator("NA-4", "Fourth Admin")
    request_id = request_action(admin, target).json()["id"]
    admin.is_active = False  # the requester loses their standing meanwhile
    admin.save()
    url = f"/api/v1/access/admin-actions/{request_id}/approve/"
    assert client_for(second).post(url).json()["status"] == "pending"
    assert client_for(third).post(url).json()["status"] == "executed"


def test_removal_needs_enough_administrators_and_revokes_the_appointment(admin):
    successor = member_account("08032220027", name="Successor")
    na_role = RoleDefinition.objects.get(code=Role.NATIONAL_ADMINISTRATOR)
    appointment = appoint(admin, successor, na_role, NATIONAL).json()["id"]
    # With two administrators nobody else could approve, so the request is refused.
    assert request_action(admin, successor.user, kind="remove").status_code == 400
    third = administrator("NA-3", "Third Admin")
    made = request_action(admin, successor.user, kind="remove").json()
    done = client_for(third).post(f"/api/v1/access/admin-actions/{made['id']}/approve/").json()
    assert done["status"] == "executed"
    assert RoleAssignment.objects.get(uuid=appointment).status == "revoked"
    assert not engine.is_national_administrator(successor.user)


def test_only_the_requester_cancels_a_pending_request(admin):
    second = administrator("NA-2", "Second Admin")
    target = administrator("NA-3", "Third Admin")
    request_id = request_action(admin, target).json()["id"]
    url = f"/api/v1/access/admin-actions/{request_id}/cancel/"
    assert client_for(second).post(url, {}, format="json").status_code == 403
    cancelled = client_for(admin).post(url, {"reason": "Resolved"}, format="json")
    assert cancelled.json()["status"] == "cancelled"
    assert request_action(second, target).status_code == 201  # a new request may follow


def test_recovery_adds_an_administrator_only_while_too_few_are_active(admin):
    candidate = member_account("08032220028", name="Recovered Admin")
    out = io.StringIO()
    reason = "Board decision of 11 October"
    call_command(
        "recover_administrator", member=candidate.membership_number, reason=reason, stdout=out
    )
    assert not engine.is_national_administrator(candidate.user)  # only checked
    call_command(
        "recover_administrator",
        member=candidate.membership_number,
        reason=reason,
        confirm=True,
        stdout=out,
    )
    assert engine.is_national_administrator(candidate.user)
    event = AuditEvent.objects.get(action="administrator.recovered")
    assert event.actor is None and event.provenance["authority"] == "recovery"

    administrator("NA-3", "Third Admin")  # now three are active
    another = member_account("08032220029", name="Another")
    with pytest.raises(CommandError):
        call_command(
            "recover_administrator",
            member=another.membership_number,
            reason=reason,
            confirm=True,
            stdout=out,
        )


# --- review findings: authority never acts upward or beyond its reach ------------


def test_a_subordinate_cannot_widen_or_restore_their_superior(admin):
    superior = member_account("08032220040", name="Superior")
    subordinate = member_account("08032220041", name="Subordinate")
    ceiling = [C.MANAGE_ASSIGNMENTS, C.ASSIGN_ROLES, C.SEND_BROADCAST, C.VIEW_INCIDENTS]
    assert (
        delegate(
            admin,
            superior,
            permissions=[*ceiling, C.DELEGATE_AUTHORITY],
            can_redelegate=True,
            depth=1,
            levels=["state"],
        ).status_code
        == 201
    )
    role = media_role(admin, "Desk", [C.SEND_BROADCAST, C.VIEW_INCIDENTS], levels=("state",))
    held = appoint(admin, superior, role, [KD], restricted_permissions=[C.SEND_BROADCAST])
    held_id = held.json()["id"]
    assert (
        delegate(superior.user, subordinate, permissions=ceiling, levels=["state"]).status_code
        == 201
    )

    client = client_for(subordinate.user)
    lift = client.post(
        f"/api/v1/access/assignments/{held_id}/restrict/", {"permissions": None}, format="json"
    )
    assert lift.status_code == 403
    assert not engine.allows(superior.user, C.VIEW_INCIDENTS, ScopeRef.resolve("state", "KD"))
    client_for(admin).post(
        f"/api/v1/access/assignments/{held_id}/suspend/", {"reason": "Audit"}, format="json"
    )
    restore = client.post(f"/api/v1/access/assignments/{held_id}/reactivate/", {}, format="json")
    assert restore.status_code == 403


def test_lifting_a_restriction_needs_authority_to_appoint(admin):
    manager_role = media_role(
        admin,
        "Desk Manager",
        [C.MANAGE_ASSIGNMENTS, C.SEND_BROADCAST, C.VIEW_INCIDENTS],
        levels=("state",),
    )
    manager = member_account("08032220042", name="Desk Manager")
    assert appoint(admin, manager, manager_role, [KD]).status_code == 201
    role = media_role(admin, "Desk", [C.SEND_BROADCAST, C.VIEW_INCIDENTS], levels=("state",))
    holder = member_account("08032220043", name="Desk Officer")
    narrow = appoint(admin, holder, role, [KD], restricted_permissions=[C.SEND_BROADCAST])
    url = f"/api/v1/access/assignments/{narrow.json()['id']}/restrict/"
    client = client_for(manager.user)
    assert client.post(url, {"permissions": None}, format="json").status_code == 403
    assert capabilities_for(holder.user) == {C.SEND_BROADCAST}
    narrowed = client.post(url, {"permissions": []}, format="json")
    assert narrowed.status_code == 200 and narrowed.json()["permissions"] == []


def test_role_changes_must_cover_everyone_holding_the_role(admin):
    creator_role = media_role(
        admin,
        "Kaduna Roles Officer",
        [C.MANAGE_ROLES, C.SEND_BROADCAST, C.VIEW_INCIDENTS],
        levels=("state",),
    )
    creator = member_account("08032220044", name="Kaduna Creator")
    assert appoint(admin, creator, creator_role, [KD]).status_code == 201
    made = client_for(creator.user).post(
        "/api/v1/access/roles/",
        {
            "name": "Desk Officer",
            "permissions": [C.SEND_BROADCAST],
            "permitted_levels": ["state"],
            "category": MEDIA,
        },
        format="json",
    )
    desk = RoleDefinition.objects.get(code=made.json()["code"])
    kano_holder = member_account("08032220045", name="Kano Holder")
    assert appoint(admin, kano_holder, desk, [KN]).status_code == 201

    client = client_for(creator.user)
    url = f"/api/v1/access/roles/{desk.code}/"
    widen = {"permissions": [C.SEND_BROADCAST, C.VIEW_INCIDENTS]}
    assert client.patch(url, widen, format="json").status_code == 403  # reaches Kano
    assert not engine.allows(kano_holder.user, C.VIEW_INCIDENTS, ScopeRef.resolve("state", "KN"))
    line = {"supervising_role": creator_role.code}
    assert client.patch(url, line, format="json").status_code == 403  # supervision over Kano
    archive = client.post(f"{url}archive/", {"reason": "x"}, format="json")
    assert archive.status_code == 403  # would cut off Kano
    assert client.patch(url, {"description": "Front desk"}, format="json").status_code == 200

    # What an administrator archived, only an administrator brings back.
    client_for(admin).post(f"{url}archive/", {"reason": "Merged"}, format="json")
    assert client.post(f"{url}restore/", {}, format="json").status_code == 403


def test_supervisors_lift_only_their_own_suspensions(admin):
    director_role = media_role(admin, "State Media Director", levels=("state",))
    officer_role = media_role(admin, "LGA Media Officer", supervising_role=director_role.code)
    director = member_account("08032220046", name="Media Director")
    appoint(admin, director, director_role, [KD])
    officer = appoint(admin, member_account("08032220047"), officer_role, [KD_LGA]).json()["id"]
    url = f"/api/v1/access/assignments/{officer}/"
    client_for(admin).post(f"{url}suspend/", {"reason": "Inquiry"}, format="json")
    client = client_for(director.user)
    assert client.post(f"{url}reactivate/", {}, format="json").status_code == 403
    client_for(admin).post(f"{url}reactivate/", {}, format="json")
    assert client.post(f"{url}suspend/", {"reason": "Absent"}, format="json").status_code == 200
    assert client.post(f"{url}reactivate/", {}, format="json").status_code == 200


def test_administrator_appointments_are_never_restricted(admin):
    na_role = RoleDefinition.objects.get(code=Role.NATIONAL_ADMINISTRATOR)
    member = member_account("08032220048", name="Half Admin")
    response = appoint(admin, member, na_role, NATIONAL, restricted_permissions=[C.VIEW_AUDIT])
    assert response.status_code == 400
    assert not engine.is_national_administrator(member.user)


def test_a_suspended_member_cannot_revoke_what_they_granted(admin):
    first = member_account("08032220049", name="First Coordinator")
    second = member_account("08032220050", name="Second Coordinator")
    delegate(admin, first, permissions=POWERS, can_redelegate=True, depth=1)
    lower = delegate(first.user, second).json()["id"]
    first.membership_state = "suspended"
    first.save()
    revoke = client_for(first.user).post(
        f"/api/v1/access/delegations/{lower}/revoke/", {"reason": "x"}, format="json"
    )
    assert revoke.status_code == 403
    assert DelegationGrant.objects.get(uuid=lower).is_effective()


def test_one_person_with_two_accounts_counts_once(admin):
    """An administrator account and a member account with the same phone are one person."""
    phone = "08032220051"
    twin = User.objects.create_user(
        "NA-TWIN", PASSWORD, full_name="Twin", role=Role.NATIONAL_ADMINISTRATOR, phone_number=phone
    )
    own_member = member_account(phone, name="Twin")
    na_role = RoleDefinition.objects.get(code=Role.NATIONAL_ADMINISTRATOR)
    assert appoint(twin, own_member, na_role, NATIONAL).status_code == 403  # oneself
    assert appoint(admin, own_member, na_role, NATIONAL).status_code == 201
    target = administrator("NA-3", "Third Admin")
    request_id = request_action(twin, target).json()["id"]
    url = f"/api/v1/access/admin-actions/{request_id}/approve/"
    assert client_for(own_member.user).post(url).status_code == 400  # the same person again
    assert client_for(admin).post(url).json()["status"] == "executed"


def test_only_an_administrator_puts_delegation_into_a_role(admin):
    """Below the administrators, further delegation is always a counted grant."""
    holder_role = media_role(
        admin,
        "State Delegator",
        [C.ASSIGN_ROLES, C.MANAGE_ROLES, C.DELEGATE_AUTHORITY, C.SEND_BROADCAST],
        levels=("state",),
    )
    holder = member_account("08032220052", name="State Delegator")
    assert appoint(admin, holder, holder_role, [KD]).status_code == 201
    made = client_for(holder.user).post(
        "/api/v1/access/roles/",
        {
            "name": "Deputy Delegator",
            "permissions": [C.ASSIGN_ROLES, C.DELEGATE_AUTHORITY],
            "permitted_levels": ["lga"],
            "category": MEDIA,
        },
        format="json",
    )
    assert made.status_code == 403
    assert delegate(holder.user, member_account("08032220053")).status_code == 201

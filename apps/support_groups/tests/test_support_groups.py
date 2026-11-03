"""Support groups (3 November 2026): organizations allied with TGCG, vetted
nationally, whose leaders manage their own members. Who is in a group shows
only to the member, the leader and those who vet groups."""

import pytest
from django.contrib.auth import authenticate

from apps.access.models import AuditEvent
from apps.access.tests.test_access import appoint, client_for, custom_role, member_account
from apps.accounts.models import User
from apps.core.choices import RegistrationChannel, VerificationStatus
from apps.core.permissions import Capability
from apps.membership.models import Member
from apps.messaging.models import Broadcast
from apps.support_groups import services
from apps.support_groups.models import GroupMember, SupportGroup
from apps.support_groups.tests.conftest import GROUPS, MINE, register
from conftest import PASSWORD

pytestmark = pytest.mark.django_db
C = Capability
INBOX = "/api/v1/me/messages/"


def add(user, group, *people, consent=True):
    return client_for(user).post(
        f"/api/v1/my-groups/{group.uuid}/add/",
        {"people": list(people), "consent": consent},
        format="json",
    )


def person(name, phone, **extra):
    return {"full_name": name, "phone_number": phone, **extra}


def roster(user, group):
    response = client_for(user).get(f"/api/v1/my-groups/{group.uuid}/members/")
    assert response.status_code == 200, response.content
    return response.json()["results"]


def inbox_titles(member):
    return [item["title"] for item in client_for(member.user).get(INBOX).json()]


# --- registering and vetting ----------------------------------------------------------


def test_a_member_registers_an_organization_they_lead(admin, leader):
    response = register(leader.user)
    assert response.status_code == 201, response.content
    body = response.json()
    assert body["serial_number"] == "TGCG/0001/BAT/27"
    assert body["status"] == "pending" and body["coverage_label"] == "State"
    assert body["state"] == "Kaduna" and body["leader"]["full_name"] == "Halima Sani"
    assert body["officials"] == [
        {"name": "Bala Musa", "position": "Secretary", "phone": "08035550009"}
    ]
    # Nothing to share before TGCG vets it.
    assert body["join_code"] is None and body["link"] is None
    assert "Organization registered" in inbox_titles(leader)
    second = register(leader.user, organization_name="Second Group")
    assert second.json()["serial_number"] == "TGCG/0002/BAT/27"

    mine = client_for(leader.user).get(MINE).json()
    assert [item["name"] for item in mine["leading"]] == [
        "Second Group",
        "Arewa Youth for Progress",
    ]
    assert mine["vets"] is False


def test_the_form_says_what_is_missing(leader):
    response = register(leader.user, organization_name="", organization_state="")
    assert response.status_code == 400
    assert set(response.json()) >= {"organization_name", "organization_state"}
    regional = register(leader.user, organization_coverage="regional")
    assert "organization_zone" in regional.json()
    bad_phone = register(
        leader.user, organization_officials=[{"name": "Bala Musa", "phone": "12345"}]
    )
    assert "organization_officials" in bad_phone.json()
    assert not SupportGroup.objects.exists()


def test_only_those_who_vet_groups_nationwide_see_and_decide(admin, leader):
    response = register(leader.user)
    group = SupportGroup.objects.get(uuid=response.json()["id"])
    plain = member_account("08035550002", name="Plain Member")
    assert client_for(plain.user).get(GROUPS).status_code == 403
    assert client_for(leader.user).post(f"{GROUPS}{group.uuid}/approve/").status_code == 403

    state_role = custom_role(admin, "State Groups Desk", [C.MANAGE_SUPPORT_GROUPS])
    officer = member_account("08035550003", name="State Officer")
    assert (
        appoint(admin, officer, state_role, [{"level": "state", "stateId": "KD"}]).status_code
        == 201
    )
    assert client_for(officer.user).get(GROUPS).status_code == 403

    national = custom_role(
        admin, "National Secretary", [C.MANAGE_SUPPORT_GROUPS], levels=("country",)
    )
    secretary = member_account("08035550004", name="Aisha Bello")
    assert appoint(admin, secretary, national, [{"level": "country"}]).status_code == 201
    listed = client_for(secretary.user).get(GROUPS).json()["results"]
    assert [item["name"] for item in listed] == ["Arewa Youth for Progress"]
    assert listed[0]["registered_by"] == "Halima Sani" and listed[0]["members"] == 1


def test_approving_gives_the_group_a_code_and_link(admin, leader):
    response = register(leader.user)
    group = SupportGroup.objects.get(uuid=response.json()["id"])
    approved = client_for(admin).post(f"{GROUPS}{group.uuid}/approve/")
    assert approved.status_code == 200 and approved.json()["status"] == "approved"
    led = client_for(leader.user).get(f"/api/v1/my-groups/{group.uuid}/").json()
    assert len(led["join_code"]) == 7
    leader.refresh_from_db()
    assert led["link"].endswith(f"/?ref={leader.referral_code}&group={led['join_code']}")
    assert "Arewa Youth for Progress is approved" in inbox_titles(leader)
    assert AuditEvent.objects.filter(action="supportGroup.approved").exists()
    # Approved once only.
    assert client_for(admin).post(f"{GROUPS}{group.uuid}/approve/").status_code == 400


def test_turning_down_and_suspending_need_a_reason(admin, group, leader):
    assert client_for(admin).post(f"{GROUPS}{group.uuid}/suspend/", {}).status_code == 400
    suspended = client_for(admin).post(f"{GROUPS}{group.uuid}/suspend/", {"reason": "Inactive"})
    assert suspended.json()["status"] == "suspended"
    # A suspended group's leader can add nobody.
    assert add(leader.user, group, person("New Person", "08035550100")).status_code == 400
    restored = client_for(admin).post(f"{GROUPS}{group.uuid}/restore/")
    assert restored.json()["status"] == "approved"

    other = SupportGroup.objects.get(
        uuid=register(leader.user, organization_name="Another Group").json()["id"]
    )
    rejected = client_for(admin).post(f"{GROUPS}{other.uuid}/reject/", {"reason": "Duplicate"})
    assert rejected.json()["status"] == "rejected"
    assert rejected.json()["decision_reason"] == "Duplicate"


# --- the leader's members ---------------------------------------------------------------


def test_new_people_get_an_account_with_their_phone_as_pin(group, leader):
    response = add(
        leader.user,
        group,
        person("Usman Garba", "0803 555 0101", line=1, scope={"level": "lga", "lgaId": "KD-ZARIA"}),
        person("Zainab Ali", "08035550102", line=2, gender="female"),
    )
    assert response.status_code == 200, response.content
    assert [row["outcome"] for row in response.json()["results"]] == ["registered", "registered"]

    usman = Member.objects.get(phone_number="+2348035550101")
    assert usman.created_by == leader.user
    assert usman.registration_channel == RegistrationChannel.REFERRED
    assert usman.lga.code == "KD-ZARIA"
    assert usman.verification_status == VerificationStatus.UNVERIFIED
    assert authenticate(username="08035550101", password="08035550101") == usman.user
    assert Member.objects.get(phone_number="+2348035550102").gender == "female"
    assert "Welcome to TGCG" in inbox_titles(usman)

    rows = roster(leader.user, group)
    assert {row["full_name"] for row in rows} == {"Halima Sani", "Usman Garba", "Zainab Ali"}
    usman_row = next(row for row in rows if row["full_name"] == "Usman Garba")
    # Only what the leader needs: no PVC, photos, birthday or disability.
    assert set(usman_row) == {
        "id",
        "full_name",
        "phone_number",
        "place",
        "approved",
        "status",
        "status_label",
        "via",
        "via_label",
        "created_at",
        "answered_at",
    }
    assert usman_row["place"] == "Zaria, Kaduna" and usman_row["approved"] is False


def test_people_already_registered_are_asked_in_the_app(group, leader):
    existing = member_account("08035550201", name="Ibrahim Yusuf")
    response = add(leader.user, group, person("Ibrahim Y.", "08035550201"))
    assert response.json()["results"][0]["outcome"] == "asked"
    assert "Arewa Youth for Progress lists you as a member" in inbox_titles(existing)

    # Until they say yes, the leader sees only the name and number it gave.
    waiting = next(row for row in roster(leader.user, group) if row["status"] == "invited")
    assert waiting["full_name"] == "Ibrahim Y." and waiting["place"] == ""
    assert waiting["approved"] is None

    mine = client_for(existing.user).get(MINE).json()
    assert [item["group"]["name"] for item in mine["invitations"]] == ["Arewa Youth for Progress"]
    place = mine["invitations"][0]["id"]
    said = client_for(existing.user).post(f"{MINE}{place}/answer/", {"yes": True}, format="json")
    assert said.status_code == 200 and said.json()["status"] == "active"
    shown = next(row for row in roster(leader.user, group) if row["id"] == place)
    assert shown["full_name"] == "Ibrahim Yusuf" and shown["approved"] is True

    # Asked once: adding them again changes nothing.
    again = add(leader.user, group, person("Ibrahim Y.", "08035550201"))
    assert again.json()["results"][0]["outcome"] == "already"


def test_a_no_is_respected(group, leader):
    existing = member_account("08035550202", name="Grace Obi")
    add(leader.user, group, person("Grace", "08035550202"))
    place = client_for(existing.user).get(MINE).json()["invitations"][0]["id"]
    client_for(existing.user).post(f"{MINE}{place}/answer/", {"yes": False}, format="json")
    assert [row["status"] for row in roster(leader.user, group)] == ["active"]
    again = add(leader.user, group, person("Grace", "08035550202"))
    assert again.json()["results"][0]["outcome"] == "declined"
    # Answered once.
    response = client_for(existing.user).post(
        f"{MINE}{place}/answer/", {"yes": True}, format="json"
    )
    assert response.status_code == 400


def test_problems_are_reported_line_by_line(group, leader):
    staff = User.objects.create_user(
        "STAFF-1", PASSWORD, full_name="Staff Person", phone_number="+2348035550301"
    )
    assert staff.pk
    response = add(
        leader.user,
        group,
        person("Bad Number", "12345", line=4),
        person("Staff Person", "08035550301", line=5),
    )
    results = response.json()["results"]
    assert [(row["line"], row["outcome"]) for row in results] == [(4, "problem"), (5, "problem")]
    assert "staff account" in results[1]["detail"]


def test_the_leader_confirms_people_agreed(group, leader):
    response = add(leader.user, group, person("Usman Garba", "08035550101"), consent=False)
    assert response.status_code == 400 and "consent" in response.json()
    assert not Member.objects.filter(phone_number="+2348035550101").exists()


def test_batches_are_limited(group, leader):
    people = [person(f"Person Number {n}", f"0803556{n:04d}") for n in range(26)]
    assert add(leader.user, group, *people).status_code == 400


def test_nobody_else_sees_or_changes_the_group(group, leader, admin):
    other = member_account("08035550401", name="Other Member")
    assert client_for(other.user).get(f"/api/v1/my-groups/{group.uuid}/members/").status_code == 404
    assert add(other.user, group, person("Someone", "08035550402")).status_code == 404
    # Those who vet groups see everyone in it, with membership numbers.
    rows = client_for(admin).get(f"{GROUPS}{group.uuid}/members/").json()["results"]
    assert rows[0]["membership_number"] == leader.membership_number
    # A member's own record says nothing of their groups.
    card = client_for(leader.user).get("/api/v1/me/member/").json()
    assert not any("group" in key for key in card)


def test_the_leader_removes_people_but_not_themselves(group, leader):
    add(leader.user, group, person("Usman Garba", "08035550101"))
    rows = roster(leader.user, group)
    usman = next(row for row in rows if row["full_name"] == "Usman Garba")
    own = next(row for row in rows if row["full_name"] == "Halima Sani")
    url = f"/api/v1/my-groups/{group.uuid}/members/"
    assert client_for(leader.user).post(f"{url}{own['id']}/remove/").status_code == 400
    removed = client_for(leader.user).post(f"{url}{usman['id']}/remove/")
    assert removed.status_code == 200 and removed.json()["status"] == "removed"
    assert [row["full_name"] for row in roster(leader.user, group)] == ["Halima Sani"]


# --- the member's own side -----------------------------------------------------------------


def test_members_join_with_the_code_and_leave(group, leader):
    joiner = member_account("08035550501", name="Musa Danjuma")
    wrong = client_for(joiner.user).post(f"{MINE}join/", {"code": "0000000"}, format="json")
    assert wrong.status_code == 400 and "group_code" in wrong.json()
    joined = client_for(joiner.user).post(f"{MINE}join/", {"code": group.join_code}, format="json")
    assert joined.status_code == 201, joined.content
    mine = client_for(joiner.user).get(MINE).json()
    assert [item["group"]["name"] for item in mine["groups"]] == ["Arewa Youth for Progress"]

    left = client_for(joiner.user).post(f"{MINE}{joined.json()['id']}/leave/")
    assert left.status_code == 200 and left.json()["status"] == "left"
    assert client_for(joiner.user).get(MINE).json()["groups"] == []
    # Having left, they are not added back from a list.
    again = add(leader.user, group, person("Musa", "08035550501"))
    assert again.json()["results"][0]["outcome"] == "left"
    # The leader cannot leave the group they lead.
    own = client_for(leader.user).get(MINE).json()["groups"][0]["id"]
    assert client_for(leader.user).post(f"{MINE}{own}/leave/").status_code == 400


def test_a_group_registered_for_someone_already_a_member_waits_for_their_yes(admin):
    officer = member_account("08035550601", name="Mobilization Officer")
    named = member_account("08035550602", name="Chief Adamu")
    response = register(officer.user, leader_full_name="Adamu", leader_phone_number="08035550602")
    assert response.status_code == 201, response.content
    group = SupportGroup.objects.get(uuid=response.json()["id"])
    assert response.json()["leader_confirmed"] is False
    assert client_for(admin).post(f"{GROUPS}{group.uuid}/approve/").status_code == 400

    invitation = client_for(named.user).get(MINE).json()["invitations"][0]
    assert invitation["leads"] is True
    client_for(named.user).post(f"{MINE}{invitation['id']}/answer/", {"yes": True}, format="json")
    assert client_for(admin).post(f"{GROUPS}{group.uuid}/approve/").status_code == 200


def test_a_named_leader_who_says_no_ends_the_application():
    officer = member_account("08035550603", name="Mobilization Officer")
    named = member_account("08035550604", name="Chief Bello")
    group_id = register(
        officer.user, leader_full_name="Bello", leader_phone_number="08035550604"
    ).json()["id"]
    invitation = client_for(named.user).get(MINE).json()["invitations"][0]
    client_for(named.user).post(f"{MINE}{invitation['id']}/answer/", {"yes": False}, format="json")
    assert SupportGroup.objects.get(uuid=group_id).status == SupportGroup.Status.REJECTED


def test_a_new_leader_is_registered_with_the_group(admin):
    officer = member_account("08035550605", name="Mobilization Officer")
    refused = register(
        officer.user, leader_full_name="Fatima Lawal", leader_phone_number="08035550606"
    )
    assert refused.status_code == 400 and "consent" in refused.json()
    response = register(
        officer.user,
        leader_full_name="Fatima Lawal",
        leader_phone_number="08035550606",
        consent=True,
    )
    assert response.status_code == 201, response.content
    fatima = Member.objects.get(phone_number="+2348035550606")
    assert fatima.created_by == officer.user and response.json()["leader_confirmed"] is True
    assert SupportGroup.objects.get().leader == fatima


# --- messages and counting -------------------------------------------------------------


def test_the_leader_messages_the_group_in_the_app(group, leader):
    existing = member_account("08035550701", name="Joy Eze")
    add(leader.user, group, person("Usman Garba", "08035550101"), person("Joy", "08035550701"))
    url = f"/api/v1/my-groups/{group.uuid}/message/"
    sent = client_for(leader.user).post(url, {"title": "Meeting", "body": "Saturday at 4."})
    assert sent.status_code == 201 and sent.json() == {"people": 2}  # Joy has not said yes yet
    usman = Member.objects.get(phone_number="+2348035550101")
    assert "Arewa Youth for Progress: Meeting" in inbox_titles(usman)
    assert "Arewa Youth for Progress: Meeting" not in inbox_titles(existing)
    message = Broadcast.objects.get(audience__supportGroup=str(group.uuid))
    assert message.kind == Broadcast.Kind.NOTICE and message.channels == ["app"]

    for _ in range(services.MESSAGES_PER_DAY - 1):
        client_for(leader.user).post(url, {"title": "Again", "body": "Reminder."})
    assert (
        client_for(leader.user).post(url, {"title": "More", "body": "Too many."}).status_code == 400
    )
    other = member_account("08035550702", name="Not The Leader")
    assert client_for(other.user).post(url, {"title": "Hi", "body": "Hello"}).status_code == 404


def test_people_in_two_groups_are_counted_once(admin, group, leader):
    second_leader = member_account("08035550801", name="Second Leader")
    second = SupportGroup.objects.get(
        uuid=register(second_leader.user, organization_name="Women for Tomorrow").json()["id"]
    )
    client_for(admin).post(f"{GROUPS}{second.uuid}/approve/")
    add(leader.user, group, person("Usman Garba", "08035550101"))
    usman = Member.objects.get(phone_number="+2348035550101")
    client_for(usman.user).post(
        f"{MINE}join/", {"code": SupportGroup.objects.get(pk=second.pk).join_code}, format="json"
    )

    totals = client_for(admin).get(f"{GROUPS}totals/").json()
    assert totals["groups"]["approved"] == 2
    # Halima, Second Leader and Usman: three people, though Usman is in both.
    assert totals["people"] == 3
    assert totals["approved_people"] == 2
    listed = {item["name"]: item for item in client_for(admin).get(GROUPS).json()["results"]}
    assert listed["Arewa Youth for Progress"]["members"] == 2
    assert listed["Women for Tomorrow"]["members"] == 2
    assert GroupMember.objects.filter(member=usman, status="active").count() == 2

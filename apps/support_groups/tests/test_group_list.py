"""A group's own members' list (4 November 2026): reading the PDF's rows,
keeping only VINs and phones that are a person's own, placing people in the
group, and members without a phone signing in with their VIN and choosing
their own PIN first. Every name and number here is made up."""

import json

import pytest
from django.core.management import call_command
from rest_framework.test import APIClient

from apps.access.tests.test_access import client_for, member_account
from apps.accounts.models import User
from apps.membership.models import Member
from apps.support_groups import group_list
from apps.support_groups.group_list import Person, read_line
from apps.support_groups.models import GroupMember
from apps.support_groups.places import Places

pytestmark = pytest.mark.django_db

VIN_A = "90F5B0E8D0295847263"
VIN_B = "90F5B0E8D0295847264"
VIN_C = "90F5B0E8D0295847265"
SOURCE = "Directorate list, 4 November 2026"


def line(serial, name, *columns):
    return f" {serial}     {name}" + "".join(f"     {col}" for col in columns)


def test_rows_are_read_by_their_columns_and_bank_details_are_not():
    row = read_line(
        3,
        line(
            12,
            "MUSA BELLO",
            "Kaduna",
            "Zaria",
            "Kufena",
            "18-23-1-1",
            "90F5 B0E8 D029 5847 263",
            "0803 123 4567",
            "0123456789",
            "Kuda Bank",
        ),
    )
    assert (row.page, row.serial, row.name) == (3, 12, "Musa Bello")
    assert row.places == ["Kaduna", "Zaria", "Kufena"] and row.unit == "18-23-01-001"
    assert row.vin == VIN_A and row.phone == "+2348031234567"
    assert "Kuda" not in str(row) and "0123456789" not in str(row)

    abroad = read_line(4, line(13, "Ade Obi", "+234 805 555 0101"))
    assert abroad.phone == "+2348055550101" and abroad.vin == ""
    short = read_line(4, line(14, "Ade Obi", "90F5B0E8D02958472"))
    assert short.code and short.vin == ""
    assert read_line(4, "SN     NAME     STATE") is None
    assert read_line(4, line(15, "1234")) is None
    # The PDF's ligatures read as letters.
    assert read_line(5, line(16, "Haﬁzu Sani", VIN_C)).name == "Hafizu Sani"


def test_only_a_persons_own_vin_or_phone_counts():
    rows = [
        read_line(1, line(1, "Musa Bello", VIN_A)),
        read_line(1, line(2, "Musa Bello", VIN_A, "08031234567")),  # listed again
        read_line(1, line(3, "Aisha Sani", VIN_B)),
        read_line(1, line(4, "Bala Umar", VIN_B)),  # one VIN, two names
        read_line(1, line(5, "Grace Obi", "08035550001")),
        read_line(1, line(6, "Ibrahim Ali", "08035550001")),  # one phone, two names
        read_line(1, line(7, "Zainab Yusuf", "90F5B0E8D02958472")),  # 17 characters
        read_line(1, line(8, "Halima Musa")),
        read_line(1, line(9, "Halima Musa", VIN_C, "08035550001")),  # own VIN, shared phone
    ]
    people, held = group_list.plan(rows)
    assert [(p.name, p.vin, p.phone, p.rows) for p in people] == [
        ("Musa Bello", VIN_A, "+2348031234567", [[1, 1], [1, 2]]),
        ("Halima Musa", VIN_C, "", [[1, 9]]),
    ]
    assert [(row.serial, reason) for row, reason in held] == [
        (3, "VIN shared with 1 other names"),
        (4, "VIN shared with 1 other names"),
        (5, "phone shared with 2 other names"),
        (6, "phone shared with 2 other names"),
        (7, "VIN column has 17 characters; a VIN has 19"),
        (8, "no VIN or phone"),
    ]
    assert group_list.from_json(json.loads(json.dumps(group_list.to_json(people)))) == people


def place(admin, group, person):
    return group_list.place_person(
        actor=admin, group=group, person=person, places=Places(), source=SOURCE
    )


def test_new_people_are_registered_in_the_group(admin, group):
    with_phone = Person("Musa Bello", vin=VIN_A, phone="+2348031234567", state="Kaduna")
    assert place(admin, group, with_phone) == "registered with the phone"
    member = Member.objects.get(vin=VIN_A)
    assert member.phone_number == "+2348031234567" and member.state.code == "KD"
    assert member.registration_channel == "import" and member.verification_status == "unverified"
    assert member.user.check_password("08031234567") and not member.user.must_change_password
    in_group = GroupMember.objects.get(group=group, member=member)
    assert (in_group.status, in_group.via) == ("active", "list")

    # Without a phone, more than one member may have none.
    for vin, name in ((VIN_B, "Aisha Sani"), (VIN_C, "Bala Umar")):
        assert place(admin, group, Person(name, vin=vin)) == "registered with the VIN"
    phoneless = Member.objects.filter(phone_number__isnull=True)
    assert phoneless.count() == 2
    assert all(m.user.must_change_password and m.user.phone_number is None for m in phoneless)


def test_without_a_phone_the_vin_signs_in_and_a_pin_comes_first(admin, group):
    place(admin, group, Person("Aisha Sani", vin=VIN_B))
    client = APIClient()
    login = client.post(
        "/api/v1/auth/login/",
        {"username": "90f5 b0e8 d029 5847 264", "password": VIN_B},
        format="json",
    )
    assert login.status_code == 200, login.content
    assert login.json()["user"]["must_change_pin"] is True
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {login.json()['access']}")

    blocked = client.get("/api/v1/me/member/")
    assert blocked.status_code == 403 and blocked.json()["code"] == "pin_change_required"
    assert client.get("/api/v1/auth/me/").json()["must_change_pin"] is True
    changed = client.post(
        "/api/v1/auth/password/",
        {"current_password": VIN_B, "new_password": "482915"},
        format="json",
    )
    assert changed.status_code == 200, changed.content
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {changed.json()['access']}")
    assert client.get("/api/v1/me/member/").status_code == 200
    assert client.get("/api/v1/auth/me/").json()["must_change_pin"] is False
    again = APIClient().post(
        "/api/v1/auth/login/", {"username": VIN_B, "password": VIN_B}, format="json"
    )
    assert again.status_code == 401


def test_members_already_registered_are_placed_at_once_and_told(admin, group):
    by_phone = member_account("08035550002", name="Grace Obi")
    assert place(admin, group, Person("Grace Obi", phone="+2348035550002")) == (
        "already a member, placed in the group"
    )
    assert GroupMember.objects.get(group=group, member=by_phone).via == "list"
    inbox = client_for(by_phone.user).get("/api/v1/me/messages/").json()
    assert [item["title"] for item in inbox][0] == f"You are in {group.name}"
    assert place(admin, group, Person("Grace Obi", phone="+2348035550002")) == (
        "already in the group"
    )
    assert Member.objects.filter(full_name="Grace Obi").count() == 1

    other = member_account("08035550003", name="Ibrahim Ali")
    Member.objects.filter(pk=other.pk).update(vin=VIN_C)
    two = Person("Ibrahim Ali", vin=VIN_C, phone="+2348035550002")
    assert place(admin, group, two).startswith("held: the VIN and the phone belong to two")


def test_the_command_places_people_in_shards(admin, group, tmp_path):
    people = tmp_path / "people.json"
    report = tmp_path / "report.csv"
    found = [
        Person("Musa Bello", vin=VIN_A, rows=[[1, 1]]),
        Person("Aisha Sani", vin=VIN_B, rows=[[1, 2]]),
    ]
    people.write_text(json.dumps(group_list.to_json(found)), encoding="utf-8")
    common = {
        "group": group.serial_number,
        "by": admin.access_id,
        "source": SOURCE,
        "report": str(report),
    }
    call_command("import_group_list", str(people), **common)
    assert not Member.objects.filter(vin__in=[VIN_A, VIN_B]).exists()  # a dry run
    for shard in ("0/2", "1/2"):
        call_command("import_group_list", str(people), shard=shard, commit=True, **common)
    assert Member.objects.filter(vin__in=[VIN_A, VIN_B]).count() == 2
    assert sorted(report.read_text().splitlines()) == [
        "1,1,registered with the VIN",
        "1,2,registered with the VIN",
    ]
    leader = client_for(group.leader.user).get(f"/api/v1/my-groups/{group.uuid}/members/")
    assert {row["full_name"] for row in leader.json()["results"]} >= {"Musa Bello", "Aisha Sani"}
    with pytest.raises(Exception, match="National Administrator"):
        call_command("import_group_list", str(people), **{**common, "by": "NOBODY"})
    assert User.objects.filter(must_change_password=True).count() == 2

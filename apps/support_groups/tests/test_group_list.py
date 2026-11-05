"""A group's own members' list (4 November 2026): reading the PDF's rows,
keeping only VINs, phones and NINs that are a person's own, placing people
in the group, and members without a phone signing in with their VIN or NIN
and choosing their own PIN first. Every name and number here is made up."""

import json

import pytest
from django.core.management import call_command
from rest_framework.test import APIClient

from apps.access.models import AuditEvent
from apps.access.tests.test_access import client_for, member_account
from apps.accounts.models import User
from apps.core.models import Sequence
from apps.membership import registration
from apps.membership.models import Member
from apps.support_groups import group_list, table_list
from apps.support_groups.group_list import Person, read_line
from apps.support_groups.management.commands.read_group_list import _signs_in
from apps.support_groups.models import GroupMember
from apps.support_groups.places import Places

pytestmark = pytest.mark.django_db

VIN_A = "90F5B0E8D0295847263"
VIN_B = "90F5B0E8D0295847264"
VIN_C = "90F5B0E8D0295847265"
NIN_A = "12345678901"
NIN_B = "12345678902"
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
        (8, "no VIN, phone or NIN"),
    ]
    assert group_list.from_json(json.loads(json.dumps(group_list.to_json(people)))) == people


# A spreadsheet's header printed to PDF: each column's left edge (5 November 2026).
HEADER = [
    (51.5, "S/NO2"),
    (70.9, "CODE NUMBER"),
    (119.1, "FULL NAMES"),
    (177.5, "DISABILITY"),
    (225.7, "GENDER"),
    (252.7, "OCCUPATION"),
    (300.9, "LOCAL GOVERNMENT"),
    (355.7, "WARD"),
    (403.9, "PHONE NUMBER"),
    (452.1, "NIN"),
    (500.3, "HIGHEST QUALIFICATION"),
]


def cells_at(y, *values):
    """A row's pieces of text, one per cell; S/N right-aligned in its column."""
    lefts = [60.2] + [left for left, _ in HEADER[1:]]
    return [(y, left, value) for left, value in zip(lefts, values, strict=False) if value]


def test_a_spreadsheets_table_is_read_by_where_its_cells_stand():
    first = [(733.4, 51.5, "LIST OF PERSONS WITH DISABILITIES")]
    first += [(721.0, left, text) for left, text in HEADER]
    first += cells_at(
        713.3,
        "1",
        "KD-01",
        "MUSA BELLO",
        "SPINAL CORD INJUR",
        "MALE",
        "FARMER",
        "ZARIA",
        "Kufena",
        "7031234567",
        "No",
        "SSCE",
    )
    first += cells_at(
        705.7,
        "2",
        "KD-02",
        "Hauwa",
        "DEAF",
        "FEMAL",
        "FARMER",
        "ZARIA",
        "Kufena",
        "0803 555 0101",
        NIN_A,
        "SSCE",
    )
    first.append((702.0, 119.1, "Sani"))  # the name cell's wrapped line
    # Cells moved one column right as far as GENDER, as the last rows of a list.
    second = cells_at(
        733.4,
        "3",
        "5/10/24 23:08",
        "KD-ZAR -23-03",
        "Ladi Musa",
        "LEPER",
        "FARMER",
        "ZARIA",
        "ZARIA",
        "",
        NIN_B,
        "MSC",
    )
    rows = table_list.read_pages([first, second], state="Kaduna")
    assert [(r.page, r.serial, r.name) for r in rows] == [
        (1, 1, "Musa Bello"),
        (1, 2, "Hauwa Sani"),
        (2, 3, "Ladi Musa"),
    ]
    musa, hauwa, ladi = rows
    assert musa.places == ["Kaduna", "ZARIA", "Kufena"]
    assert (musa.phone, musa.nin, musa.gender) == ("+2347031234567", "", "male")
    assert (musa.disabilities, musa.occupation) == (["physical"], "Farmer")
    assert (hauwa.phone, hauwa.nin, hauwa.gender) == ("+2348035550101", NIN_A, "female")
    assert hauwa.disabilities == ["hearing"]
    assert (ladi.disabilities, ladi.disability_other, ladi.gender) == (["other"], "Leprosy", "")
    assert (ladi.phone, ladi.nin, ladi.places[1]) == ("", NIN_B, "ZARIA")
    assert "KD-0" not in str(rows) and "SSCE" not in str(rows)


def test_a_nin_counts_when_it_is_their_own():
    def row(serial, name, phone="", nin="", **more):
        return group_list.Row(page=1, serial=serial, name=name, phone=phone, nin_column=nin, **more)

    rows = [
        row(1, "Musa Bello", "+2348031234567", NIN_A, gender="male", disabilities=["visual"]),
        row(2, "Musa Bello", "+2348031234567", NIN_A),  # listed twice
        row(3, "Hauwa Sani", "+2348035550001", NIN_B),
        row(4, "Ladi Musa", "+2348035550001", "12345678903"),  # one phone, two names
        row(5, "Kabiru Ali", "+2348035550004", "12345678904"),
        row(6, "Kabiru Ali", "+2348035550005", "12345678904"),  # two phones, one NIN
        row(7, "Bala Umar", nin="1234567890"),
        row(8, "Sani Bello", nin="08031234599"),
        row(9, "Zainab Yusuf"),
        row(10, "", "+2348035550010"),
        row(11, "Ade Obi", phone_column="0810587467", nin="195758976673"),
        row(12, "Ladi Bala", nin="07212345678"),  # sign-in reads it as a phone
        row(13, "Yusuf Ali", "+2348035550013", "12345678913"),
        row(14, "Yusuf Ali", "+2348035550014", "12345678914"),
        row(15, "Yusuf Ali", "+2348035550014", "12345678913"),  # each with another row
    ]
    people, held = group_list.plan(rows)
    assert [(p.name, p.phone, p.nin, p.rows) for p in people] == [
        ("Musa Bello", "+2348031234567", NIN_A, [[1, 1], [1, 2]]),
        ("Hauwa Sani", "", NIN_B, [[1, 3]]),
        ("Ladi Musa", "", "12345678903", [[1, 4]]),
        ("Kabiru Ali", "+2348035550004", "12345678904", [[1, 5]]),
        ("Kabiru Ali", "+2348035550005", "", [[1, 6]]),
        ("Yusuf Ali", "+2348035550013", "12345678913", [[1, 13]]),
        ("Yusuf Ali", "+2348035550014", "12345678914", [[1, 14]]),
    ]
    assert (people[0].gender, people[0].disabilities) == ("male", ["visual"])
    # A shared phone stays with them, to find them if registered with it before.
    assert [p.other_phones for p in people[:3]] == [[], ["+2348035550001"], ["+2348035550001"]]
    assert [(row.serial, reason) for row, reason in held] == [
        (7, "NIN column has 10 digits; a NIN has 11"),
        (8, "NIN column holds a phone number"),
        (9, "no VIN, phone or NIN"),
        (10, "no name"),
        (
            11,
            "phone column is not a Nigerian mobile number + NIN column has 12 digits; a NIN has 11",
        ),
        (12, "NIN column holds a phone number"),
        (15, "phone and NIN each with another row of this name"),
    ]
    assert group_list.from_json(json.loads(json.dumps(group_list.to_json(people)))) == people
    assert [_signs_in(p) for p in people[:2]] == ["phone only", "NIN only"]
    assert _signs_in(Person("Nobody")) == "nothing to sign in with"


def place(admin, group, person):
    return group_list.place_person(
        actor=admin, group=group, person=person, places=Places(), source=SOURCE
    )


def test_with_neither_phone_nor_vin_the_nin_signs_in_and_a_pin_comes_first(admin, group):
    hauwa = Person(
        "Hauwa Sani",
        nin=NIN_A,
        state="Kaduna",
        gender="female",
        disabilities=["other"],
        disability_other="Leprosy",
        occupation="Farmer",
    )
    assert place(admin, group, hauwa) == "registered with the NIN"
    member = Member.objects.get(nin=NIN_A)
    assert member.phone_number is None and member.vin is None and member.state.code == "KD"
    assert (member.gender, member.disabilities, member.disability_other, member.occupation) == (
        "female",
        ["other"],
        "Leprosy",
        "Farmer",
    )
    assert member.user.must_change_password and member.user.check_password(NIN_A)
    assert NIN_A not in str(list(AuditEvent.objects.values_list("changes", flat=True)))

    login = APIClient().post(
        "/api/v1/auth/login/", {"username": "1234 5678 901", "password": NIN_A}, format="json"
    )
    assert login.status_code == 200, login.content
    assert login.json()["user"]["must_change_pin"] is True

    # Kept only to sign in: someone with a phone signs in with the phone.
    musa = Person("Musa Bello", phone="+2348031234567", nin=NIN_B)
    assert place(admin, group, musa) == "registered with the phone"
    assert Member.objects.get(phone_number="+2348031234567").nin is None
    assert place(admin, group, Person("Hauwa Sani", nin=NIN_A)) == "already in the group"
    member_account("08035550009", name="Grace Obi")
    other = Person("Hauwa Sani", phone="+2348035550009", nin=NIN_A)
    assert place(admin, group, other) == "held: the NIN belongs to another member"


def test_a_corrected_list_still_finds_who_registered_with_a_phone(admin, group):
    """A correction may give someone else the phone a person registered
    with: the person then comes with their NIN only, and the phone among
    their rows' other phones."""
    assert place(admin, group, Person("Musa Bello", phone="+2348031234567", nin=NIN_A)) == (
        "registered with the phone"
    )
    again = Person("Musa Bello", nin=NIN_A, other_phones=["+2348031234567"])
    assert place(admin, group, again) == "already in the group"
    hauwa = Person("Hauwa Sani", nin=NIN_B, other_phones=["+2348031234567"])
    assert place(admin, group, hauwa) == "registered with the NIN"
    assert Member.objects.filter(full_name="Musa Bello").count() == 1
    assert Member.objects.filter(nin__isnull=False).count() == 1


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


def test_the_pin_is_hashed_before_the_membership_number_is_taken(admin, group, monkeypatch):
    """The number's row stays locked until the registration commits: hashing
    under it queued every registration behind the one hashing."""
    numbers = Sequence.objects.filter(name="member").values_list("value", flat=True)
    before = numbers.first()
    seen = []
    real = registration._hashed

    def spy(pin):
        seen.append(numbers.first())
        return real(pin)

    monkeypatch.setattr(registration, "_hashed", spy)
    assert place(admin, group, Person("Aisha Sani", vin=VIN_B)) == "registered with the VIN"
    assert seen == [before]
    assert numbers.first() == (before or 0) + 1
    assert Member.objects.get(vin=VIN_B).user.check_password(VIN_B)


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

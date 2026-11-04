"""A support group's own members' list, as a table in a PDF (4 November 2026:
the Directorate of Contact and Mobilization's): SN, NAME, STATE, LGA, WARD,
PU-No., VIN, PHONE, ACCOUNT NUMBER, BANK NAME, the columns parted by runs
of spaces in the PDF's layout text. Bank details are never read.

Only what is a person's own counts, decided with the owner: a VIN the list
gives under one name only, or a phone it gives under one name only. Many
rows share a value in the VIN or phone column; those, and codes that are not
19 characters, cannot be anyone's sign-in, so the rows are held for the
group to correct. The same name with other details is another person.

Reading and planning happen where the list is, so the PDF itself never
reaches the server: what goes is `people`, as JSON. On the server each
person is placed in the group: someone already a member (the same VIN or
phone) at once, with a notice, as the owner decided; anyone else registered
(apps.membership.registration.register_from_list).
"""

import collections
import re
import unicodedata
from dataclasses import asdict, dataclass, field

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.accounts.models import User
from apps.accounts.phone import normalize_ng_phone
from apps.core.choices import MembershipState
from apps.geography.scope import ScopeField
from apps.membership import registration
from apps.membership.management.commands.import_executives import person_name
from apps.membership.models import Member
from apps.messaging.services import notify

from .models import GroupMember
from .places import Places

ROW = re.compile(r"^\s*(\d{1,6})\s{2,}(\S.*)$")
VIN = re.compile(r"[0-9A-Z]{19}")
# Written as people write them: 08031234567, or with 234 in front. Ten bare
# digits are an account number.
PHONE = re.compile(r"(?:\+?234|0)[789][01]\d{8}")
UNIT = re.compile(r"(\d{1,2})\s*[-/]\s*(\d{1,2})\s*[-/]\s*(\d{1,2})\s*[-/]\s*(\d{1,3})")


@dataclass
class Row:
    page: int
    serial: int
    name: str
    places: list = field(default_factory=list)
    unit: str = ""  # a polling unit code, 18-23-01-001
    code: str = ""  # what the VIN column holds, without spaces
    phone: str = ""  # +234..., when the PHONE column holds a Nigerian mobile

    @property
    def vin(self) -> str:
        return self.code if VIN.fullmatch(self.code) and re.search(r"[A-Z]", self.code) else ""

    @property
    def key(self) -> str:
        """The name as compared: capitals and spaces only."""
        return " ".join(re.sub(r"[^A-Z ]", " ", self.name.upper()).split())


def read_line(page: int, line: str) -> Row | None:
    # NFKC turns the PDF's ligatures (ﬁ) back into letters.
    match = ROW.match(unicodedata.normalize("NFKC", line))
    if not match:
        return None
    columns = [col.strip() for col in re.split(r"\s{2,}", match.group(2).strip()) if col.strip()]
    if not columns or not re.search(r"[A-Za-z]", columns[0]):
        return None
    row = Row(page=page, serial=int(match.group(1)), name=person_name(columns[0]))
    for col in columns[1:]:
        compact = re.sub(r"[\s.\-]", "", col).upper()
        if unit := UNIT.fullmatch(col):
            row.unit = "-".join(
                f"{int(part):0{width}d}"
                for part, width in zip(unit.groups(), (2, 2, 2, 3), strict=True)
            )
        elif PHONE.fullmatch(compact):
            row.phone = normalize_ng_phone(compact) or ""
        elif re.fullmatch(r"\d{10}", compact):
            break  # the account number; the bank's name follows
        elif re.search(r"\d", compact) and re.search(r"[A-Z]", compact):
            row.code = compact
        elif not row.code and not row.phone and re.search(r"[A-Za-z]", col):
            row.places.append(" ".join(col.split()))
    return row


def read_pages(pages) -> list[Row]:
    """Rows from each page's layout text, pages counted from 1."""
    rows = []
    for number, text in enumerate(pages, start=1):
        for line in (text or "").splitlines():
            row = read_line(number, line)
            if row is not None:
                rows.append(row)
    return rows


@dataclass
class Person:
    name: str
    vin: str = ""
    phone: str = ""
    state: str = ""
    lga: str = ""
    ward: str = ""
    unit: str = ""
    rows: list = field(default_factory=list)  # [page, serial] of each row naming them


def plan(rows: list[Row]) -> tuple[list[Person], list[tuple[Row, str]]]:
    """The people to place, one per own VIN or own phone, and the rows held
    with the reason."""
    vin_names = collections.defaultdict(set)
    phone_names = collections.defaultdict(set)
    for row in rows:
        if row.vin:
            vin_names[row.vin].add(row.key)
        if row.phone:
            phone_names[row.phone].add(row.key)

    people, held = [], []
    by_vin, by_phone = {}, {}
    for row in rows:
        vin = row.vin if row.vin and len(vin_names[row.vin]) == 1 else ""
        phone = row.phone if row.phone and len(phone_names[row.phone]) == 1 else ""
        if not vin and not phone:
            held.append((row, _why(row, vin_names, phone_names)))
            continue
        person = by_vin.get(vin) if vin else None
        if person is None and phone in by_phone and not (vin and by_phone[phone].vin):
            person = by_phone[phone]
        if person is None:
            person = Person(row.name, "", "", *(row.places + ["", "", "", ""])[:4])
            person.unit = row.unit or person.unit
            people.append(person)
        if vin and not person.vin:
            person.vin = vin
        # The same name under two VINs with one phone: the phone stays with the first.
        if phone and not person.phone and phone not in by_phone:
            person.phone = phone
        if person.vin:
            by_vin[person.vin] = person
        if person.phone:
            by_phone[person.phone] = person
        person.rows.append([row.page, row.serial])
    return people, held


def _why(row, vin_names, phone_names) -> str:
    if row.vin:
        return f"VIN shared with {len(vin_names[row.vin]) - 1} other names"
    if row.phone:
        return f"phone shared with {len(phone_names[row.phone]) - 1} other names"
    if row.code and len(row.code) == 19:
        return "VIN column has no letters; a VIN has some"
    if row.code:
        return f"VIN column has {len(row.code)} characters; a VIN has 19"
    return "no VIN or phone"


def to_json(people: list[Person]) -> list[dict]:
    return [asdict(person) for person in people]


def from_json(items: list[dict]) -> list[Person]:
    return [Person(**item) for item in items]


# --- on the server ------------------------------------------------------------


def _scope(places: Places, person: Person):
    found, _ = places.place(state=person.state, lga=person.lga, ward=person.ward, unit=person.unit)
    return ScopeField().to_internal_value(found) if found else None


def _place_existing(*, actor, group, member, notice) -> str:
    if member.membership_state != MembershipState.ACTIVE:
        return "already a member, not active"
    place = GroupMember.objects.filter(group=group, member=member).first()
    if place is not None and place.status == GroupMember.Status.ACTIVE:
        return "already in the group"
    if place is not None and place.status != GroupMember.Status.INVITED:
        return "already a member, left or said no before"
    now = timezone.now()
    if place is None:
        try:
            with transaction.atomic():
                GroupMember.objects.create(
                    group=group,
                    member=member,
                    status=GroupMember.Status.ACTIVE,
                    via=GroupMember.Via.LIST,
                    added_by=actor,
                    answered_at=now,
                )
        except IntegrityError:
            return "already in the group"  # placed a moment ago, by another shard
    else:
        place.status = GroupMember.Status.ACTIVE
        place.via = GroupMember.Via.LIST
        place.answered_at = now
        place.save(update_fields=["status", "via", "answered_at", "updated_at"])
    if notice:
        notify(sender=None, member=member, title=notice[0], body=notice[1], sms=False)
    return "already a member, placed in the group"


def place_person(*, actor, group, person: Person, places: Places, source: str) -> str:
    """Place one person from the list in the group; returns what happened."""
    by_vin = Member.objects.filter(vin=person.vin).first() if person.vin else None
    by_phone = Member.objects.filter(phone_number=person.phone).first() if person.phone else None
    if by_vin and by_phone and by_vin.pk != by_phone.pk:
        return "held: the VIN and the phone belong to two different members"
    notice = (
        f"You are in {group.name}",
        f"TGCG listed you in the support group {group.name}, from its members' list. Open "
        "Account, then Support groups, to see it, or to leave if this is wrong.",
    )
    existing = by_vin or by_phone
    if existing is not None:
        return _place_existing(actor=actor, group=group, member=existing, notice=notice)
    if person.phone and User.objects.filter(phone_number=person.phone).exists():
        return "held: the phone belongs to a staff account"
    try:
        with transaction.atomic():
            member = registration.register_from_list(
                actor=actor,
                full_name=person.name,
                source=source,
                phone_number=person.phone or None,
                vin=person.vin or None,
                scope=_scope(places, person),
            )
            GroupMember.objects.create(
                group=group,
                member=member,
                status=GroupMember.Status.ACTIVE,
                via=GroupMember.Via.LIST,
                added_by=actor,
                answered_at=timezone.now(),
            )
    except ValidationError as error:
        return f"held: {'; '.join(error.messages)[:200]}"
    return "registered with the phone" if person.phone else "registered with the VIN"

"""A support group's own members' list, as a table in a PDF (4 November 2026:
the Directorate of Contact and Mobilization's): SN, NAME, STATE, LGA, WARD,
PU-No., VIN, PHONE, ACCOUNT NUMBER, BANK NAME, the columns parted by runs
of spaces in the PDF's layout text. Bank details are never read.

Only what is a person's own counts, decided with the owner: a VIN the list
gives under one name only, or a phone it gives under one name only. Many
rows share a value in the VIN or phone column; those, and codes that are not
19 characters, cannot be anyone's sign-in, so the rows are held for the
group to correct. The same name with other details is another person.

A spreadsheet's table printed to PDF is read by its columns' places instead
(apps.support_groups.table_list; 5 November 2026: the Kaduna State persons
with disabilities farmers). It may give a NIN, which counts the same way:
someone without a phone of their own signs in with it, as the owner decided;
with neither, the row is left out. Its gender, disability and occupation are
recorded for those newly registered.

Reading and planning happen where the list is, so the PDF itself never
reaches the server: what goes is `people`, as JSON. On the server each
person is placed in the group: someone already a member (the same VIN,
phone or NIN) at once, with a notice, as the owner decided; anyone else
registered (apps.membership.registration.register_from_list).
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
NIN = re.compile(r"\d{11}")
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
    phone_column: str = ""  # a table list's PHONE column, without spaces
    nin_column: str = ""  # what the NIN column holds, without spaces
    gender: str = ""  # a Gender value
    disabilities: list = field(default_factory=list)  # Disability values
    disability_other: str = ""
    occupation: str = ""

    @property
    def vin(self) -> str:
        return self.code if VIN.fullmatch(self.code) and re.search(r"[A-Z]", self.code) else ""

    @property
    def nin(self) -> str:
        """11 digits, unless they are a phone number written in the column."""
        column = self.nin_column
        return column if NIN.fullmatch(column) and not PHONE.fullmatch(column) else ""

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
    nin: str = ""
    gender: str = ""
    disabilities: list = field(default_factory=list)
    disability_other: str = ""
    occupation: str = ""


def plan(rows: list[Row]) -> tuple[list[Person], list[tuple[Row, str]]]:
    """The people to place, one per own VIN, own phone or own NIN, and the
    rows held with the reason."""
    names = {"vin": collections.defaultdict(set), "phone": collections.defaultdict(set)}
    names["nin"] = collections.defaultdict(set)
    for row in rows:
        for kind, found in names.items():
            if getattr(row, kind):
                found[getattr(row, kind)].add(row.key)

    def own(row, kind):
        value = getattr(row, kind)
        return value if value and len(names[kind][value]) == 1 else ""

    people, held = [], []
    by_vin, by_phone, by_nin = {}, {}, {}
    for row in rows:
        if not row.key:
            held.append((row, "no name"))
            continue
        vin, phone, nin = own(row, "vin"), own(row, "phone"), own(row, "nin")
        if not vin and not phone and not nin:
            held.append((row, _why(row, names)))
            continue
        person = by_vin.get(vin) if vin else None
        # The same name under two phones with one NIN: the NIN stays with the first.
        if (
            person is None
            and nin in by_nin
            and not (vin and by_nin[nin].vin)
            and not (phone and by_nin[nin].phone and by_nin[nin].phone != phone)
        ):
            person = by_nin[nin]
        if (
            person is None
            and phone in by_phone
            and not (vin and by_phone[phone].vin)
            and not (nin and by_phone[phone].nin)
        ):
            person = by_phone[phone]
        if person is None:
            person = Person(row.name, "", "", *(row.places + ["", "", "", ""])[:4])
            person.unit = row.unit or person.unit
            people.append(person)
        if vin and not person.vin:
            person.vin = vin
        if nin and not person.nin and nin not in by_nin:
            person.nin = nin
        # The same name under two VINs with one phone: the phone stays with the first.
        if phone and not person.phone and phone not in by_phone:
            person.phone = phone
        if not person.gender:
            person.gender = row.gender
        if not person.disabilities:
            person.disabilities, person.disability_other = row.disabilities, row.disability_other
        if not person.occupation:
            person.occupation = row.occupation
        for value, found in ((person.vin, by_vin), (person.nin, by_nin), (person.phone, by_phone)):
            if value:
                found[value] = person
        person.rows.append([row.page, row.serial])
    return people, held


def _why(row, names) -> str:
    """Why a row is held; each problem, the first deciding how it is counted."""
    reasons = []
    for kind, label in (("vin", "VIN"), ("phone", "phone"), ("nin", "NIN")):
        if value := getattr(row, kind):
            reasons.append(f"{label} shared with {len(names[kind][value]) - 1} other names")
    if row.code and not row.vin and len(row.code) == 19:
        reasons.append("VIN column has no letters; a VIN has some")
    elif row.code and not row.vin:
        reasons.append(f"VIN column has {len(row.code)} characters; a VIN has 19")
    if row.phone_column and not row.phone:
        reasons.append("phone column is not a Nigerian mobile number")
    column = row.nin_column
    if column and PHONE.fullmatch(column):
        reasons.append("NIN column holds a phone number")
    elif column and column.isdigit() and not row.nin:
        reasons.append(f"NIN column has {len(column)} digits; a NIN has 11")
    elif column and not row.nin:
        reasons.append("NIN column is not a number")
    return " + ".join(reasons) or "no VIN, phone or NIN"


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
    by_nin = Member.objects.filter(nin=person.nin).first() if person.nin else None
    if by_vin and by_phone and by_vin.pk != by_phone.pk:
        return "held: the VIN and the phone belong to two different members"
    existing = by_vin or by_phone
    if by_nin and existing and by_nin.pk != existing.pk:
        return "held: the NIN belongs to another member"
    notice = (
        f"You are in {group.name}",
        f"TGCG listed you in the support group {group.name}, from its members' list. Open "
        "Account, then Support groups, to see it, or to leave if this is wrong.",
    )
    existing = existing or by_nin
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
                nin=person.nin or None,
                scope=_scope(places, person),
                gender=person.gender,
                occupation=person.occupation,
                disabilities=person.disabilities,
                disability_other=person.disability_other,
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
    if person.phone:
        return "registered with the phone"
    return "registered with the VIN" if person.vin else "registered with the NIN"

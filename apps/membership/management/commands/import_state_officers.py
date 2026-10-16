"""Import a state chapter's officers from a CSV file prepared from its list.

Columns:
- sn: the row's label on the list, e.g. "12" or "Soba 17";
- name, phone: as on the list (the phone, as written, is the first password);
- role: the position's role name, e.g. "State Treasurer". "State
  Coordinator", "LGA Coordinator" and "Ward Coordinator" are the system
  roles. "Deputy State Coordinator" is created, when missing, with the State
  Coordinator's permissions, for a senatorial district. Any other missing role
  is created for the level of the areas it is given over here: an officer role
  for a state, district, LGA or ward, whose holders see their area's incident
  counts, results and collation, meet and use the forum, and change nothing
  (agreed with the user on 16 October 2026); or, at a polling unit, an
  executive role like those of import_executives. Administrators change roles
  later in Role Management;
- place: where the officer votes, as precisely as the list allows: a polling
  unit code (21-03-03-018), a ward code (21-20-05), an LGA code (KE-ARGUNGU),
  or the state's own code (KD) when the list gives no place;
- area (optional): what the role covers: a senatorial district (SD/049/JG),
  LGA, ward or polling unit code; empty for the whole state (--state);
- vin (optional): the VIN of their voter's card, stored on the membership;
- hold: a reason to hold the row back, e.g. while the chapter confirms it.

Rows with the same phone number and the same name are one person holding
several roles (agreed with the user on 16 October 2026): they are registered
once, at the place their rows share, and appointed to each role. The same
phone number under different names holds all those rows back.

Each person becomes a member at their place, with an app account, and an
appointment to each role over its area. State, Deputy State, LGA and Ward
Coordinators also receive, from --by, the power to appoint and manage the
roles below them in their area, as no system role carries it
(docs/access-control.md, section 5). As with import_executives, the chapter
checked these people and holds their agreement to TGCG keeping their details,
so --by, a National Administrator, records both on its word.

A dry run unless --commit. Rows are held back when the list holds them, or
their phone number or VIN is invalid, shared with someone else or registered
already, and the report names them by line and S/N only, never by name, phone
number or VIN. Run it again once rows are corrected: those already imported
are then held as registered, and only the corrected ones are added.
"""

import csv
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.access import engine
from apps.access.models import RoleCategory, RoleDefinition, name_key
from apps.access.services import appoint, create_role, delegate
from apps.accounts.models import User
from apps.core.choices import GeographyLevel, Role
from apps.core.permissions import Capability as C
from apps.geography.models import Lga, PollingUnit, SenatorialDistrict, State, Ward
from apps.membership import registration
from apps.membership.models import Member

from .import_executives import MOBILE, PU_PERMISSIONS, person_name, registered

L = GeographyLevel
# What an officer may do across their area.
STATE_OFFICER_PERMISSIONS = sorted(
    {
        C.VIEW_INCIDENTS,
        C.VIEW_COLLATION,
        C.VIEW_MEETING_ROOM,
        C.START_MEETING,
        C.JOIN_MEETING,
        C.VIEW_DISCUSSION_ROOM,
        C.CREATE_DISCUSSION_THREAD,
        C.POST_DISCUSSION_REPLY,
    }
)
DEPUTY = "Deputy State Coordinator"
# The roles whose holders also appoint the roles below them in their area.
COORDINATORS = {
    name_key(name) for name in ("State Coordinator", DEPUTY, "LGA Coordinator", "Ward Coordinator")
}
# The levels a coordinator over an area of each level appoints at.
BELOW = {
    L.STATE: [L.LGA, L.WARD, L.POLLING_UNIT],
    L.SENATORIAL_DISTRICT: [L.LGA, L.WARD, L.POLLING_UNIT],
    L.LGA: [L.WARD, L.POLLING_UNIT],
    L.WARD: [L.POLLING_UNIT],
}
# The field roles a coordinator appoints in their area.
APPOINTED = (Role.LGA_COORDINATOR, Role.WARD_COORDINATOR, Role.OBSERVER)
# Who a new role reports to, by the highest level it serves.
SUPERVISORS = {
    L.STATE: Role.STATE_COORDINATOR,
    L.SENATORIAL_DISTRICT: Role.STATE_COORDINATOR,
    L.LGA: Role.LGA_COORDINATOR,
    L.WARD: Role.WARD_COORDINATOR,
    L.POLLING_UNIT: Role.WARD_COORDINATOR,
}
ORDER = [L.STATE, L.SENATORIAL_DISTRICT, L.LGA, L.WARD, L.POLLING_UNIT]
UNIT_LEVELS = {"polling_unit": L.POLLING_UNIT, "ward": L.WARD, "lga": L.LGA}
VIN = re.compile(r"^[A-Z0-9]{19}$")


@dataclass
class Row:
    line: int
    sn: str
    name: str
    role: str
    phone: str  # as written: 11 digits, also the first password
    place: dict  # polling_unit, ward, lga or state for registration.register_imported
    area: dict  # the appointment's area
    vin: str | None

    @property
    def level(self) -> str:
        return self.area["scope_level"]


def new_role_levels(name: str, levels: set) -> list[str]:
    """The levels a missing role is created for, given the levels of the
    areas it is given over in the file."""
    if name_key(name) == name_key(DEPUTY):
        return [L.SENATORIAL_DISTRICT, L.STATE]
    if levels <= {L.POLLING_UNIT}:
        return [L.POLLING_UNIT]
    top = min(levels, key=ORDER.index)
    if top in (L.STATE, L.SENATORIAL_DISTRICT):
        return [L.STATE, L.SENATORIAL_DISTRICT]
    return sorted(levels - {L.POLLING_UNIT}, key=ORDER.index)


def ensure_role(actor, name: str, levels: list[str]) -> RoleDefinition:
    """The role named [name]; a missing one is created for [levels]."""
    role = RoleDefinition.objects.filter(name_key=name_key(name)).first()
    if role is not None:
        return role
    coordinator = RoleDefinition.objects.get(code=Role.STATE_COORDINATOR.value)
    if name_key(name) == name_key(DEPUTY):
        return create_role(
            actor=actor,
            name=name,
            description="Coordinates a senatorial district for the State Coordinator.",
            category=coordinator.category,
            permitted_levels=levels,
            permissions=sorted(coordinator.permissions),
            supervising_role=coordinator,
        )
    supervisor = RoleDefinition.objects.get(code=SUPERVISORS[levels[0]].value)
    if levels == [L.POLLING_UNIT]:
        description, category, permissions = (
            "Polling unit executive of a TGCG chapter.",
            RoleCategory.FIELD,
            PU_PERMISSIONS,
        )
    elif levels[0] == L.STATE:
        description, category, permissions = (
            "State officer of a TGCG chapter.",
            RoleCategory.COMMAND,
            STATE_OFFICER_PERMISSIONS,
        )
    else:
        # Field, so the coordinators above them may appoint them in their area.
        description, category, permissions = (
            f"{'LGA' if levels[0] == L.LGA else 'Ward'} officer of a TGCG chapter.",
            RoleCategory.FIELD,
            STATE_OFFICER_PERMISSIONS,
        )
    return create_role(
        actor=actor,
        name=name,
        description=description,
        category=category,
        permitted_levels=levels,
        permissions=permissions,
        supervising_role=supervisor,
    )


def appointing_power() -> list[str]:
    """Appoint and manage appointments, up to everything the field roles a
    coordinator appoints carry."""
    ceiling = set(PU_PERMISSIONS) | set(STATE_OFFICER_PERMISSIONS)
    for role in RoleDefinition.objects.filter(code__in=[r.value for r in APPOINTED]):
        ceiling |= set(role.permissions)
    return sorted(ceiling | {C.ASSIGN_ROLES, C.MANAGE_ASSIGNMENTS})


class Command(BaseCommand):
    help = "Import a state chapter's officers from a CSV file (dry run unless --commit)."

    def add_arguments(self, parser):
        parser.add_argument("csv_path")
        parser.add_argument("--state", required=True, help="State code, e.g. KE for Kebbi.")
        parser.add_argument(
            "--by", required=True, help="Access ID of the National Administrator importing it."
        )
        parser.add_argument(
            "--source",
            required=True,
            help='The list, for the record, e.g. "TGCG Kebbi State chapter: officers list".',
        )
        parser.add_argument("--commit", action="store_true", help="Save; without it, a dry run.")

    def handle(self, *args, csv_path, state, by, source, commit, **options):
        actor = User.objects.filter(access_id=by).first()
        if actor is None or not engine.is_national_administrator(actor):
            raise CommandError("--by must be the access ID of a National Administrator.")
        state = State.objects.filter(code=state).first()
        if state is None:
            raise CommandError("No such state.")
        path = Path(csv_path)
        if not path.is_file():
            raise CommandError(f"No such file: {path}")

        accepted, held, levels = self._check(path, state)
        for line, sn, reason in held:
            self.stdout.write(f"held: line {line} (S/N {sn or '-'}): {reason}")
        people = defaultdict(list)
        for row in accepted:
            people[row.phone].append(row)
        places = Counter(next(iter(rows[0].place)) for rows in people.values())
        with_vin = sum(1 for rows in people.values() if any(row.vin for row in rows))
        self.stdout.write(
            f"{len(accepted)} to import, {len(held)} held back. Placed at "
            f"{places['polling_unit']} polling units, {places['ward']} wards and "
            f"{places['lga']} LGAs; {with_vin} with a VIN."
        )
        if places["state"]:
            self.stdout.write(f"{places['state']} placed at state level.")
        several = sum(1 for rows in people.values() if len(rows) > 1)
        if several:
            self.stdout.write(f"{len(people)} people; {several} of them hold more than one role.")
        names = {name_key(row.role) for row in accepted}
        known = RoleDefinition.objects.filter(name_key__in=names).count()
        self.stdout.write(f"{len(names)} roles, {len(names) - known} of them new.")
        if not commit:
            self.stdout.write("Dry run: nothing was saved. Add --commit to import.")
            return

        with transaction.atomic():
            roles, powers = {}, 0
            for rows in people.values():
                first = rows[0]
                member = registration.register_imported(
                    actor=actor,
                    full_name=first.name,
                    phone_number=first.phone,
                    password=first.phone,
                    source=source,
                    vin=next((row.vin for row in rows if row.vin), None),
                    **first.place,
                )
                for row in rows:
                    key = name_key(row.role)
                    if key not in roles:
                        roles[key] = ensure_role(actor, row.role, levels[key])
                    appoint(
                        actor=actor,
                        member=member,
                        role=roles[key],
                        areas=[row.area],
                        reason=source,
                    )
                    if key in COORDINATORS:
                        delegate(
                            actor=actor,
                            recipient=member,
                            areas=[row.area],
                            permissions=appointing_power(),
                            categories=[RoleCategory.FIELD],
                            levels=BELOW[row.level],
                            reason=source,
                        )
                        powers += 1
        appointments = f", {len(accepted)} appointments" if len(accepted) != len(people) else ""
        self.stdout.write(
            self.style.SUCCESS(
                f"Imported {len(people)} members with their roles ({len(roles)} roles)"
                f"{appointments}; {powers} coordinators may appoint in their areas."
            )
        )

    def _place(self, code: str, state) -> dict | None:
        """The polling unit, ward or LGA a place code names, or the state
        itself, if it is in [state]."""
        if code == state.code:
            return {"state": state}
        if unit := PollingUnit.objects.filter(code=code).select_related("ward__lga").first():
            return {"polling_unit": unit} if unit.ward.lga.state_id == state.pk else None
        if ward := Ward.objects.filter(code=code).select_related("lga").first():
            return {"ward": ward} if ward.lga.state_id == state.pk else None
        if lga := Lga.objects.filter(code=code).first():
            return {"lga": lga} if lga.state_id == state.pk else None
        return None

    def _area(self, code: str, state) -> dict | None:
        """The whole state, or a senatorial district, LGA, ward or polling unit of it."""
        if not code:
            return {"scope_level": L.STATE, "state": state}
        if district := SenatorialDistrict.objects.filter(code=code, state=state).first():
            return {"scope_level": L.SENATORIAL_DISTRICT, "senatorial_district": district}
        place = self._place(code, state)
        if place is None or "state" in place:
            return None
        ((field, unit),) = place.items()
        return {"scope_level": UNIT_LEVELS[field], field: unit}

    def _check(self, path: Path, state):
        """The rows to import, the rows held back with their reasons, and the
        levels each missing role will serve."""
        with path.open(encoding="utf-8-sig", newline="") as handle:
            records = [
                (line, record) for line, record in enumerate(csv.DictReader(handle), start=2)
            ]
        rows = []
        for line, record in records:
            rows.append(
                (
                    (record.get("hold") or "").strip(),
                    Row(
                        line=line,
                        sn=(record.get("sn") or "").strip(),
                        name=person_name(record.get("name") or ""),
                        role=" ".join((record.get("role") or "").split()),
                        phone=re.sub(r"\D", "", record.get("phone") or ""),
                        place=self._place((record.get("place") or "").strip(), state),
                        area=self._area((record.get("area") or "").strip(), state),
                        vin="".join((record.get("vin") or "").split()).upper() or None,
                    ),
                )
            )

        # A phone number is one person: their rows must agree on name and place.
        by_phone = defaultdict(list)
        for _, row in rows:
            if MOBILE.match(row.phone):
                by_phone[row.phone].append(row)
        shared = {
            phone
            for phone, group in by_phone.items()
            if len({name_key(row.name) for row in group}) > 1
        }
        moved = {
            phone
            for phone, group in by_phone.items()
            if len({_place_key(row.place) for row in group if row.place}) > 1
        }
        owners = defaultdict(set)
        for _, row in rows:
            if row.vin:
                owners[row.vin].add(row.phone)

        # The levels each role serves here, and those an existing role allows.
        # Rows the list holds back do not shape a new role.
        levels = defaultdict(set)
        for hold, row in rows:
            if row.role and row.area and not hold:
                levels[name_key(row.role)].add(row.level)
        allowed = {}
        for key, used in levels.items():
            role = RoleDefinition.objects.filter(name_key=key).first()
            allowed[key] = role.permitted_levels if role else new_role_levels(key, used)

        accepted, held, given = [], [], set()
        for hold, row in rows:
            appointment = row.area and (row.phone, name_key(row.role), _area_key(row.area))
            if hold:
                reason = f"held by the list: {hold}"
            elif not row.name:
                reason = "no name"
            elif not row.role:
                reason = "no role"
            elif not MOBILE.match(row.phone):
                reason = "not a valid Nigerian mobile number"
            elif row.phone in shared:
                reason = "phone number shared with another row"
            elif row.place is None:
                reason = "place not in this state's INEC register"
            elif row.area is None:
                reason = "area not in this state"
            elif row.phone in moved:
                reason = "the same person has another place on another row"
            elif row.level not in allowed[name_key(row.role)]:
                reason = "role not allowed for this area"
            elif appointment in given:
                reason = "the same role and area as another row for this person"
            elif row.vin and not VIN.match(row.vin):
                reason = "VIN not valid"
            elif row.vin and len(owners[row.vin]) > 1:
                reason = "VIN shared with another row"
            elif registered(row.phone):
                reason = "phone number registered already"
            elif row.vin and Member.objects.filter(vin=row.vin).exists():
                reason = "VIN registered already"
            else:
                accepted.append(row)
                given.add(appointment)
                continue
            held.append((row.line, row.sn, reason))
        return accepted, held, {key: allowed[key] for key in levels}


def _place_key(place: dict) -> tuple:
    ((field, unit),) = place.items()
    return field, unit.pk


def _area_key(area: dict) -> tuple:
    ((field, unit),) = ((k, v) for k, v in area.items() if k != "scope_level")
    return area["scope_level"], unit.pk

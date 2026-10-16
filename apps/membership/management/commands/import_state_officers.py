"""Import a state chapter's officers from a CSV file prepared from its list.

Columns:
- sn: the list's serial number;
- name, phone: as on the list (the phone, as written, is the first password);
- role: the position's role name, e.g. "State Treasurer". "State Coordinator"
  is the system role. "Deputy State Coordinator" is created, when missing,
  with the State Coordinator's permissions, for a senatorial district. Any
  other missing role is created as a state officer role: its holders see the
  incident counts, results and collation of their area, meet and use the
  forum, and change nothing (agreed with the user on 16 October 2026). Both
  report to the State Coordinator. Administrators change roles later in Role
  Management;
- place: where the officer votes, as precisely as the list allows: a polling
  unit code (21-03-03-018), a ward code (21-20-05) or an LGA code (KE-ARGUNGU);
- area (optional): the senatorial district the role covers (SD/049/JG); empty
  for the whole state (--state);
- vin (optional): the VIN of their voter's card, stored on the membership;
- hold: a reason to hold the row back, e.g. while the chapter confirms it.

Each other valid row becomes a member at that place, with an app account, and
an appointment to the role over its area. State and Deputy State Coordinators
also receive, from --by, the power to appoint and manage the field roles below
them in their area (LGA and ward coordinators, observers, polling unit
executives), as no system role carries it (docs/access-control.md, section 5).
As with import_executives, the chapter checked these people and holds their
agreement to TGCG keeping their details, so --by, a National Administrator,
records both on its word.

A dry run unless --commit. Rows are held back when the list holds them, or
their phone number or VIN is invalid, shared with another row or registered
already, and the report names them by line and S/N only, never by name, phone
number or VIN. Run it again once rows are corrected: those already imported
are then held as registered, and only the corrected ones are added.
"""

import csv
import re
from collections import Counter
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

# What a state officer may do across their area.
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
# The roles whose holders also appoint the field roles below them.
COORDINATORS = {name_key("State Coordinator"), name_key(DEPUTY)}
# The field roles a coordinator appoints in their area.
APPOINTED = (Role.LGA_COORDINATOR, Role.WARD_COORDINATOR, Role.OBSERVER)
VIN = re.compile(r"^[A-Z0-9]{19}$")


@dataclass
class Row:
    line: int
    sn: str
    name: str
    role: str
    phone: str  # as written: 11 digits, also the first password
    place: dict  # polling_unit, ward or lga for registration.register_imported
    area: dict  # the appointment's area
    vin: str | None


def ensure_role(actor, name: str) -> RoleDefinition:
    """The role named [name]; a missing one is created as a Deputy State
    Coordinator or a state officer role."""
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
            permitted_levels=[GeographyLevel.SENATORIAL_DISTRICT, GeographyLevel.STATE],
            permissions=sorted(coordinator.permissions),
            supervising_role=coordinator,
        )
    return create_role(
        actor=actor,
        name=name,
        description="State officer of a TGCG chapter.",
        category=RoleCategory.COMMAND,
        permitted_levels=[GeographyLevel.STATE, GeographyLevel.SENATORIAL_DISTRICT],
        permissions=STATE_OFFICER_PERMISSIONS,
        supervising_role=coordinator,
    )


def appointing_power() -> list[str]:
    """Appoint and manage appointments, up to everything the field roles a
    coordinator appoints carry."""
    ceiling = set(PU_PERMISSIONS)
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

        accepted, held = self._check(path, state)
        for line, sn, reason in held:
            self.stdout.write(f"held: line {line} (S/N {sn or '-'}): {reason}")
        levels = Counter(next(iter(row.place)) for row in accepted)
        self.stdout.write(
            f"{len(accepted)} to import, {len(held)} held back. Placed at "
            f"{levels['polling_unit']} polling units, {levels['ward']} wards and "
            f"{levels['lga']} LGAs; {sum(1 for row in accepted if row.vin)} with a VIN."
        )
        names = {name_key(row.role) for row in accepted}
        known = RoleDefinition.objects.filter(name_key__in=names).count()
        self.stdout.write(f"{len(names)} roles, {len(names) - known} of them new.")
        if not commit:
            self.stdout.write("Dry run: nothing was saved. Add --commit to import.")
            return

        with transaction.atomic():
            roles, powers = {}, 0
            for row in accepted:
                if row.role not in roles:
                    roles[row.role] = ensure_role(actor, row.role)
                member = registration.register_imported(
                    actor=actor,
                    full_name=row.name,
                    phone_number=row.phone,
                    password=row.phone,
                    source=source,
                    vin=row.vin,
                    **row.place,
                )
                appoint(
                    actor=actor,
                    member=member,
                    role=roles[row.role],
                    areas=[row.area],
                    reason=source,
                )
                if name_key(row.role) in COORDINATORS:
                    delegate(
                        actor=actor,
                        recipient=member,
                        areas=[row.area],
                        permissions=appointing_power(),
                        categories=[RoleCategory.FIELD],
                        levels=[
                            GeographyLevel.LGA,
                            GeographyLevel.WARD,
                            GeographyLevel.POLLING_UNIT,
                        ],
                        reason=source,
                    )
                    powers += 1
        self.stdout.write(
            self.style.SUCCESS(
                f"Imported {len(accepted)} members with their roles ({len(roles)} roles); "
                f"{powers} coordinators may appoint in their areas."
            )
        )

    def _place(self, code: str, state) -> dict | None:
        """The polling unit, ward or LGA a place code names, if it is in [state]."""
        if unit := PollingUnit.objects.filter(code=code).select_related("ward__lga").first():
            return {"polling_unit": unit} if unit.ward.lga.state_id == state.pk else None
        if ward := Ward.objects.filter(code=code).select_related("lga").first():
            return {"ward": ward} if ward.lga.state_id == state.pk else None
        if lga := Lga.objects.filter(code=code).first():
            return {"lga": lga} if lga.state_id == state.pk else None
        return None

    def _area(self, code: str, state) -> dict | None:
        """The whole state, or a senatorial district of it."""
        if not code:
            return {"scope_level": GeographyLevel.STATE, "state": state}
        district = SenatorialDistrict.objects.filter(code=code, state=state).first()
        if district is None:
            return None
        return {"scope_level": GeographyLevel.SENATORIAL_DISTRICT, "senatorial_district": district}

    def _check(self, path: Path, state):
        with path.open(encoding="utf-8-sig", newline="") as handle:
            rows = [(line, record) for line, record in enumerate(csv.DictReader(handle), start=2)]
        phones = Counter(re.sub(r"\D", "", record.get("phone") or "") for _, record in rows)
        vins = Counter("".join((record.get("vin") or "").split()).upper() for _, record in rows)
        accepted, held = [], []
        for line, record in rows:
            sn = (record.get("sn") or "").strip()
            name = person_name(record.get("name") or "")
            role = " ".join((record.get("role") or "").split())
            phone = re.sub(r"\D", "", record.get("phone") or "")
            place = self._place((record.get("place") or "").strip(), state)
            area = self._area((record.get("area") or "").strip(), state)
            vin = "".join((record.get("vin") or "").split()).upper() or None
            if hold := (record.get("hold") or "").strip():
                reason = f"held by the list: {hold}"
            elif not name:
                reason = "no name"
            elif not role:
                reason = "no role"
            elif not MOBILE.match(phone):
                reason = "not a valid Nigerian mobile number"
            elif phones[phone] > 1:
                reason = "phone number shared with another row"
            elif place is None:
                reason = "place not in this state's INEC register"
            elif area is None:
                reason = "area not a senatorial district of this state"
            elif vin and not VIN.match(vin):
                reason = "VIN not valid"
            elif vin and vins[vin] > 1:
                reason = "VIN shared with another row"
            elif registered(phone):
                reason = "phone number registered already"
            elif vin and Member.objects.filter(vin=vin).exists():
                reason = "VIN registered already"
            else:
                accepted.append(Row(line, sn, name, role, phone, place, area, vin))
                continue
            held.append((line, sn, reason))
        return accepted, held

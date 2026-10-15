"""Import a chapter's polling unit executives from a CSV file.

Columns: sn, name, rank, phone, polling_unit (the unit's number within
--ward). Each valid row becomes a member at that polling unit, with an app
account whose first password is their phone number as written, and an
appointment to the role for their rank. The polling unit executive roles are
created when missing. The chapter checked these people and holds their
agreement to TGCG keeping their details, so --by, a National Administrator,
records both on its word.

A dry run unless --commit. Rows are held back when their phone number is
shared with another row, is not a valid Nigerian mobile number, or is
registered already, and the report names them by line and S/N only, never by
name or phone number. Run it again once numbers are corrected: rows already
imported are then held as registered, and only the corrected ones are added.
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
from apps.access.services import appoint, create_role
from apps.accounts.models import User
from apps.accounts.phone import normalize_ng_phone
from apps.core.choices import GeographyLevel, Role
from apps.core.permissions import Capability as C
from apps.geography.models import PollingUnit, Ward
from apps.membership import registration
from apps.membership.models import Member

# Every polling unit executive role, including those a list may not fill yet.
PU_ROLES = (
    "PU Coordinator",
    "Assistant PU Coordinator",
    "PU Youth Leader",
    "Assistant PU Youth Leader",
    "PU Women Coordinator",
    "PU Organising Officer",
)
# What they may do at their polling unit, as agreed with the user on
# 15 October 2026: their unit's incidents, but no Situation Room, which is
# national. Administrators change a role later in Role Management.
PU_PERMISSIONS = sorted(
    {
        C.VIEW_MEETING_ROOM,
        C.JOIN_MEETING,
        C.CREATE_INCIDENT,
        C.SUBMIT_FIELD_REPORT,
        C.SUBMIT_ELECTION_RESULT,
        C.VIEW_INCIDENTS,
        C.VERIFY_MEMBER_IDENTITY,
        C.VIEW_DISCUSSION_ROOM,
        C.CREATE_DISCUSSION_THREAD,
        C.POST_DISCUSSION_REPLY,
    }
)
# The ranks on a chapter's list, compared without dots or spaces.
RANKS = {
    "PUCOORDINATOR": "PU Coordinator",
    "ASSTPUCOORDINATOR": "Assistant PU Coordinator",
    "PUYOUTH": "PU Youth Leader",
    "PUYOUTHLEADER": "PU Youth Leader",
    "ASSTPUYOUTH": "Assistant PU Youth Leader",
    "ASSTPUYOUTHLEADER": "Assistant PU Youth Leader",
    "PUWOMENCOORDINATOR": "PU Women Coordinator",
    "PUORGOFFICER": "PU Organising Officer",
    "PUORGANISINGOFFICER": "PU Organising Officer",
}
# A Nigerian mobile number as written locally: 070x, 080x, 081x, 090x, 091x.
MOBILE = re.compile(r"^0(70[1-9]|80[2-9]|81\d|90[1-9]|91[1-6])\d{7}$")


@dataclass
class Row:
    line: int
    sn: str
    name: str
    role: str
    phone: str  # as written: 11 digits, also the first password
    unit: PollingUnit


def person_name(value: str) -> str:
    """NAFI'U ALIYU -> Nafi'u Aliyu: capitals only after spaces and hyphens."""
    name = " ".join(value.replace("`", "'").split()).lower()
    return re.sub(r"(^|[\s\-])([a-z])", lambda m: m[1] + m[2].upper(), name)


def ensure_pu_roles(actor) -> dict[str, RoleDefinition]:
    """The polling unit executive roles, reporting to the Ward Coordinator.
    Missing ones are created; existing ones stay as administrators left them."""
    supervisor = RoleDefinition.objects.get(code=Role.WARD_COORDINATOR.value)
    roles = {}
    for name in PU_ROLES:
        role = RoleDefinition.objects.filter(name_key=name_key(name)).first()
        if role is None:
            role = create_role(
                actor=actor,
                name=name,
                description="Polling unit executive of a TGCG chapter.",
                category=RoleCategory.FIELD,
                permitted_levels=[GeographyLevel.POLLING_UNIT],
                permissions=PU_PERMISSIONS,
                supervising_role=supervisor,
            )
        roles[name] = role
    return roles


class Command(BaseCommand):
    help = "Import a chapter's polling unit executives from a CSV file (dry run unless --commit)."

    def add_arguments(self, parser):
        parser.add_argument("csv_path")
        parser.add_argument("--ward", required=True, help="INEC ward code, e.g. 18-23-05.")
        parser.add_argument(
            "--by", required=True, help="Access ID of the National Administrator importing it."
        )
        parser.add_argument(
            "--source",
            required=True,
            help='The list, for the record, e.g. "TGCG Zaria LGA chapter: PU executives list".',
        )
        parser.add_argument("--commit", action="store_true", help="Save; without it, a dry run.")

    def handle(self, *args, csv_path, ward, by, source, commit, **options):
        actor = User.objects.filter(access_id=by).first()
        if actor is None or not engine.is_national_administrator(actor):
            raise CommandError("--by must be the access ID of a National Administrator.")
        if not Ward.objects.filter(code=ward).exists():
            raise CommandError(f"Ward {ward} is not in the INEC register.")
        path = Path(csv_path)
        if not path.is_file():
            raise CommandError(f"No such file: {path}")

        accepted, held = self._check(path, ward)
        for line, sn, reason in held:
            self.stdout.write(f"held: line {line} (S/N {sn or '-'}): {reason}")
        self.stdout.write(f"{len(accepted)} to import, {len(held)} held back.")
        if not commit:
            self.stdout.write("Dry run: nothing was saved. Add --commit to import.")
            return

        with transaction.atomic():
            roles = ensure_pu_roles(actor)
            for row in accepted:
                member = registration.register_imported(
                    actor=actor,
                    full_name=row.name,
                    phone_number=row.phone,
                    polling_unit=row.unit,
                    password=row.phone,
                    source=source,
                )
                appoint(
                    actor=actor,
                    member=member,
                    role=roles[row.role],
                    areas=[{"scope_level": GeographyLevel.POLLING_UNIT, "polling_unit": row.unit}],
                    reason=source,
                )
        self.stdout.write(self.style.SUCCESS(f"Imported {len(accepted)} members with their roles."))

    def _check(self, path: Path, ward: str):
        with path.open(encoding="utf-8-sig", newline="") as handle:
            rows = [(line, record) for line, record in enumerate(csv.DictReader(handle), start=2)]
        phones = Counter(re.sub(r"\D", "", record.get("phone") or "") for _, record in rows)
        units = {unit.code: unit for unit in PollingUnit.objects.filter(ward__code=ward)}
        accepted, held = [], []
        for line, record in rows:
            sn = (record.get("sn") or "").strip()
            name = person_name(record.get("name") or "")
            role = RANKS.get(re.sub(r"[^A-Z]", "", (record.get("rank") or "").upper()))
            phone = re.sub(r"\D", "", record.get("phone") or "")
            number = (record.get("polling_unit") or "").strip()
            unit = units.get(f"{ward}-{int(number):03d}") if number.isdigit() else None
            if not name:
                reason = "no name"
            elif role is None:
                reason = "rank not recognised"
            elif not MOBILE.match(phone):
                reason = "not a valid Nigerian mobile number"
            elif phones[phone] > 1:
                reason = "phone number shared with another row"
            elif unit is None:
                reason = "polling unit not in this ward"
            elif registered(phone):
                reason = "phone number registered already"
            else:
                accepted.append(Row(line, sn, name, role, phone, unit))
                continue
            held.append((line, sn, reason))
        return accepted, held


def registered(phone: str) -> bool:
    number = normalize_ng_phone(phone)
    return (
        Member.objects.filter(phone_number=number).exists()
        or User.objects.filter(phone_number=number).exists()
    )

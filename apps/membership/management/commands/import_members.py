"""Import people from a list as members only, with no post (CSV).

Columns, as for import_state_officers without role, area or vin:
- sn: the row's label on the list, e.g. "Babura 12";
- name, phone: as on the list (the phone, as written, is the first password);
- place: where the person votes, as precisely as the list allows: a polling
  unit code (17-05-02-001), a ward code (17-05-02), an LGA code (JI-BUJI), or
  the state's own code (JI) when the list gives no place;
- hold: a reason to hold the row back, e.g. while the chapter confirms it.

Agreed with the user on 18 October 2026 for the Jigawa polling unit agent
lists: the people listed become members at their polling unit, with no post.
The chapter holds their agreement to TGCG keeping their details, so --by, a
National Administrator, records their consent on its word. Their identity
is not recorded as checked: a post, an agent's accreditation included, waits
for the usual check, as for anyone who joins.

A dry run unless --commit. Rows are held back when the list holds them, when
the name is missing, or when the phone number is invalid, registered already
or shared with someone else under another name, or when the same person is
listed at two places. The same person listed again at the same place is
imported once. The report names rows by line and S/N only, never by name or
phone number. Run it again once rows are corrected: those already imported
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
from apps.access.models import name_key
from apps.accounts.models import User
from apps.geography.models import State
from apps.membership import registration

from .import_executives import MOBILE, person_name, registered
from .import_state_officers import _place_key, place_in


@dataclass
class Person:
    line: int
    sn: str
    name: str
    phone: str  # as written: 11 digits, also the first password
    place: dict | None  # polling_unit, ward, lga or state for registration.register_imported


class Command(BaseCommand):
    help = "Import people from a list as members, with no post (dry run unless --commit)."

    def add_arguments(self, parser):
        parser.add_argument("csv_path")
        parser.add_argument("--state", required=True, help="State code, e.g. JI for Jigawa.")
        parser.add_argument(
            "--by", required=True, help="Access ID of the National Administrator importing it."
        )
        parser.add_argument(
            "--source", required=True, help="The list, as recorded with each member."
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
        places = Counter(next(iter(person.place)) for person in accepted)
        self.stdout.write(
            f"{len(accepted)} to import, {len(held)} held back. Placed at "
            f"{places['polling_unit']} polling units, {places['ward']} wards and "
            f"{places['lga']} LGAs."
        )
        if places["state"]:
            self.stdout.write(f"{places['state']} placed at state level.")
        if not commit:
            self.stdout.write("Dry run: nothing was saved. Add --commit to import.")
            return

        with transaction.atomic():
            for person in accepted:
                registration.register_imported(
                    actor=actor,
                    full_name=person.name,
                    phone_number=person.phone,
                    password=person.phone,
                    source=source,
                    verified=False,
                    **person.place,
                )
        self.stdout.write(
            self.style.SUCCESS(
                f"Imported {len(accepted)} members, with no post; "
                "their identity is not checked yet."
            )
        )

    def _check(self, path: Path, state):
        """The people to import and the rows held back, with their reasons."""
        with path.open(encoding="utf-8-sig", newline="") as handle:
            records = list(enumerate(csv.DictReader(handle), start=2))
        rows = [
            (
                (record.get("hold") or "").strip(),
                Person(
                    line=line,
                    sn=(record.get("sn") or "").strip(),
                    name=person_name(record.get("name") or ""),
                    phone=re.sub(r"\D", "", record.get("phone") or ""),
                    place=place_in((record.get("place") or "").strip(), state),
                ),
            )
            for line, record in records
        ]

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

        accepted, held, seen = [], [], set()
        for hold, row in rows:
            if hold:
                reason = f"held by the list: {hold}"
            elif not row.name:
                reason = "no name"
            elif not MOBILE.match(row.phone):
                reason = "not a valid Nigerian mobile number"
            elif row.phone in shared:
                reason = "phone number shared with another row"
            elif row.place is None:
                reason = "place not in this state's INEC register"
            elif row.phone in moved:
                reason = "the same person has another place on another row"
            elif row.phone in seen:
                reason = "the same person listed again (imported once)"
            elif registered(row.phone):
                reason = "phone number registered already"
            else:
                accepted.append(row)
                seen.add(row.phone)
                continue
            held.append((row.line, row.sn, reason))
        return accepted, held

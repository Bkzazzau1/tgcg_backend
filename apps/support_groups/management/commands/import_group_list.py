"""Place the people of a support group's list in the group (4 November 2026,
decided with the owner for the Directorate of Contact and Mobilization).

Takes the JSON read_group_list wrote. Someone already a member, by the same
VIN or phone, is placed in the group at once and told in the app; anyone
else is registered, with no post and the identity not checked, by --by, a
National Administrator, on the group's word that they agreed. A member
without a phone signs in with their VIN and must choose their own PIN
first. See apps.support_groups.group_list.

A dry run unless --commit: it then only checks the group and the actor.
--shard i/n places every n-th person from the i-th, so shards can run side by
side; each person is saved on their own, so a stopped run is simply started
again: whoever was placed is then "already in the group". The report names
people by the first page and S/N of their rows only.
"""

import csv
import json
from collections import Counter
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from apps.access import engine
from apps.accounts.models import User
from apps.support_groups import group_list
from apps.support_groups.models import SupportGroup
from apps.support_groups.places import Places


class Command(BaseCommand):
    help = "Place the people of a group's list in the group (dry run unless --commit)."

    def add_arguments(self, parser):
        parser.add_argument("people_path", help="The JSON from read_group_list.")
        parser.add_argument("--group", required=True, help="The group's serial number.")
        parser.add_argument("--by", required=True, help="A National Administrator's access ID.")
        parser.add_argument("--source", required=True, help="The list, as recorded with each one.")
        parser.add_argument("--report", required=True, help="Where to write each outcome (CSV).")
        parser.add_argument("--shard", default="0/1", help="i/n: every n-th person from the i-th.")
        parser.add_argument("--commit", action="store_true", help="Save; without it, a dry run.")

    def handle(self, *args, people_path, group, by, source, report, shard, commit, **options):
        actor = User.objects.filter(access_id=by).first()
        if actor is None or not engine.is_national_administrator(actor):
            raise CommandError("--by must be the access ID of a National Administrator.")
        found = SupportGroup.objects.filter(serial_number=group).first()
        if found is None or found.status != SupportGroup.Status.APPROVED:
            raise CommandError("--group must be an approved support group's serial number.")
        try:
            index, count = (int(part) for part in shard.split("/"))
        except ValueError as exc:
            raise CommandError("--shard is i/n, e.g. 0/2.") from exc
        if not 0 <= index < count:
            raise CommandError("--shard is i/n with 0 <= i < n.")
        people = group_list.from_json(json.loads(Path(people_path).read_text(encoding="utf-8")))
        mine = people[index::count]
        self.stdout.write(
            f"{found.serial_number} {found.name}: {len(mine)} of {len(people)} people"
        )
        if not commit:
            self.stdout.write("Dry run: nothing was saved. Add --commit to place them.")
            return

        places = Places()
        outcomes = Counter()
        with Path(report).open("a", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            for number, person in enumerate(mine, start=1):
                outcome = group_list.place_person(
                    actor=actor, group=found, person=person, places=places, source=source
                )
                outcomes[outcome.split(":")[0] if outcome.startswith("held") else outcome] += 1
                page, serial = person.rows[0]
                writer.writerow([page, serial, outcome])
                if number % 500 == 0:
                    handle.flush()
                    self.stdout.write(f"{number} done: {dict(outcomes)}")
        self.stdout.write(self.style.SUCCESS(f"Finished {len(mine)}: {dict(outcomes)}"))

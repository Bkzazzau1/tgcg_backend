"""Read a support group's members' list (a PDF table) where the list is,
and write the people to place as JSON for import_group_list, and the rows
held back. See apps.support_groups.group_list, and for a spreadsheet's table
printed to PDF (--table), apps.support_groups.table_list.

Saves nothing in the database. The held rows are named by page and S/N
only, with the reason, for the group to correct in its own list; counts are
all that is printed. Bank details are never read.
"""

import csv
import json
from collections import Counter
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from pypdf import PdfReader

from apps.support_groups import group_list, table_list


def _signs_in(person) -> str:
    has = [label for label, value in (("VIN", person.vin), ("phone", person.phone)) if value]
    if not has:
        return "NIN only" if person.nin else "nothing to sign in with"
    return " and ".join(has) if len(has) > 1 else f"{has[0]} only"


class Command(BaseCommand):
    help = "Read a group's list PDF into people to import, and rows held back (counts only)."

    def add_arguments(self, parser):
        parser.add_argument("pdf_path")
        parser.add_argument("--people", required=True, help="Where to write the people (JSON).")
        parser.add_argument("--held", required=True, help="Where to write the held rows (CSV).")
        parser.add_argument(
            "--table",
            action="store_true",
            help="A spreadsheet's table printed to PDF, with a header naming its columns.",
        )
        parser.add_argument("--state", default="", help="Everyone's state, when no column has it.")

    def handle(self, *args, pdf_path, people, held, table, state, **options):
        path = Path(pdf_path)
        if not path.is_file():
            raise CommandError(f"No such file: {path}")
        reader = PdfReader(path)
        if table:
            rows = table_list.read_pages(
                (table_list.page_pieces(page) for page in reader.pages), state=state
            )
        else:
            rows = group_list.read_pages(
                page.extract_text(extraction_mode="layout") for page in reader.pages
            )
        found, kept_back = group_list.plan(rows)
        Path(people).write_text(json.dumps(group_list.to_json(found)), encoding="utf-8")
        with Path(held).open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["page", "sn", "reason"])
            for row, reason in kept_back:
                writer.writerow([row.page, row.serial, reason])

        kinds = Counter(_signs_in(person) for person in found)
        reasons = Counter(reason.split(" with ")[0].split(" + ")[0] for _, reason in kept_back)
        self.stdout.write(f"pages: {len(reader.pages)}, rows: {len(rows)}")
        self.stdout.write(f"people to place: {len(found)} ({dict(kinds)})")
        self.stdout.write(f"rows held back: {len(kept_back)} ({dict(reasons)})")
        self.stdout.write(
            f"with a place: {sum(1 for p in found if p.state or p.lga or p.ward or p.unit)}"
        )

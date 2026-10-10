from django.core.management.base import BaseCommand
from django.db import transaction

from apps.geography.seed import DATA_FILE, load_canonical_geography


class Command(BaseCommand):
    help = "Load or refresh canonical zones, states, senatorial districts and LGAs."

    def add_arguments(self, parser):
        parser.add_argument(
            "--file",
            default=str(DATA_FILE),
            help="Geography JSON exported by the Flutter tool/export_geography.dart.",
        )

    @transaction.atomic
    def handle(self, *args, **options):
        counts = load_canonical_geography(data_file=options["file"])
        summary = ", ".join(f"{count} {level}" for level, count in counts.items())
        self.stdout.write(self.style.SUCCESS(f"Geography loaded: {summary}."))

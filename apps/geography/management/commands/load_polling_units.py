import time

from django.core.management.base import BaseCommand, CommandError

from apps.geography.polling_units import DATA_FILE, PollingUnitDataError, load_polling_units


class Command(BaseCommand):
    help = "Load or refresh INEC wards and polling units (176,846 units nationwide)."

    def add_arguments(self, parser):
        parser.add_argument("--file", default=str(DATA_FILE))
        parser.add_argument(
            "--lga",
            action="append",
            dest="lgas",
            help="Only load this canonical LGA code (repeatable), e.g. --lga KD-ZARIA.",
        )

    def handle(self, *args, **options):
        started = time.monotonic()
        try:
            counts = load_polling_units(options["file"], lga_codes=options["lgas"])
        except PollingUnitDataError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(
            self.style.SUCCESS(
                f"Loaded {counts['polling_units']:,} polling units in {counts['wards']:,} "
                f"wards across {counts['lgas']} LGAs ({time.monotonic() - started:.1f}s)."
            )
        )
        if counts["not_in_file"]:
            self.stdout.write(
                self.style.WARNING(
                    f"{counts['not_in_file']:,} existing polling units are not in this file "
                    "and were left unchanged."
                )
            )

from django.core.management.base import BaseCommand, CommandError

from apps.geography.inec_locations import DATA_FILE, InecLocationDataError, load_inec_locations


class Command(BaseCommand):
    help = (
        "Load polling unit locations from INEC's Polling Unit Locator. Field locations "
        "and units already checked by a coordinator are never overwritten."
    )

    def add_arguments(self, parser):
        parser.add_argument("--file", default=str(DATA_FILE))

    def handle(self, *args, **options):
        try:
            counts = load_inec_locations(options["file"])
        except (InecLocationDataError, FileNotFoundError) as exc:
            raise CommandError(str(exc)) from exc
        applied = counts["imported"] + counts["updated"]
        self.stdout.write(
            self.style.SUCCESS(
                f"INEC locations applied to {applied:,} polling units "
                f"({counts['imported']:,} new, {counts['updated']:,} updated, "
                f"{counts['unchanged']:,} unchanged)."
            )
        )
        kept = {
            "kept_field_location": "have a location from the field",
            "kept_checked_by_coordinator": "were already checked by a coordinator",
            "kept_problem_report": "have a problem report",
        }
        for key, reason in kept.items():
            if counts[key]:
                self.stdout.write(f"Left unchanged: {counts[key]:,} units {reason}.")
        rejected = {key: value for key, value in counts.items() if key.startswith("rejected_")}
        for key, value in sorted(rejected.items()):
            self.stdout.write(
                self.style.WARNING(f"Not imported ({key.removeprefix('rejected_')}): {value:,}")
            )
        if counts["unknown_code"]:
            self.stdout.write(
                self.style.WARNING(f"{counts['unknown_code']:,} codes are not in the register.")
            )

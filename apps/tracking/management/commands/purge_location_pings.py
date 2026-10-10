from django.conf import settings
from django.core.management.base import BaseCommand

from apps.tracking.services import purge_old_pings


class Command(BaseCommand):
    help = "Delete on-duty location pings older than the retention period (run daily)."

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=settings.LOCATION_PING_RETENTION_DAYS)

    def handle(self, *args, **options):
        deleted = purge_old_pings(options["days"])
        self.stdout.write(self.style.SUCCESS(f"Deleted {deleted:,} location pings."))

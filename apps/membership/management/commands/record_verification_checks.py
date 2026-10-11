"""Record identity checks for members who registered before checks existed.

    python manage.py record_verification_checks          # members without checks
    python manage.py record_verification_checks --all    # every member again

Coordinators' decisions (verified or rejected) are kept; everyone else is
verified only if every check passed, or otherwise waits for review.
"""

from django.core.management.base import BaseCommand

from apps.membership import identity
from apps.membership.models import Member


class Command(BaseCommand):
    help = "Record identity checks for members (by default, those without any)."

    def add_arguments(self, parser):
        parser.add_argument("--all", action="store_true", help="Run for every member again.")

    def handle(self, *args, **options):
        members = Member.objects.order_by("pk")
        if not options["all"]:
            members = members.filter(verification_checks__isnull=True)
        count = 0
        for member in members.iterator():
            identity.record_checks(member)
            count += 1
        self.stdout.write(self.style.SUCCESS(f"Recorded checks for {count} member(s)."))

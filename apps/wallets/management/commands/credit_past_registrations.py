"""Credit registrars, once, for members who signed in before credits started.

    python manage.py credit_past_registrations           # counts only, credits nothing
    python manage.py credit_past_registrations --commit  # credits them

Credits are earned at a registered member's first sign-in. Members who had
already signed in when credits started (23 October 2026) never earned their
registrar anything; on 24 October 2026 the user decided to credit those
registrations too. This finds every member registered at /me/registered/
who has signed in but earned no credit, and credits their registrar at
today's rate, recorded in the audit log as a backfill. A registrar without a
membership has no wallet and is skipped. Prints counts only.

It refuses to run if credits were ever paused (a rate of 0 credits): a first
sign-in during a pause earns nothing by design, and this command cannot tell
those apart.
"""

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db.models import Exists, OuterRef

from apps.membership.models import Member
from apps.wallets import registrars, services
from apps.wallets.models import CreditRate, WalletEntry


class Command(BaseCommand):
    help = "Credit registrars once for members who signed in before credits started."

    def add_arguments(self, parser):
        parser.add_argument("--commit", action="store_true", help="Credit them (else a dry run).")

    def handle(self, *args, commit=False, **options):
        if settings.CREDITS_EARNED_ON != "signIn":
            raise CommandError("CREDITS_EARNED_ON is not signIn: a sign-in alone earns nothing.")
        if CreditRate.objects.filter(credits_per_registration=0).exists():
            raise CommandError("Credits were paused at some point: decide these by hand.")
        rate = services.current_rate()
        if not rate.credits_per_registration:
            raise CommandError("Credits are paused.")

        due = (
            registrars.referred()
            .filter(registrars.SIGNED_IN)
            .exclude(Exists(WalletEntry.objects.filter(registered_member=OuterRef("pk"))))
        )
        earners = set(
            Member.objects.filter(user_id__in=due.values("created_by_id")).values_list(
                "user_id", flat=True
            )
        )
        payable = due.filter(created_by_id__in=earners)
        skipped = due.count() - payable.count()
        self.stdout.write(
            f"{due.count()} signed in without a credit; {payable.count()} to credit "
            f"({payable.count() * rate.credits_per_registration} credits) to "
            f"{payable.values('created_by_id').distinct().count()} registrars; "
            f"{skipped} skipped (registrar has no membership)."
        )
        if not commit:
            self.stdout.write("Dry run: nothing credited. Run again with --commit.")
            return
        credited = 0
        for member in payable.order_by("pk").iterator():
            if services.reward_registration(member, proof="signIn", backfill=True):
                credited += 1
        self.stdout.write(f"Credited {credited} registrations.")

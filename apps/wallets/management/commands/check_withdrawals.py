"""Ask Paystack how the withdrawals being paid stand.

    python manage.py check_withdrawals              # those sent 10 or more minutes ago
    python manage.py check_withdrawals --minutes 0  # every one being paid

Paystack's webhook usually settles a withdrawal within seconds. Run this
every 15 minutes (systemd timer or cron) for the rest: a webhook that never
came, or a transfer request that got no answer. A withdrawal Paystack has no
record of goes back to the member's wallet. Prints counts only.
"""

from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.wallets import paystack, services
from apps.wallets.models import Withdrawal


class Command(BaseCommand):
    help = "Settle withdrawals being paid, by asking Paystack how each stands."

    def add_arguments(self, parser):
        parser.add_argument("--minutes", type=int, default=10)

    def handle(self, *args, **options):
        if not paystack.configured():
            self.stdout.write("Paystack is not set up: nothing to check.")
            return
        since = timezone.now() - timedelta(minutes=options["minutes"])
        waiting = Withdrawal.objects.filter(
            status=Withdrawal.Status.PROCESSING, decided_at__lte=since
        ).select_related("member")
        counts = {"settled": 0, "waiting": 0, "unreachable": 0}
        for withdrawal in waiting:
            try:
                checked = services.check_withdrawal(withdrawal)
            except paystack.Unreachable:
                counts["unreachable"] += 1
                continue
            counts["waiting" if checked.status == Withdrawal.Status.PROCESSING else "settled"] += 1
        self.stdout.write(
            f"Settled {counts['settled']}, still being paid {counts['waiting']}, "
            f"Paystack unreachable for {counts['unreachable']}."
        )

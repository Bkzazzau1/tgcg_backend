"""Send the bulk messages waiting in the queue.

    python manage.py deliver_messages                # until the queue is empty, or 55 s
    python manage.py deliver_messages --seconds 600  # keep going longer

Run it every minute (systemd timer or cron): each run works for up to
`--seconds`, so the next one picks up where it stopped. Two runs never send
the same message: each takes its own batch. Prints counts only.
"""

import time

from django.core.management.base import BaseCommand

from apps.messaging import services


class Command(BaseCommand):
    help = "Send waiting bulk messages (SMS, WhatsApp, app notifications)."

    def add_arguments(self, parser):
        parser.add_argument("--seconds", type=int, default=55)
        parser.add_argument("--batch", type=int, default=500)

    def handle(self, *args, **options):
        deadline = time.monotonic() + options["seconds"]
        handled = 0
        while time.monotonic() < deadline:
            done = services.deliver(limit=options["batch"])
            handled += done
            if not done:
                break
        self.stdout.write(f"Handled {handled} message(s).")

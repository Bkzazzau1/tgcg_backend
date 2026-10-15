"""End live incident videos left open past their time, and copy finished
recordings from the bucket onto their incidents. Run every minute (cron)."""

import logging
from collections import Counter

from botocore.exceptions import BotoCoreError, ClientError
from django.core.management.base import BaseCommand

from apps.field import live
from apps.meetings import livekit

logger = logging.getLogger(__name__)
# Problems worth another try on the next run.
PASSING = (livekit.LiveKitError, livekit.NotConfigured, BotoCoreError, ClientError)


class Command(BaseCommand):
    help = "End overdue live videos and copy finished recordings onto their incidents."

    def handle(self, *args, **options):
        ended = live.end_overdue()
        results = Counter()
        for stream in live.needs_collecting():
            try:
                results[live.collect(stream)] += 1
            except PASSING as error:
                logger.warning("Recording of live video %s waits: %s", stream.uuid, error)
                results["retry"] += 1
        self.stdout.write(
            f"Ended {ended} overdue live video(s). Recordings: {results['saved']} saved, "
            f"{results['failed']} failed, {results['waiting'] + results['retry']} waiting."
        )

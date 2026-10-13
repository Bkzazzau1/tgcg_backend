"""Remove expired USSD drafts and callback records, retaining all members."""

from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from apps.membership.models import UssdRegistrationDraft, UssdSession


class Command(BaseCommand):
    help = "Purge USSD progress older than USSD_DRAFT_TTL_DAYS (default: 30)."

    def handle(self, *args, **options):
        try:
            days = int(getattr(settings, "USSD_DRAFT_TTL_DAYS", 30))
        except (TypeError, ValueError) as error:
            raise CommandError("USSD_DRAFT_TTL_DAYS must be a positive integer.") from error
        if days <= 0:
            raise CommandError("USSD_DRAFT_TTL_DAYS must be a positive integer.")
        cutoff = timezone.now() - timedelta(days=days)
        with transaction.atomic():
            # Callbacks lock draft before session. Acquire the same locks first:
            # deleting a draft also updates its sessions through SET_NULL.
            stale_draft_ids = list(
                UssdRegistrationDraft.objects.filter(updated_at__lt=cutoff)
                .order_by("pk")
                .select_for_update()
                .values_list("pk", flat=True)
            )
            _, sessions = UssdSession.objects.filter(updated_at__lt=cutoff).delete()
            # A callback may have refreshed a candidate while the purge waited.
            _, drafts = UssdRegistrationDraft.objects.filter(
                pk__in=stale_draft_ids, updated_at__lt=cutoff
            ).delete()
        session_count = sessions.get(UssdSession._meta.label, 0)
        draft_count = drafts.get(UssdRegistrationDraft._meta.label, 0)
        self.stdout.write(
            self.style.SUCCESS(
                f"Purged {draft_count} USSD draft(s) and {session_count} session(s) "
                f"inactive for more than {days} day(s)."
            )
        )

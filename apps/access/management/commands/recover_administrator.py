"""Server recovery: appoint a National Administrator while too few are active
for two of them to approve action on a third (docs/access-control.md §7).

    python manage.py recover_administrator --member TGCG-000123 \
        --reason "Two administrators unreachable; approved by the board on 12 Oct" --confirm

Without --confirm it only checks. It never suspends or removes anyone, and it
is not reachable through the API: only someone with access to the server can
run it. Every use is written to the audit log.
"""

import getpass

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.access import services
from apps.membership.models import Member


class Command(BaseCommand):
    help = "Appoint a National Administrator while fewer than three are active (server recovery)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--member",
            required=True,
            help="Membership number of a verified, active member with an account.",
        )
        parser.add_argument(
            "--reason", required=True, help="Why recovery is needed and who approved it."
        )
        parser.add_argument(
            "--confirm", action="store_true", help="Carry it out (otherwise only check)."
        )

    def handle(self, *args, **options):
        number = options["member"].strip().upper()
        member = Member.objects.filter(membership_number=number).first()
        if member is None:
            raise CommandError(f"No member {number}.")
        active = services.active_national_administrators()
        self.stdout.write(f"Active National Administrators: {len(active)}")
        try:
            with transaction.atomic():
                assignment = services.recover_administrator(
                    member=member, reason=options["reason"], operator=_operator()
                )
                if not options["confirm"]:
                    transaction.set_rollback(True)
        except ValidationError as error:
            raise CommandError(" ".join(error.messages)) from error
        if options["confirm"]:
            self.stdout.write(
                self.style.SUCCESS(
                    f"{member.full_name} ({number}) is now a National Administrator "
                    f"(appointment {assignment.uuid})."
                )
            )
        else:
            self.stdout.write("Checks passed; nothing was changed. Re-run with --confirm.")


def _operator() -> str:
    try:
        return getpass.getuser()
    except Exception:  # noqa: BLE001 - no login name available
        return ""

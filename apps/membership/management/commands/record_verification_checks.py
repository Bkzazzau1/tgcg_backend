"""Record identity checks for members.

    python manage.py record_verification_checks             # members without checks
    python manage.py record_verification_checks --awaiting  # members awaiting a decision
    python manage.py record_verification_checks --all       # every member again

Add --dry-run to count what would happen, without recording or reading anything.
Add --recompare (with --awaiting) after the comparison itself changed: cards
that did not match are read and compared again.

Decisions already made (verified by a coordinator or automatically, or
rejected) are kept. A member awaiting one has their card photo read when no
comparison covers their name, VIN and photo as they are now; the photo goes
to this server's reader (Google Cloud Vision when its key is set). They are
verified automatically when the policy allows (apps.membership.identity), or
otherwise wait for a coordinator's review. Only counts are printed.
"""

from django.core.management.base import BaseCommand

from apps.core.choices import VerificationStatus
from apps.membership import identity
from apps.membership.models import Member, VerificationCheck

DECIDED = (VerificationStatus.VERIFIED, VerificationStatus.REJECTED)
K = VerificationCheck.Kind
R = VerificationCheck.Result


def _awaiting(member) -> bool:
    return member.verification_status not in DECIDED


def _card_to_read(member) -> bool:
    return (
        _awaiting(member)
        and bool(member.pvc_image and member.vin)
        and not identity.card_compared(member)
    )


# What came of reading a card, in the order they are reported.
OUTCOMES = (
    "matched",
    "VIN a few characters off",
    "name not found",
    "VIN not found",
    "name and VIN not found",
    "not read",
)


def _outcome(check) -> str:
    """What the comparison found, from its recorded result (never the details)."""
    detail = check.detail
    if check.result == R.PASSED:
        return "matched"
    if "differs from the one given" in detail and "name" not in detail:
        return "VIN a few characters off"
    if detail.startswith("The name and VIN given could not be found"):
        return "name and VIN not found"
    if detail.startswith("The name given could not be found"):
        return "name not found"
    if detail.startswith("The VIN given could not be found"):
        return "VIN not found"
    return "not read"


class Command(BaseCommand):
    help = "Record identity checks for members (by default, those without any)."

    def add_arguments(self, parser):
        which = parser.add_mutually_exclusive_group()
        which.add_argument("--all", action="store_true", help="Run for every member again.")
        which.add_argument(
            "--awaiting", action="store_true", help="Run for members awaiting a decision."
        )
        parser.add_argument(
            "--dry-run", action="store_true", help="Count only: record and read nothing."
        )
        parser.add_argument(
            "--recompare",
            action="store_true",
            help="Read again the cards of members awaiting a decision whose card did not match.",
        )

    def handle(self, *args, **options):
        members = Member.objects.order_by("pk")
        if options["awaiting"]:
            members = members.exclude(verification_status__in=DECIDED)
        elif not options["all"]:
            members = members.filter(verification_checks__isnull=True)
        unmatched = VerificationCheck.objects.filter(
            member__in=members.exclude(verification_status__in=DECIDED), kind=K.CARD_MATCH
        ).exclude(result=R.PASSED)
        if options["recompare"] and options["dry_run"]:
            self.stdout.write(
                f"{unmatched.count()} card(s) that did not match would be read again."
            )
        elif options["recompare"]:
            # No comparison covers them any more, so their cards are read again.
            unmatched.update(basis="")
        if options["dry_run"]:
            self._count(members)
            return
        count = read = unread = verified = 0
        outcomes: dict[str, int] = {}
        for member in members.iterator():
            to_read, awaiting = _card_to_read(member), _awaiting(member)
            member = identity.record_checks(member)
            count += 1
            if to_read:
                read += 1
                unread += not identity.card_compared(member)
                outcome = _outcome(member.verification_checks.get(kind=K.CARD_MATCH))
                outcomes[outcome] = outcomes.get(outcome, 0) + 1
            verified += awaiting and member.verification_status == VerificationStatus.VERIFIED
        self.stdout.write(self.style.SUCCESS(f"Recorded checks for {count} member(s)."))
        if read:
            self.stdout.write(f"Read {read} card photo(s); {unread} could not be read this time.")
            self.stdout.write(
                "Card comparisons: "
                + "; ".join(f"{label} {outcomes[label]}" for label in OUTCOMES if label in outcomes)
                + "."
            )
        if verified:
            self.stdout.write(f"{verified} member(s) verified automatically.")

    def _count(self, members):
        count = awaiting = to_read = if_matched = now = 0
        for member in members.iterator():
            count += 1
            if not _awaiting(member):
                continue
            awaiting += 1
            results = {kind: result for kind, (result, _) in identity.assess(member).items()}
            if _card_to_read(member):
                to_read += 1
                if_matched += identity.meets_policy(member, {**results, K.CARD_MATCH: R.PASSED})
                continue
            stored = member.verification_checks.filter(kind=K.CARD_MATCH).first()
            if stored is not None and identity.card_compared(member):
                now += identity.meets_policy(member, {**results, K.CARD_MATCH: stored.result})
        self.stdout.write(f"{count} member(s) selected; {awaiting} await a decision.")
        self.stdout.write(
            f"{to_read} card photo(s) would be read; {if_matched} of those members would be "
            "verified automatically if their card matches."
        )
        if now:
            self.stdout.write(f"{now} member(s) would be verified automatically now.")
        self.stdout.write("Dry run: nothing was recorded and no card was read.")

"""Who registers members at /me/registered/, how many, and how many of those
have signed in and earned their registrar a credit (24 October 2026).

A registrar earns at the member's first sign-in (see services.reward_registration),
so "waiting" are the members who have not signed in yet.
"""

from django.db.models import Count, Exists, F, IntegerField, Max, OuterRef, Q, Subquery, Sum, Value
from django.db.models.functions import Coalesce

from apps.accounts.models import User
from apps.core.choices import RegistrationChannel
from apps.core.permissions import Capability
from apps.geography.scope import SCOPE_FIELDS, any_of, within_scope_q
from apps.membership.models import Member

from .models import WalletEntry

C = Capability
# Any of these shows the registrars in the holder's area (finance staff and
# the National Administrator).
SEEING = (
    C.VIEW_WALLETS,
    C.CREDIT_WALLETS,
    C.APPROVE_WALLET_CREDITS,
    C.APPROVE_WITHDRAWALS,
    C.MANAGE_CREDIT_RATES,
)

SIGNED_IN = Q(user__last_login__isnull=False)


def referred():
    """Members someone registered at /me/registered/."""
    return Member.objects.filter(
        registration_channel=RegistrationChannel.REFERRED, created_by__isnull=False
    )


def _count(condition=None):
    rows = (
        referred()
        .filter(condition or Q(), created_by=OuterRef("pk"))
        .order_by()
        .values("created_by")
        .annotate(n=Count("pk"))
        .values("n")
    )
    return Coalesce(Subquery(rows[:1], output_field=IntegerField()), Value(0))


def registrars():
    """Every account that registered someone, with its counts."""
    earned = (
        WalletEntry.objects.filter(
            kind=WalletEntry.Kind.REWARD, registered_member__created_by=OuterRef("pk")
        )
        .order_by()
        .values("registered_member__created_by")
        .annotate(n=Sum("credits"))
        .values("n")
    )
    latest = referred().filter(created_by=OuterRef("pk")).order_by().values("created_by")
    return (
        User.objects.filter(Exists(referred().filter(created_by=OuterRef("pk"))))
        .annotate(
            registered=_count(),
            signed_in=_count(SIGNED_IN),
            credited=_count(Q(registration_reward__isnull=False)),
            credits_earned=Coalesce(Subquery(earned[:1], output_field=IntegerField()), Value(0)),
            last_registered_at=Subquery(latest.annotate(at=Max("created_at")).values("at")[:1]),
        )
        .annotate(waiting=F("registered") - F("signed_in"))
        .select_related(
            "member__wallet",
            *SCOPE_FIELDS,
            *(f"member__{field}" for field in SCOPE_FIELDS),
        )
    )


def visible_q(user) -> Q:
    """Registrars in the user's reach: by the member's place, or, for an
    account without a membership, by the account's area."""
    conditions = []
    for permission in SEEING:
        as_member = within_scope_q(user, "member__", permission=permission)
        if not as_member:  # national
            return Q()
        as_account = within_scope_q(user, permission=permission)
        conditions.append(
            (Q(member__isnull=False) & as_member) | (Q(member__isnull=True) & as_account)
        )
    return any_of(*conditions)


def in_area_q(*, state: str = "", lga: str = "") -> Q:
    """Registrars from one state or LGA (by code)."""
    condition = Q()
    for field, code in (("state", state), ("lga", lga)):
        if code:
            condition &= Q(**{f"member__{field}__code": code}) | Q(
                member__isnull=True, **{f"{field}__code": code}
            )
    return condition


def people_seen_by_q(user) -> Q:
    """A registrar's people the user may see: those in the user's reach for
    any of SEEING, and those registered without a place, who belong to no
    area but their registrar's."""
    in_reach = any_of(*(within_scope_q(user, permission=permission) for permission in SEEING))
    if not in_reach:  # national
        return Q()
    return Q(pk__in=Member.objects.filter(in_reach).values("pk")) | Q(state__isnull=True)


def counts(people) -> dict:
    """How many of these registered members signed in, are waiting, and
    earned a credit."""
    totals = people.aggregate(
        registered=Count("pk"),
        signed_in=Count("pk", filter=SIGNED_IN),
        credited=Count("pk", filter=Q(registration_reward__isnull=False)),
    )
    return {**totals, "waiting": totals["registered"] - totals["signed_in"]}


def own_counts(user) -> dict:
    """What a registrar sees in their own wallet."""
    return counts(referred().filter(created_by=user))

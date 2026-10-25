"""Referral codes (25 October 2026): every member has a 7-digit code, digits
only so it can be typed on USSD. Someone who joins in the app with it (typed,
or filled in from the member's link) is "referred by" them; the member then
earns registration credits as for people they register themselves (see
apps.wallets.services.reward_registration). Someone who joined by USSD gives
the code afterwards, from the USSD menu.
"""

import secrets

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from apps.core.choices import MembershipState, RegistrationChannel

from .models import Member

DIGITS = 7
NOT_FOUND = "That referral code was not found. Check it, or leave it empty."


def code_for(member: Member) -> str:
    """The member's code, made the first time it is asked for."""
    if member.referral_code:
        return member.referral_code
    for _ in range(20):
        code = str(secrets.randbelow(9 * 10 ** (DIGITS - 1)) + 10 ** (DIGITS - 1))
        try:
            with transaction.atomic():
                updated = Member.objects.filter(pk=member.pk, referral_code__isnull=True).update(
                    referral_code=code
                )
        except IntegrityError:
            continue  # another member has it
        if not updated:  # made meanwhile by another request
            member.refresh_from_db(fields=["referral_code"])
            return member.referral_code
        member.referral_code = code
        return code
    raise RuntimeError("Could not make a referral code.")


def link_for(code: str) -> str:
    """The web link that opens joining with the code filled in."""
    return f"{settings.APP_PUBLIC_URL.rstrip('/')}/?ref={code}"


def clean(code: str) -> str:
    return "".join(char for char in str(code or "") if char.isdigit())


def referrer(code: str):
    """The account whose code this is: an active member with an app account."""
    code = clean(code)
    if len(code) != DIGITS:
        return None
    owner = (
        Member.objects.select_related("user")
        .filter(
            referral_code=code,
            membership_state=MembershipState.ACTIVE,
            user__isnull=False,
            user__is_active=True,
        )
        .first()
    )
    return owner.user if owner else None


def referrer_or_error(code: str):
    found = referrer(code)
    if found is None:
        raise ValidationError({"referral_code": NOT_FOUND})
    return found


@transaction.atomic
def record_later(*, member: Member, code: str) -> Member:
    """A member who joined by USSD gives who invited them, once."""
    member = Member.objects.select_for_update().get(pk=member.pk)
    if member.registration_channel != RegistrationChannel.USSD:
        raise ValidationError({"referral_code": "Give a referral code when you join."})
    if member.referred_by_id:
        raise ValidationError({"referral_code": "A referral code was already given."})
    found = referrer_or_error(code)
    if member.user_id and found.pk == member.user_id:
        raise ValidationError({"referral_code": NOT_FOUND})
    member.referred_by = found
    member.save(update_fields=["referred_by", "updated_at"])
    return member

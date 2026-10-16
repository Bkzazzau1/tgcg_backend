"""Who may download their membership card, and what it shows.

A card needs an active membership and the member's own photo, a photo of
their PVC, their gender and their date of birth; the identity need not be
checked. Roles print only once the identity is checked, because holding a
role needs that check.
"""

from django.utils import timezone

from apps.access.models import AssignmentStatus
from apps.core.choices import AccreditationStatus, MembershipState, VerificationStatus

# What a member still needs before downloading their card, as the app asks for it.
MISSING = {
    "active_membership": "an active membership",
    "full_name": "your full name",
    "photo": "a photo of you",
    "pvc_photo": "a photo of your PVC",
    "gender": "your gender",
    "date_of_birth": "your date of birth",
}


def card_missing(member) -> list[str]:
    """What stands between the member and their card; empty when it is ready."""
    checks = {
        "active_membership": member.status not in ("rejected", "archived")
        and member.membership_state == MembershipState.ACTIVE,
        "full_name": bool(member.full_name),
        "photo": bool(member.photo),
        "pvc_photo": bool(member.pvc_image),
        "gender": bool(member.gender),
        "date_of_birth": member.date_of_birth is not None,
    }
    return [name for name, done in checks.items() if not done]


def missing_message(missing) -> str:
    return "To download your card, add " + ", ".join(MISSING[name] for name in missing) + "."


def card_roles(member, now=None) -> list[str]:
    """The roles printed on the card: current appointments and an approved
    agent accreditation, once the member's identity is checked."""
    if member.verification_status != VerificationStatus.VERIFIED:
        return []
    now = now or timezone.now()
    names = [
        assignment.role.name
        for assignment in member.role_assignments.select_related("role")
        if assignment.effective_status(now) == AssignmentStatus.ACTIVE
    ]
    names += [
        agent.get_role_display()
        for agent in member.accreditations.all()
        if agent.status == AccreditationStatus.APPROVED
    ]
    return list(dict.fromkeys(names))

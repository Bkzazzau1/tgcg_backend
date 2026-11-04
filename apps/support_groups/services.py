"""Support groups: registering and vetting them, people joining and leaving,
and the leader's messages. See apps.support_groups.models.

Decided with the owner on 3 November 2026: only those who vet groups (the
power manageSupportGroups, held for all of Nigeria) and a group's own leader
see who is in it. A person may be in several groups and is counted once in
the totals. The leader's messages stay in the app, never SMS. People the
group registers earn TGC tokens for whoever registered them, as anyone's do.
"""

import secrets
from datetime import timedelta

from django.core.exceptions import PermissionDenied, ValidationError
from django.core.validators import validate_email
from django.db import IntegrityError, transaction
from django.db.models import Count, Q
from django.utils import timezone
from django.utils.text import slugify

from apps.access import audit, engine
from apps.accounts.models import User
from apps.accounts.phone import local_ng_phone, normalize_ng_phone
from apps.core.choices import MembershipState, VerificationStatus
from apps.core.models import Sequence
from apps.core.permissions import Capability
from apps.geography.models import State, Zone
from apps.membership import referrals, registration
from apps.membership.models import Member
from apps.messaging.models import IN_APP, Broadcast, Delivery
from apps.messaging.services import notify

from .models import GroupKind, GroupMember, SupportGroup

S = SupportGroup.Status
M = GroupMember.Status
VIA = GroupMember.Via

MAX_OFFICIALS = 3
# Applications one person may have waiting for vetting at once.
MAX_WAITING = 5
# People added in one request: each new one gets an account, which is slow.
MAX_BATCH = 25
MESSAGES_PER_DAY = 10
CODE_DIGITS = 7
CODE_NOT_FOUND = "That group code was not found. Check it with the group's leader."
# Where members answer, said in every notice that asks them something.
WHERE = "Open Account, then Support groups"


# --- who may do what -------------------------------------------------------------


def vets(user) -> bool:
    """Vetting groups and seeing every group's members need the power for all of Nigeria."""
    if not getattr(user, "is_authenticated", False):
        return False
    return any(
        Capability.MANAGE_SUPPORT_GROUPS in grant.permissions and grant.is_national
        for grant in engine.active_grants(user)
    )


def require_vetter(user):
    if not vets(user):
        raise PermissionDenied("Only those who vet support groups may do this.")


def leads(user, group: SupportGroup) -> bool:
    return bool(user.is_authenticated and group.leader.user_id == user.pk)


def require_leader(user, group: SupportGroup):
    """The group's own leader, while the group is approved."""
    if not leads(user, group):
        raise PermissionDenied("Only the group's leader may do this.")
    if group.status == S.PENDING:
        raise ValidationError("TGCG has not vetted this group yet.")
    if group.status != S.APPROVED:
        raise ValidationError(f"This group is {group.get_status_display().lower()}.")


def _active_member(user) -> Member:
    member = getattr(user, "member", None)
    if member is None:
        raise PermissionDenied("Only members can do this.")
    if member.membership_state != MembershipState.ACTIVE:
        raise PermissionDenied("Your membership is not active.")
    return member


# --- registering an organization --------------------------------------------------


def clean_details(
    *, name="", kind="", coverage="", zone="", state="", email="", officials=()
) -> dict:
    """The organization's details as the form gives them, checked. Errors
    name the form's fields, which all start with organization_."""
    errors = {}
    name = " ".join((name or "").split())
    if len(name) < 3:
        errors["organization_name"] = "Enter the organization's name."
    elif len(name) > 150:
        errors["organization_name"] = "Shorten the name to 150 letters."
    found_kind = GroupKind.objects.filter(code=(kind or "").strip(), offered=True).first()
    if found_kind is None:
        errors["organization_kind"] = "Choose the type of group."
    found_zone = found_state = None
    if coverage not in SupportGroup.Coverage.values:
        errors["organization_coverage"] = "Choose national, regional or state."
    elif coverage == SupportGroup.Coverage.REGIONAL:
        found_zone = Zone.objects.filter(code=zone or "").first()
        if found_zone is None:
            errors["organization_zone"] = "Choose the region it covers."
    elif coverage == SupportGroup.Coverage.STATE:
        found_state = State.objects.filter(code=state or "").first()
        if found_state is None:
            errors["organization_state"] = "Choose the state it covers."
    email = (email or "").strip().lower()
    if email:
        try:
            validate_email(email)
        except ValidationError:
            errors["organization_email"] = "Enter a valid email address."
    people = []
    for official in officials or ():
        if not isinstance(official, dict):
            errors["organization_officials"] = "List each leader with a name, position and phone."
            break
        person = {
            key: " ".join(str(official.get(key) or "").split())[:150]
            for key in ("name", "position", "phone")
        }
        if not any(person.values()):
            continue
        phone = normalize_ng_phone(person["phone"]) if person["phone"] else ""
        if not person["name"]:
            errors["organization_officials"] = "Give the name of each leader listed."
        elif phone is None:
            errors["organization_officials"] = f"{person['name']}'s phone is not a Nigerian number."
        person["phone"] = local_ng_phone(phone) if phone else ""
        people.append(person)
    if len(people) > MAX_OFFICIALS:
        errors["organization_officials"] = f"List at most {MAX_OFFICIALS} other leaders."
    if errors:
        raise ValidationError(errors)
    return {
        "name": name,
        "kind": found_kind,
        "coverage": coverage,
        "zone": found_zone,
        "state": found_state,
        "email": email,
        "officials": people,
    }


def _serial_number() -> str:
    """Numbered like the paper registration form: TGCG/0001/BAT/27."""
    return f"TGCG/{Sequence.next_value('support_group'):04d}/BAT/27"


def _record(actor, action, group, **extra):
    audit.record(
        actor=actor,
        action=f"supportGroup.{action}",
        target_type="supportGroup",
        target_id=group.uuid,
        target_label=f"{group.serial_number} {group.name}",
        **extra,
    )


def _check_waiting(actor):
    waiting = SupportGroup.objects.filter(registered_by=actor, status=S.PENDING).count()
    if waiting >= MAX_WAITING:
        raise ValidationError(
            f"You have {waiting} organizations waiting for vetting. Wait for TGCG to vet them."
        )


def reserve_serial_number() -> str:
    """The next serial number, taken first in any transaction that also
    registers a member: the serial's row, then the membership number's, then
    the audit chain's, always in that order, so two registrations never wait
    on each other's locks (4 November 2026)."""
    return _serial_number()


@transaction.atomic
def register_group(
    *, actor, leader: Member, details: dict, leader_said_yes=True, serial_number=None
) -> SupportGroup:
    """Register an organization, waiting for vetting, with `leader` leading it.

    A leader who is not the person registering it, and was already a
    member, leads it only once they say yes in the app; TGCG cannot approve
    it before. A `serial_number` from reserve_serial_number, when the leader
    was registered in the same transaction."""
    if leader.membership_state != MembershipState.ACTIVE:
        raise ValidationError("The leader's membership is not active.")
    _check_waiting(actor)
    now = timezone.now()
    group = SupportGroup.objects.create(
        serial_number=serial_number or _serial_number(),
        leader=leader,
        leader_confirmed_at=now if leader_said_yes else None,
        registered_by=actor,
        **details,
    )
    GroupMember.objects.create(
        group=group,
        member=leader,
        status=M.ACTIVE if leader_said_yes else M.INVITED,
        via=VIA.LEADER,
        added_by=actor,
        answered_at=now if leader_said_yes else None,
    )
    _record(
        actor,
        "registered",
        group,
        changes={
            "coverage": group.coverage,
            "leader": leader.membership_number,
            "leader_said_yes": leader_said_yes,
        },
    )
    if leader_said_yes:
        notify(
            sender=None,
            member=leader,
            title="Organization registered",
            body=f"{group.name} is registered as {group.serial_number}. TGCG will vet it, "
            "and you will hear here when it is approved.",
            sms=False,
        )
    else:
        notify(
            sender=None,
            member=leader,
            title=f"{group.name} names you as its leader",
            body=f"{actor.full_name} registered {group.name} with TGCG and named you as its "
            f"leader. {WHERE} to say yes or no.",
            sms=False,
        )
    return group


def _renamed(error: ValidationError, names: dict) -> ValidationError:
    if not hasattr(error, "error_dict"):
        return error
    return ValidationError(
        {names.get(field, field): messages for field, messages in error.error_dict.items()}
    )


@transaction.atomic
def register_for(
    *, actor, details: dict, leader_name="", leader_phone="", consent=False
) -> SupportGroup:
    """A signed-in member registers an organization: led by themselves, or
    by someone else given by name and phone. A new person is registered as
    a member, as anyone signed in may register people; someone already a
    member is asked in the app."""
    own = _active_member(actor)
    if not (leader_phone or "").strip():
        return register_group(actor=actor, leader=own, details=details)
    phone = normalize_ng_phone(leader_phone)
    if phone is None:
        raise ValidationError({"leader_phone_number": "Enter a valid Nigerian mobile number."})
    existing = Member.objects.filter(phone_number=phone).first()
    if existing is not None:
        return register_group(
            actor=actor, leader=existing, details=details, leader_said_yes=existing.pk == own.pk
        )
    if consent is not True:
        raise ValidationError({"consent": "Confirm that the leader agreed to join TGCG."})
    _check_waiting(actor)
    serial = reserve_serial_number()
    try:
        leader = registration.register_someone(
            actor=actor, full_name=leader_name, phone_number=phone, consent=True
        )
    except ValidationError as error:
        raise _renamed(
            error, {"full_name": "leader_full_name", "phone_number": "leader_phone_number"}
        ) from error
    return register_group(actor=actor, leader=leader, details=details, serial_number=serial)


# --- vetting --------------------------------------------------------------------------


def _locked(group) -> SupportGroup:
    return (
        SupportGroup.objects.select_for_update(of=("self",))
        .select_related("leader")
        .get(pk=group.pk)
    )


def _new_code() -> str:
    for _ in range(20):
        code = str(secrets.randbelow(9 * 10 ** (CODE_DIGITS - 1)) + 10 ** (CODE_DIGITS - 1))
        if not SupportGroup.objects.filter(join_code=code).exists():
            return code
    raise RuntimeError("Could not make a group code.")


def _decide(group, actor, status, reason=""):
    group.status = status
    group.decided_by = actor
    group.decided_at = timezone.now()
    group.decision_reason = reason
    group.save(
        update_fields=[
            "status",
            "join_code",
            "decided_by",
            "decided_at",
            "decision_reason",
            "updated_at",
        ]
    )


def _reason(reason) -> str:
    reason = (reason or "").strip()
    if not reason:
        raise ValidationError({"reason": "Say why, for the leader and the record."})
    return reason[:1000]


@transaction.atomic
def approve(*, actor, group) -> SupportGroup:
    require_vetter(actor)
    group = _locked(group)
    if group.status != S.PENDING:
        raise ValidationError("Only a group waiting for vetting can be approved.")
    if group.leader_confirmed_at is None:
        raise ValidationError("The leader named has not said yes yet.")
    if group.join_code is None:
        group.join_code = _new_code()
    try:
        with transaction.atomic():
            _decide(group, actor, S.APPROVED)
    except IntegrityError:
        group.join_code = _new_code()
        _decide(group, actor, S.APPROVED)
    _record(actor, "approved", group)
    notify(
        sender=None,
        member=group.leader,
        title=f"{group.name} is approved",
        body=f"TGCG approved {group.name} ({group.serial_number}). Add its members under "
        f"Account, then Support groups. People can also join with the group's code, "
        f"{group.join_code}.",
        sms=False,
    )
    return group


@transaction.atomic
def reject(*, actor, group, reason) -> SupportGroup:
    require_vetter(actor)
    reason = _reason(reason)
    group = _locked(group)
    if group.status != S.PENDING:
        raise ValidationError("Only a group waiting for vetting can be turned down.")
    _decide(group, actor, S.REJECTED, reason)
    _record(actor, "rejected", group, reason=reason)
    notify(
        sender=None,
        member=group.leader,
        title=f"{group.name} was not approved",
        body=f"TGCG did not approve {group.name}: {reason}",
        sms=False,
    )
    return group


@transaction.atomic
def suspend(*, actor, group, reason) -> SupportGroup:
    """The leader can no longer add people or send messages. Its members stay."""
    require_vetter(actor)
    reason = _reason(reason)
    group = _locked(group)
    if group.status != S.APPROVED:
        raise ValidationError("Only an approved group can be suspended.")
    _decide(group, actor, S.SUSPENDED, reason)
    _record(actor, "suspended", group, reason=reason)
    notify(
        sender=None,
        member=group.leader,
        title=f"{group.name} is suspended",
        body=f"TGCG suspended {group.name}: {reason}",
        sms=False,
    )
    return group


@transaction.atomic
def restore(*, actor, group) -> SupportGroup:
    require_vetter(actor)
    group = _locked(group)
    if group.status != S.SUSPENDED:
        raise ValidationError("Only a suspended group can be restored.")
    _decide(group, actor, S.APPROVED)
    _record(actor, "restored", group)
    notify(
        sender=None,
        member=group.leader,
        title=f"{group.name} is approved again",
        body=f"TGCG lifted the suspension of {group.name}.",
        sms=False,
    )
    return group


# --- the leader adds and removes people -----------------------------------------------


def _outcome(outcome, detail="", **extra):
    return {"outcome": outcome, "detail": detail, **extra}


def _ask(actor, group, member, given_name, place=None) -> dict:
    """Ask someone already registered with TGCG whether they are in the group."""
    if place is None:
        place = GroupMember(group=group, member=member)
    elif place.status in (M.ACTIVE, M.INVITED):
        return _outcome("already", "Already in the group, or already asked.")
    elif place.status == M.DECLINED:
        return _outcome("declined", "Said no to this group before. They can join with its code.")
    elif place.status == M.LEFT:
        return _outcome("left", "Left this group. They can join again with its code.")
    place.status = M.INVITED
    place.via = VIA.CONFIRMED
    place.given_name = given_name[:150]
    place.added_by = actor
    place.answered_at = place.ended_at = None
    place.save()
    notify(
        sender=None,
        member=member,
        title=f"{group.name} lists you as a member",
        body=f"The support group {group.name} says you are one of its members. {WHERE} to "
        "say yes or no. The group sees you only after you say yes.",
        sms=False,
    )
    _record(actor, "memberAsked", group, changes={"member": member.membership_number})
    return _outcome("asked", "Already a TGCG member: asked in the app to say yes.")


def add_person(*, actor, group, full_name, phone_number, scope=None, gender="") -> dict:
    """Add one person. Someone new gets a TGCG account, with their phone
    number as the first PIN, and is in the group at once; someone already
    registered is asked in the app. Returns what happened."""
    phone = normalize_ng_phone(phone_number)
    if phone is None:
        return _outcome("problem", "Not a Nigerian mobile number.")
    existing = Member.objects.filter(phone_number=phone).first()
    if existing is not None:
        if existing.membership_state != MembershipState.ACTIVE:
            return _outcome("problem", "This person cannot be added.")
        place = GroupMember.objects.filter(group=group, member=existing).first()
        with transaction.atomic():
            return _ask(actor, group, existing, " ".join((full_name or "").split()), place)
    if User.objects.filter(phone_number=phone).exists():
        return _outcome("problem", "This number belongs to a staff account.")
    try:
        with transaction.atomic():
            member = registration.register_someone(
                actor=actor, full_name=full_name, phone_number=phone, consent=True, scope=scope
            )
            if gender in ("male", "female"):
                member.gender = gender
                member.save(update_fields=["gender", "updated_at"])
            GroupMember.objects.create(
                group=group,
                member=member,
                status=M.ACTIVE,
                via=VIA.REGISTERED,
                added_by=actor,
                answered_at=timezone.now(),
            )
            _record(actor, "memberRegistered", group, changes={"member": member.membership_number})
    except ValidationError as error:
        messages = error.messages
        return _outcome("problem", messages[0] if messages else "This person could not be added.")
    notify(
        sender=None,
        member=member,
        title="Welcome to TGCG",
        body=f"{group.name} registered you with TGCG and as one of its members. To leave the "
        f"group, {WHERE[0].lower()}{WHERE[1:]}.",
        sms=False,
    )
    return _outcome("registered", "Registered with TGCG and added.")


def add_people(*, actor, group, people, consent) -> list[dict]:
    """Add a batch from a list or the form; the app sends a long list in batches.

    Each person is saved on their own (the view runs outside the request
    transaction), so the membership number's and the audit chain's locks
    are held for one person at a time, not the whole batch."""
    require_leader(actor, group)
    _active_member(actor)
    if consent is not True:
        raise ValidationError(
            {"consent": "Confirm that these people agreed to join TGCG and this group."}
        )
    if len(people) > MAX_BATCH:
        raise ValidationError({"people": f"Send at most {MAX_BATCH} people at a time."})
    return [
        {
            "line": person.get("line"),
            **add_person(
                actor=actor,
                group=group,
                full_name=person.get("full_name", ""),
                phone_number=person.get("phone_number", ""),
                scope=person.get("scope"),
                gender=person.get("gender", ""),
            ),
        }
        for person in people
    ]


@transaction.atomic
def remove(*, actor, group, place: GroupMember) -> GroupMember:
    require_leader(actor, group)
    place = GroupMember.objects.select_for_update().get(pk=place.pk, group=group)
    if place.via == VIA.LEADER:
        raise ValidationError("The leader cannot be removed from their own group.")
    if place.status not in (M.ACTIVE, M.INVITED):
        raise ValidationError("This person is not in the group.")
    place.status = M.REMOVED
    place.ended_at = timezone.now()
    place.save(update_fields=["status", "ended_at", "updated_at"])
    _record(actor, "memberRemoved", group, changes={"member": place.member.membership_number})
    return place


# --- the member's own side ---------------------------------------------------------------


@transaction.atomic
def answer(*, user, place: GroupMember, yes: bool) -> GroupMember:
    member = _active_member(user)
    place = GroupMember.objects.select_for_update().select_related("group").get(pk=place.pk)
    if place.member_id != member.pk:
        raise PermissionDenied("This is not yours to answer.")
    if place.status != M.INVITED:
        raise ValidationError("This was already answered.")
    now = timezone.now()
    place.status = M.ACTIVE if yes else M.DECLINED
    place.answered_at = now
    place.save(update_fields=["status", "answered_at", "updated_at"])
    group = place.group
    if place.via == VIA.LEADER:
        group = _locked(group)
        if yes:
            group.leader_confirmed_at = now
            group.save(update_fields=["leader_confirmed_at", "updated_at"])
        elif group.status == S.PENDING:
            group.status = S.REJECTED
            group.decided_at = now
            group.decision_reason = "The person named as its leader said no."
            group.save(update_fields=["status", "decided_at", "decision_reason", "updated_at"])
    _record(user, "answered", group, changes={"yes": yes, "as_leader": place.via == VIA.LEADER})
    return place


@transaction.atomic
def leave(*, user, place: GroupMember) -> GroupMember:
    member = _active_member(user)
    place = GroupMember.objects.select_for_update().select_related("group").get(pk=place.pk)
    if place.member_id != member.pk:
        raise PermissionDenied("This is not yours to change.")
    if place.via == VIA.LEADER:
        raise ValidationError("You lead this group. Ask TGCG to hand it to another leader.")
    if place.status != M.ACTIVE:
        raise ValidationError("You are not in this group.")
    place.status = M.LEFT
    place.ended_at = timezone.now()
    place.save(update_fields=["status", "ended_at", "updated_at"])
    _record(user, "left", place.group)
    return place


def group_for_code(code) -> SupportGroup:
    """The approved group whose code this is."""
    code = referrals.clean(code)
    group = (
        SupportGroup.objects.filter(join_code=code, status=S.APPROVED).first()
        if len(code) == CODE_DIGITS
        else None
    )
    if group is None:
        raise ValidationError({"group_code": CODE_NOT_FOUND})
    return group


@transaction.atomic
def join(*, user, code) -> GroupMember:
    """A member joins a group with its code: their own choice, so at once."""
    member = _active_member(user)
    group = group_for_code(code)
    place = GroupMember.objects.select_for_update().filter(group=group, member=member).first()
    if place is not None:
        if place.status == M.ACTIVE:
            raise ValidationError({"group_code": "You are already in this group."})
        if place.status == M.REMOVED:
            raise ValidationError({"group_code": "This group removed you. Ask its leader."})
    else:
        place = GroupMember(group=group, member=member)
    if place.status != M.INVITED:
        place.via = VIA.CODE
    place.status = M.ACTIVE
    place.answered_at = timezone.now()
    place.ended_at = None
    place.save()
    _record(user, "joined", group)
    return place


def join_when_registering(*, member: Member, group: SupportGroup) -> GroupMember:
    """Someone joining TGCG with a group's link is in the group from the start."""
    place = GroupMember.objects.create(
        group=group,
        member=member,
        status=M.ACTIVE,
        via=VIA.LINK,
        answered_at=timezone.now(),
    )
    _record(member.user, "joined", group, changes={"with": "link"})
    return place


def link_for(group: SupportGroup) -> str:
    """The joining page with the group's code, and the leader's referral code,
    so that whoever joins through it counts as the leader's, as on their own link."""
    referral = referrals.code_for(group.leader)
    return f"{referrals.link_for(referral)}&group={group.join_code}"


# --- the leader's messages ------------------------------------------------------------


@transaction.atomic
def send_message(*, actor, group, title, body) -> int:
    """A message in the app to everyone in the group. Returns how many."""
    require_leader(actor, group)
    title = " ".join((title or "").split())
    body = (body or "").strip()
    errors = {}
    if not title:
        errors["title"] = "Give the message a title."
    if not body:
        errors["body"] = "Write the message."
    elif len(body) > 1000:
        errors["body"] = "Shorten the message to 1,000 letters."
    if errors:
        raise ValidationError(errors)
    now = timezone.now()
    sent_today = Broadcast.objects.filter(
        kind=Broadcast.Kind.NOTICE,
        audience__supportGroup=str(group.uuid),
        created_at__gte=now - timedelta(days=1),
    ).count()
    if sent_today >= MESSAGES_PER_DAY:
        raise ValidationError(f"A group can send {MESSAGES_PER_DAY} messages a day. Try tomorrow.")
    members = list(
        Member.objects.filter(
            support_group_memberships__group=group, support_group_memberships__status=M.ACTIVE
        ).values_list("pk", flat=True)
    )
    broadcast = Broadcast.objects.create(
        sender=actor,
        kind=Broadcast.Kind.NOTICE,
        title=f"{group.name}: {title}"[:120],
        body=body,
        channels=[IN_APP],
        audience={"supportGroup": str(group.uuid)},
        people=len(members),
        status=Broadcast.Status.DONE,
        finished_at=now,
    )
    Delivery.objects.bulk_create(
        [
            Delivery(
                broadcast=broadcast,
                member_id=pk,
                channel=IN_APP,
                status=Delivery.Status.SENT,
                sent_at=now,
            )
            for pk in members
        ],
        batch_size=1000,
    )
    _record(actor, "messaged", group, changes={"people": len(members)})
    return len(members)


# --- counting -------------------------------------------------------------------------------


IN_GROUP = Q(memberships__status=M.ACTIVE)


def with_counts(groups):
    return groups.annotate(
        members=Count("memberships", filter=IN_GROUP),
        approved_members=Count(
            "memberships",
            filter=IN_GROUP
            & Q(memberships__member__verification_status=VerificationStatus.VERIFIED),
        ),
        waiting=Count("memberships", filter=Q(memberships__status=M.INVITED)),
    ).order_by("-created_at", "-id")


def _people_in(groups):
    """Members in any of these groups once approved, each counted once."""
    return Member.objects.filter(
        support_group_memberships__status=M.ACTIVE,
        support_group_memberships__group__in=groups.filter(status=S.APPROVED),
    ).distinct()


def totals() -> dict:
    """Groups by status, and the people in approved groups counted once; and
    the same for each type of group."""
    by_status = dict(
        SupportGroup.objects.values_list("status").annotate(count=Count("id")).order_by()
    )
    by_kind = {}
    for kind_id, status, count in (
        SupportGroup.objects.values_list("kind_id", "status").annotate(count=Count("id")).order_by()
    ):
        by_kind.setdefault(kind_id, {})[status] = count
    people = _people_in(SupportGroup.objects.all())
    return {
        "groups": {status: by_status.get(status, 0) for status in S.values},
        "people": people.count(),
        "approved_people": people.filter(verification_status=VerificationStatus.VERIFIED).count(),
        "kinds": [
            {
                "code": kind.code,
                "name": kind.name,
                "offered": kind.offered,
                "groups": {status: by_kind.get(kind.pk, {}).get(status, 0) for status in S.values},
                "people": _people_in(SupportGroup.objects.filter(kind=kind)).count(),
            }
            for kind in GroupKind.objects.all()
        ],
    }


def analysis(group) -> dict:
    """Everything about the people in one group, for those who vet groups
    (4 November 2026): what Members & Exco shows about members (totals,
    gender, age, profession, identity, profiles, sign-in, week by week),
    and the group's own: how each came into it, how many are asked and have
    not said yes, where they are by state, and how they sign in."""
    from apps.membership import analysis as member_analysis

    now = timezone.now()
    places = group.memberships.filter(status=M.ACTIVE)
    members = Member.objects.filter(pk__in=places.values("member_id"))
    states = (
        members.values("state__code", "state__name")
        .annotate(n=Count("pk"))
        .order_by("-n", "state__name")
    )
    return {
        "members": member_analysis.members_summary(members, now),
        "via": dict(places.values_list("via").annotate(n=Count("pk")).order_by()),
        "waiting": group.memberships.filter(status=M.INVITED).count(),
        "states": [
            {"code": row["state__code"] or "", "name": row["state__name"] or "", "count": row["n"]}
            for row in states
        ],
        "sign_in": members.aggregate(
            phone=Count("pk", filter=Q(phone_number__isnull=False) & ~Q(phone_number="")),
            vin_only=Count("pk", filter=Q(phone_number__isnull=True) | Q(phone_number="")),
            pin_to_choose=Count("pk", filter=Q(user__must_change_password=True)),
        ),
    }


# --- types of group ---------------------------------------------------------------


def _kind_name(name) -> str:
    name = " ".join((name or "").split())
    if len(name) < 3:
        raise ValidationError({"name": "Name the type of group."})
    if len(name) > 80:
        raise ValidationError({"name": "Shorten the name to 80 letters."})
    return name


def _record_kind(actor, action, kind, **extra):
    audit.record(
        actor=actor,
        action=f"supportGroupKind.{action}",
        target_type="supportGroupKind",
        target_id=kind.code,
        target_label=kind.name,
        **extra,
    )


@transaction.atomic
def add_kind(*, actor, name) -> GroupKind:
    """A new type of group, offered at once, after the others."""
    require_vetter(actor)
    name = _kind_name(name)
    if GroupKind.objects.filter(name__iexact=name).exists():
        raise ValidationError({"name": "There is already a type of group with this name."})
    base = slugify(name)[:36] or "group"
    code, number = base, 2
    while GroupKind.objects.filter(code=code).exists():
        code, number = f"{base}-{number}", number + 1
    last = GroupKind.objects.order_by("-position").values_list("position", flat=True).first()
    kind = GroupKind.objects.create(code=code, name=name, position=(last or 0) + 1)
    _record_kind(actor, "added", kind)
    return kind


@transaction.atomic
def update_kind(*, actor, kind, name=None, offered=None) -> GroupKind:
    """Rename a type, or stop or start offering it to groups registering.
    Groups already of a type keep it either way."""
    require_vetter(actor)
    kind = GroupKind.objects.select_for_update().get(pk=kind.pk)
    changes = {}
    if name is not None:
        name = _kind_name(name)
        if GroupKind.objects.filter(name__iexact=name).exclude(pk=kind.pk).exists():
            raise ValidationError({"name": "There is already a type of group with this name."})
        if name != kind.name:
            changes["name"] = {"from": kind.name, "to": name}
            kind.name = name
    if offered is not None and offered != kind.offered:
        if not offered and not GroupKind.objects.filter(offered=True).exclude(pk=kind.pk).exists():
            raise ValidationError({"offered": "Keep at least one type of group offered."})
        changes["offered"] = offered
        kind.offered = offered
    if changes:
        kind.save(update_fields=["name", "offered", "updated_at"])
        _record_kind(actor, "changed", kind, changes=changes)
    return kind


@transaction.atomic
def change_kind(*, actor, group, kind) -> SupportGroup:
    """Move a group to another type, for one registered under the wrong one."""
    require_vetter(actor)
    group = _locked(group)
    if group.kind_id != kind.pk:
        before = group.kind.name
        group.kind = kind
        group.save(update_fields=["kind", "updated_at"])
        _record(actor, "kindChanged", group, changes={"from": before, "to": kind.name})
    return group

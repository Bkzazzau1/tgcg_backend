"""Sending bulk messages, saved lists, and phones for app notifications.

All changes go through here. A send goes out at once (no approval, decided on
17 October 2026): it is queued per person and channel, and
`manage.py deliver_messages` hands the queue to the services in batches.
"""

from datetime import timedelta

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from apps.access import audit, engine
from apps.geography.scope import any_of, within_scope_q
from apps.membership.models import Member

from . import audience, providers
from .models import Broadcast, Channel, Delivery, Device, MessageList

D = Delivery.Status
# Numbers per bulk SMS request: one refused request costs at most this many.
SMS_CHUNK = 500
# A delivery claimed this long ago by a worker that stopped is tried again.
STALE = timedelta(minutes=15)


def can_send(user, channel) -> bool:
    return engine.allows(user, audience.POWER[channel])


def sending_channels(user) -> list[str]:
    """The channels this account may send on."""
    return [channel for channel in Channel.values if can_send(user, channel)]


def open_channels(user) -> list[str]:
    """The channels this account may send on that the server can carry now."""
    return [channel for channel in sending_channels(user) if providers.ready(channel)]


def _require_channels(user, channels):
    if not channels:
        raise ValidationError({"channels": "Choose SMS, WhatsApp or app notification."})
    for channel in channels:
        if not can_send(user, channel):
            raise PermissionDenied(f"You may not send {Channel(channel).label} messages.")
        # Refused rather than queued to fail for everyone.
        if not providers.ready(channel):
            raise ValidationError(
                {"channels": f"{Channel(channel).label} is not set up on the server yet."}
            )


def _reach_q(user):
    """Members in the areas where the user may send on any channel."""
    powers = [audience.POWER[channel] for channel in sending_channels(user)]
    if not powers:
        raise PermissionDenied("Your role does not include sending messages.")
    return any_of(*(within_scope_q(user, permission=power) for power in powers))


def preview(*, sender, channels, groups, body="") -> dict:
    """How many people each channel reaches, before sending."""
    _require_channels(sender, channels)
    by_channel, reachable = {}, []
    for channel in channels:
        reached = audience.reached(sender, groups, channel)
        addressable = audience.addressable(reached, channel)
        count = addressable.count()
        by_channel[channel] = {"reachable": count, "unreachable": reached.count() - count}
        reachable.append(Q(pk__in=addressable.values("pk")))
    return {
        "people": Member.objects.filter(any_of(*reachable)).count() if reachable else 0,
        "channels": by_channel,
        "sms_pages": audience.sms_pages(body) if body else 0,
    }


@transaction.atomic
def send(*, sender, channels, body: str, groups, title: str = "", record=None) -> Broadcast:
    """Queue one message to everyone its groups reach on each channel."""
    _require_channels(sender, channels)
    body = (body or "").strip()
    if not body:
        raise ValidationError({"body": "Write the message."})
    if not groups:
        raise ValidationError({"groups": "Choose who receives it."})
    broadcast = Broadcast.objects.create(
        sender=sender,
        title=" ".join((title or "").split()),
        body=body,
        channels=list(channels),
        audience=record or {},
    )
    people, unreachable = set(), {}
    for channel in channels:
        reached = audience.reached(sender, groups, channel)
        members = list(audience.addressable(reached, channel).values_list("pk", flat=True))
        unreachable[channel] = reached.count() - len(members)
        Delivery.objects.bulk_create(
            [Delivery(broadcast=broadcast, member_id=pk, channel=channel) for pk in members],
            batch_size=2000,
        )
        people.update(members)
    if not people:
        raise ValidationError("Nobody in your area can receive this message.")
    broadcast.people = len(people)
    broadcast.unreachable = unreachable
    broadcast.save(update_fields=["people", "unreachable", "updated_at"])
    audit.record(
        actor=sender,
        action="message.sent",
        target_type="broadcast",
        target_id=broadcast.uuid,
        target_label=(broadcast.title or broadcast.body)[:80],
        changes={"channels": broadcast.channels, "people": broadcast.people},
    )
    return broadcast


def notify(*, sender, member, title: str, body: str, record=None) -> list[str]:
    """Queue a notice to one member on the channels the server can carry now:
    the app on their phone, if they have it, and SMS. It goes out with the
    other messages, and is in their inbox. Returns the channels used."""
    reached = Member.objects.filter(pk=member.pk)
    channels = [
        channel
        for channel in (Channel.PUSH, Channel.SMS)
        if providers.ready(channel) and audience.addressable(reached, channel).exists()
    ]
    if not channels:
        return []
    broadcast = Broadcast.objects.create(
        sender=sender,
        kind=Broadcast.Kind.NOTICE,
        title=" ".join(title.split()),
        body=body.strip(),
        channels=channels,
        audience=record or {},
        people=1,
    )
    Delivery.objects.bulk_create(
        [Delivery(broadcast=broadcast, member=member, channel=channel) for channel in channels]
    )
    return channels


# --- delivery ----------------------------------------------------------------------


def _claim(limit: int) -> list[Delivery]:
    now = timezone.now()
    with transaction.atomic():
        Delivery.objects.filter(status=D.SENDING, attempted_at__lt=now - STALE).update(
            status=D.QUEUED
        )
        claimed = list(
            Delivery.objects.select_for_update(skip_locked=True)
            .filter(status=D.QUEUED)
            .order_by("id")
            .values_list("pk", flat=True)[:limit]
        )
        Delivery.objects.filter(pk__in=claimed).update(status=D.SENDING, attempted_at=now)
    return list(
        Delivery.objects.filter(pk__in=claimed).select_related("member", "broadcast").order_by("id")
    )


def _mark(rows, status, detail="", ref=""):
    now = timezone.now()
    for row in rows:
        row.status = status
        row.detail = detail[:200]
        row.provider_ref = ref[:100]
        row.sent_at = now if status == D.SENT else None
    Delivery.objects.bulk_update(rows, ["status", "detail", "provider_ref", "sent_at"])


def _send_sms(rows):
    text = rows[0].broadcast.body
    for start in range(0, len(rows), SMS_CHUNK):
        chunk = rows[start : start + SMS_CHUNK]
        try:
            ref = providers.send_sms_bulk([row.member.phone_number for row in chunk], text)
        except providers.ProviderError as error:
            _mark(chunk, D.FAILED, detail=str(error))
        else:
            _mark(chunk, D.SENT, ref=ref)


def _send_whatsapp(rows):
    for row in rows:
        member = row.member
        try:
            ref = providers.send_whatsapp(
                member.whatsapp_number or member.phone_number, row.broadcast.body
            )
        except providers.ProviderError as error:
            _mark([row], D.FAILED, detail=str(error))
        else:
            _mark([row], D.SENT, ref=ref)


def _send_push(rows):
    for row in rows:
        broadcast = row.broadcast
        devices = (
            list(Device.objects.filter(user_id=row.member.user_id)) if row.member.user_id else []
        )
        sent, problem = None, "No phone receives notifications for this account."
        for device in devices:
            try:
                sent = providers.send_push(
                    device.token, broadcast.title, broadcast.body, {"broadcast": broadcast.uuid}
                )
            except providers.InvalidToken:
                device.delete()  # the app was removed or signed out
            except providers.ProviderError as error:
                problem = str(error)
        if sent is None:
            _mark([row], D.FAILED, detail=problem)
        else:
            _mark([row], D.SENT, ref=sent)


SENDERS = {Channel.SMS: _send_sms, Channel.WHATSAPP: _send_whatsapp, Channel.PUSH: _send_push}


def deliver(limit: int = 500) -> int:
    """Send up to `limit` waiting messages; returns how many were handled."""
    rows = _claim(limit)
    batches: dict[tuple[int, str], list[Delivery]] = {}
    for row in rows:
        batches.setdefault((row.broadcast_id, row.channel), []).append(row)
    for (_, channel), batch in batches.items():
        SENDERS[channel](batch)
    finished = {broadcast_id for broadcast_id, _ in batches}
    waiting = set(
        Delivery.objects.filter(broadcast_id__in=finished, status__in=[D.QUEUED, D.SENDING])
        .values_list("broadcast_id", flat=True)
        .distinct()
    )
    Broadcast.objects.filter(pk__in=finished - waiting).update(
        status=Broadcast.Status.DONE, finished_at=timezone.now()
    )
    return len(rows)


# --- saved lists -------------------------------------------------------------------


def _members(owner, numbers) -> list[Member]:
    """Members by number, each in the owner's area for sending."""
    numbers = {number.strip().upper() for number in numbers if number.strip()}
    found = list(Member.objects.filter(membership_number__in=numbers).filter(_reach_q(owner)))
    missing = numbers - {member.membership_number for member in found}
    if missing:
        raise ValidationError({"members": f"Not found in your area: {', '.join(sorted(missing))}."})
    return found


def _audit_list(actor, action, message_list, changes=None):
    audit.record(
        actor=actor,
        action=action,
        target_type="messageList",
        target_id=message_list.uuid,
        target_label=message_list.name,
        changes=changes,
    )


@transaction.atomic
def create_list(*, owner, name: str, numbers=(), groups=None) -> MessageList:
    """A named list, from membership numbers and/or the people some groups
    reach now (a snapshot: it does not change when people join later)."""
    reach = _reach_q(owner)
    name = " ".join((name or "").split())
    if not name:
        raise ValidationError({"name": "Name the list."})
    members = _members(owner, numbers)
    try:
        with transaction.atomic():
            message_list = MessageList.objects.create(owner=owner, name=name)
    except IntegrityError as error:
        raise ValidationError({"name": "You already have a list with this name."}) from error
    message_list.members.add(*members)
    if groups:
        everyone = Member.objects.filter(audience.audience_q(groups)).filter(reach)
        message_list.members.add(*everyone.filter(membership_state="active"))
    _audit_list(
        owner, "messageList.created", message_list, {"people": message_list.members.count()}
    )
    return message_list


def require_owner(user, message_list):
    if message_list.owner_id != user.pk and not engine.is_national_administrator(user):
        raise PermissionDenied("This list belongs to someone else.")


@transaction.atomic
def update_list(*, user, message_list, name=None, add=(), remove=()) -> MessageList:
    require_owner(user, message_list)
    if name is not None:
        name = " ".join(name.split())
        if not name:
            raise ValidationError({"name": "Name the list."})
        message_list.name = name
        try:
            with transaction.atomic():
                message_list.save(update_fields=["name", "updated_at"])
        except IntegrityError as error:
            raise ValidationError({"name": "You already have a list with this name."}) from error
    if add:
        message_list.members.add(*_members(user, add))
    if remove:
        message_list.members.remove(
            *Member.objects.filter(membership_number__in=[n.strip().upper() for n in remove])
        )
    _audit_list(user, "messageList.updated", message_list, {"people": message_list.members.count()})
    return message_list


def delete_list(*, user, message_list) -> None:
    require_owner(user, message_list)
    _audit_list(user, "messageList.deleted", message_list)
    message_list.delete()


# --- phones for app notifications --------------------------------------------------


def register_device(*, user, token: str, platform: str = "") -> Device:
    """The phone signed in to this account receives its notifications. A
    token that was another account's moves here: one phone, one account."""
    token = token.strip()
    if not token:
        raise ValidationError({"token": "Missing."})
    device, _ = Device.objects.update_or_create(
        token=token, defaults={"user": user, "platform": (platform or "")[:16]}
    )
    return device


def unregister_device(*, user, token: str) -> None:
    Device.objects.filter(user=user, token=token.strip()).delete()

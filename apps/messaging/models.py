"""Bulk messages to members: SMS, WhatsApp and app notifications.

A sender holding the power for a channel (sendSms, sendWhatsApp,
sendPushNotifications) writes one message and picks its people: groups by
area, role, gender or identity check, project teams, and saved lists. The
server joins the groups, drops duplicates, keeps only members in the
sender's own area for each channel, and queues one delivery per person and
channel. `manage.py deliver_messages` sends them in batches.
"""

from django.conf import settings
from django.db import models

from apps.core.models import TimeStampedModel


class Channel(models.TextChoices):
    SMS = "sms", "SMS"
    WHATSAPP = "whatsapp", "WhatsApp"
    PUSH = "push", "App notification"


class Broadcast(TimeStampedModel):
    """One message sent to many people, or a notice the system sends one person."""

    class Status(models.TextChoices):
        SENDING = "sending", "Sending"
        DONE = "done", "Sent"

    class Kind(models.TextChoices):
        MESSAGE = "message", "Message"
        # Sent by the system on someone's action, such as an incident sent to
        # a security officer; it is not among the sender's messages.
        NOTICE = "notice", "Notice"

    sender = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    kind = models.CharField(max_length=10, choices=Kind.choices, default=Kind.MESSAGE)
    title = models.CharField(max_length=120, blank=True, help_text="For app notifications.")
    body = models.TextField(max_length=1000)
    channels = models.JSONField(default=list)
    audience = models.JSONField(default=dict, help_text="The groups chosen, for the record.")
    people = models.PositiveIntegerField(default=0, help_text="Members reached on any channel.")
    unreachable = models.JSONField(
        default=dict, help_text="Per channel, members in the audience with no number or app."
    )
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.SENDING)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at", "-id"]

    def __str__(self):
        return f"{self.sender_id}: {self.body[:40]}"


class Delivery(models.Model):
    """One message to one person on one channel."""

    class Status(models.TextChoices):
        QUEUED = "queued", "Waiting"
        SENDING = "sending", "Sending"
        SENT = "sent", "Sent"
        FAILED = "failed", "Not sent"

    broadcast = models.ForeignKey(Broadcast, on_delete=models.CASCADE, related_name="deliveries")
    member = models.ForeignKey(
        "membership.Member", on_delete=models.CASCADE, related_name="message_deliveries"
    )
    channel = models.CharField(max_length=10, choices=Channel.choices)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.QUEUED)
    detail = models.CharField(max_length=200, blank=True, help_text="Why it was not sent.")
    provider_ref = models.CharField(max_length=100, blank=True)
    attempted_at = models.DateTimeField(null=True, blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["id"]
        indexes = [models.Index(fields=["status", "channel"])]
        constraints = [
            models.UniqueConstraint(
                fields=["broadcast", "member", "channel"], name="one_delivery_per_channel"
            )
        ]

    def __str__(self):
        return f"{self.broadcast_id} → {self.member_id} ({self.channel}: {self.status})"


class MessageList(TimeStampedModel):
    """A named group of members a sender builds once and reuses."""

    name = models.CharField(max_length=120)
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="message_lists"
    )
    members = models.ManyToManyField("membership.Member", related_name="+", blank=True)

    class Meta:
        ordering = ["name", "id"]
        constraints = [
            models.UniqueConstraint(fields=["owner", "name"], name="one_list_name_per_owner")
        ]

    def __str__(self):
        return self.name


class Device(TimeStampedModel):
    """A phone that receives app notifications for its signed-in account."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="push_devices"
    )
    token = models.CharField(max_length=255, unique=True)
    platform = models.CharField(max_length=16, blank=True)

    def __str__(self):
        return f"{self.user_id} ({self.platform})"

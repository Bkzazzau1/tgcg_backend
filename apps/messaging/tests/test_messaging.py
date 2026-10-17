"""Bulk messages: who they reach, how they are sent, saved lists and the inbox."""

import json

import pytest
from django.core.management import call_command

from apps.access.models import AuditEvent
from apps.access.tests.test_access import KD, appoint, client_for, custom_role, member_account
from apps.accounts.models import User
from apps.core.choices import Role
from apps.core.permissions import Capability
from apps.messaging import providers
from apps.messaging.models import Broadcast, Delivery, Device
from conftest import PASSWORD

pytestmark = pytest.mark.django_db
C = Capability
EVERYONE = [{"type": "filter"}]


@pytest.fixture(autouse=True)
def outbox():
    providers.outbox.clear()
    yield providers.outbox
    providers.outbox.clear()


@pytest.fixture
def admin():
    return User.objects.create_user(
        "NA-1", PASSWORD, full_name="National Admin", role=Role.NATIONAL_ADMINISTRATOR
    )


def sender_with(admin, permissions, phone="08037770001", name="Kaduna Messenger"):
    """A member appointed over Kaduna State to a role holding `permissions`."""
    member = member_account(phone, name=name)
    role = custom_role(admin, f"{name} role", list(permissions))
    assert appoint(admin, member, role, [KD]).status_code == 201
    return client_for(member.user)


def message(channels=("sms",), groups=None, body="Meeting at the ward office, 4 pm.", **extra):
    return {"channels": list(channels), "body": body, "groups": groups or EVERYONE, **extra}


def numbers_sent(outbox, channel="sms"):
    sent = []
    for kind, to, _ in outbox:
        if kind == channel:
            sent.extend(to if isinstance(to, list) else [to])
    return sorted(sent)


def test_a_sender_reaches_only_their_own_area(admin, outbox):
    sender = sender_with(admin, [C.SEND_SMS])
    member_account("08037770002", name="Ada Obi")  # Kaduna North
    member_account("08037770003", name="Sani Umar", lga="KN-AJINGI")  # Kano

    preview = sender.post("/api/v1/broadcasts/preview/", message(), format="json")
    assert preview.status_code == 200, preview.content
    assert preview.json()["channels"]["sms"] == {"reachable": 2, "unreachable": 0}
    sent = sender.post("/api/v1/broadcasts/", message(), format="json")
    assert sent.status_code == 201, sent.content
    assert sent.json()["people"] == 2

    call_command("deliver_messages", seconds=5)
    assert numbers_sent(outbox) == ["+2348037770001", "+2348037770002"]  # not Kano's
    broadcast = Broadcast.objects.get()
    assert broadcast.status == "done"
    detail = sender.get(f"/api/v1/broadcasts/{sent.json()['id']}/").json()
    assert detail["delivered"] == {"sms": {"sent": 2}}
    event = AuditEvent.objects.get(action="message.sent")
    assert event.changes == {"channels": ["sms"], "people": 2}

    # Each channel is its own power, given by an administrator.
    refused = sender.post("/api/v1/broadcasts/", message(channels=["whatsapp"]), format="json")
    assert refused.status_code == 403
    plain = client_for(member_account("08037770004", name="Bala Musa").user)
    assert plain.post("/api/v1/broadcasts/", message(), format="json").status_code == 403
    # An administrator reaches everyone.
    national = client_for(admin)
    assert (
        national.post("/api/v1/broadcasts/preview/", message(), format="json").json()["people"] == 4
    )


def test_groups_are_joined_without_repeating_anyone(admin, outbox):
    sender = sender_with(admin, [C.SEND_SMS])
    women = member_account("08037770011", name="Hauwa Sani")
    women.gender = "female"
    women.save()
    unchecked = member_account("08037770012", name="Musa Bello", verified=False)
    agent_role = custom_role(admin, "Ward Mobiliser", [C.SEND_BROADCAST])
    mobiliser = member_account("08037770013", name="Tanko Garba")
    assert appoint(admin, mobiliser, agent_role, [KD]).status_code == 201

    def reach(groups):
        body = message(groups=groups)
        return sender.post("/api/v1/broadcasts/preview/", body, format="json").json()["people"]

    assert reach([{"type": "filter", "gender": "female"}]) == 1
    assert reach([{"type": "filter", "people": "roles", "roles": [agent_role.code]}]) == 1
    # People with a role: the mobiliser and the sender themselves.
    assert reach([{"type": "filter", "people": "withRole"}]) == 2
    assert reach([{"type": "filter", "people": "noRole"}]) == 2
    assert reach([{"type": "filter", "verified_only": True}]) == 3
    assert unchecked.verification_status != "verified"
    in_lagos = [{"type": "filter", "areas": [{"level": "state", "stateId": "LA"}]}]
    assert reach(in_lagos) == 0
    # A saved list and a filter that overlap count each person once.
    made = sender.post(
        "/api/v1/message-lists/",
        {"name": "Women leaders", "members": [women.membership_number]},
        format="json",
    )
    assert made.status_code == 201, made.content
    both = [{"type": "list", "list": made.json()["id"]}, {"type": "filter", "gender": "female"}]
    assert reach(both) == 1
    sender.post("/api/v1/broadcasts/", message(groups=both), format="json")
    assert Delivery.objects.count() == 1


def test_app_notifications_go_to_registered_phones_and_the_inbox(admin, outbox):
    sender = sender_with(admin, [C.SEND_PUSH_NOTIFICATIONS])
    reader = member_account("08037770021", name="Ada Obi")
    member_account("08037770022", name="No App")
    phone = client_for(reader.user)
    registered = phone.post(
        "/api/v1/me/devices/", {"token": "fcm-token-1", "platform": "android"}, format="json"
    )
    assert registered.status_code == 204

    body = message(channels=["push"], title="Election day", body="Polls open at 8 am.")
    preview = sender.post("/api/v1/broadcasts/preview/", body, format="json").json()
    assert preview["channels"]["push"] == {"reachable": 1, "unreachable": 2}
    assert sender.post("/api/v1/broadcasts/", body, format="json").status_code == 201
    call_command("deliver_messages", seconds=5)
    assert outbox == [("push", "fcm-token-1", "Polls open at 8 am.")]
    inbox = phone.get("/api/v1/me/messages/").json()
    assert [(m["title"], m["body"]) for m in inbox] == [("Election day", "Polls open at 8 am.")]

    # A phone that no longer has the app is forgotten.
    def gone(token, title, text, data):
        raise providers.InvalidToken("gone")

    from apps.messaging import services

    original = providers.send_push
    providers.send_push = gone
    try:
        sender.post("/api/v1/broadcasts/", body, format="json")
        services.deliver()
    finally:
        providers.send_push = original
    assert not Device.objects.exists()
    assert Delivery.objects.filter(status="failed").count() == 1

    signed_out = phone.delete("/api/v1/me/devices/", {"token": "fcm-token-1"}, format="json")
    assert signed_out.status_code == 204


def test_whatsapp_goes_to_the_members_own_whatsapp_number(admin, outbox):
    sender = sender_with(admin, [C.SEND_WHATSAPP])
    member = member_account("08037770031", name="Ada Obi")
    own = client_for(member.user)
    changed = own.patch("/api/v1/me/member/", {"whatsapp_number": "0905 123 4567"}, format="json")
    assert changed.status_code == 200, changed.content
    assert changed.json()["whatsapp_number"] == "+2349051234567"
    bad = own.patch("/api/v1/me/member/", {"whatsapp_number": "12345"}, format="json")
    assert bad.status_code == 400

    sender.post("/api/v1/broadcasts/", message(channels=["whatsapp"]), format="json")
    call_command("deliver_messages", seconds=5)
    assert numbers_sent(outbox, "whatsapp") == ["+2348037770001", "+2349051234567"]


def test_saved_lists_belong_to_their_sender(admin):
    sender = sender_with(admin, [C.SEND_SMS])
    kaduna = member_account("08037770041", name="Ada Obi")
    kano = member_account("08037770042", name="Sani Umar", lga="KN-AJINGI")
    outside = sender.post(
        "/api/v1/message-lists/",
        {"name": "Leaders", "members": [kaduna.membership_number, kano.membership_number]},
        format="json",
    )
    assert outside.status_code == 400  # Kano is outside the sender's area
    made = sender.post(
        "/api/v1/message-lists/",
        {"name": "Leaders", "groups": [{"type": "filter", "people": "noRole"}]},
        format="json",
    ).json()
    assert made["people"] == 1
    url = f"/api/v1/message-lists/{made['id']}/"
    renamed = sender.patch(url, {"name": "Ward leaders", "remove": [kaduna.membership_number]})
    assert renamed.json()["name"] == "Ward leaders" and renamed.json()["people"] == 0

    other = sender_with(admin, [C.SEND_SMS], phone="08037770043", name="Other Sender")
    assert other.get(url).status_code == 404
    using = message(groups=[{"type": "list", "list": made["id"]}])
    assert other.post("/api/v1/broadcasts/preview/", using, format="json").status_code == 403
    assert sender.delete(url).status_code == 204


def test_a_refused_send_is_recorded_per_person(admin, outbox):
    sender = sender_with(admin, [C.SEND_SMS])
    member_account("08037770051", name="Ada Obi")

    def refused(numbers, text):
        raise providers.ProviderError("Termii: Insufficient balance")

    original = providers.send_sms_bulk
    providers.send_sms_bulk = refused
    try:
        sender.post("/api/v1/broadcasts/", message(), format="json")
        call_command("deliver_messages", seconds=5)
    finally:
        providers.send_sms_bulk = original
    failed = Delivery.objects.filter(status="failed")
    assert failed.count() == 2
    assert failed.first().detail == "Termii: Insufficient balance"


def test_the_services_receive_what_they_expect(settings, monkeypatch):
    settings.SMS_BACKEND = "termii"
    settings.WHATSAPP_BACKEND = "termii"
    settings.TERMII_API_KEY = "test-key"
    settings.TERMII_SENDER_ID = "TGCG"
    calls = []

    def post(url, body, headers):
        calls.append((url, json.loads(body) if body.startswith(b"{") else body))
        return 200, {"message_id": "m-1"}

    monkeypatch.setattr(providers, "_post", post)
    assert providers.send_sms_bulk(["+2348037770001"], "Hello") == "m-1"
    url, payload = calls[-1]
    assert url.endswith("/api/sms/send/bulk")
    assert payload["to"] == ["2348037770001"] and payload["channel"] == "generic"
    providers.send_whatsapp("+2349051234567", "Hello")
    assert calls[-1][1]["channel"] == "whatsapp" and calls[-1][1]["to"] == "2349051234567"

    settings.PUSH_BACKEND = "fcm"
    monkeypatch.setattr(
        providers, "_service_account", lambda: {"project_id": "tgcg-test", "client_email": "x"}
    )
    monkeypatch.setattr(providers, "_access_token", lambda account: "token")

    def unregistered(url, body, headers):
        assert url.endswith("/projects/tgcg-test/messages:send")
        assert headers["Authorization"] == "Bearer token"
        error = {"error": {"message": "gone", "details": [{"errorCode": "UNREGISTERED"}]}}
        return 404, error

    monkeypatch.setattr(providers, "_post", unregistered)
    with pytest.raises(providers.InvalidToken):
        providers.send_push("old-token", "TGCG", "Hello", {"broadcast": "b-1"})


def test_a_channel_whose_service_is_not_set_up_is_not_offered(admin, settings):
    sender = sender_with(admin, [C.SEND_SMS, C.SEND_PUSH_NOTIFICATIONS])
    member_account("08037770071", name="Ada Obi")
    # As on a server with no Termii key or Firebase key file yet.
    settings.SMS_BACKEND = "termii"
    settings.TERMII_API_KEY = ""
    settings.PUSH_BACKEND = "fcm"
    settings.FIREBASE_SERVICE_ACCOUNT_FILE = ""
    assert sender.get("/api/v1/broadcasts/roles/").json()["channels"] == []
    refused = sender.post("/api/v1/broadcasts/", message(), format="json")
    assert refused.status_code == 400
    assert "SMS is not set up on the server yet." in str(refused.json())
    assert not Broadcast.objects.exists()

    settings.TERMII_API_KEY = "test-key"
    assert sender.get("/api/v1/broadcasts/roles/").json()["channels"] == ["sms"]


def test_senders_see_the_roles_they_can_name_and_their_channels(admin):
    sender = sender_with(admin, [C.SEND_SMS, C.SEND_PUSH_NOTIFICATIONS])
    body = sender.get("/api/v1/broadcasts/roles/").json()
    assert body["channels"] == ["sms", "push"]
    assert {"code": "pollingUnitAgent", "name": "Polling Unit Agent"} in body["roles"]
    plain = client_for(member_account("08037770061", name="Ada Obi").user)
    assert plain.get("/api/v1/broadcasts/roles/").status_code == 403

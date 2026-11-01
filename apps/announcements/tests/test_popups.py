"""Pop-up messages to everyone, and notices kept in the app's inbox."""

from datetime import timedelta
from io import BytesIO

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone
from PIL import Image

from apps.access.models import AuditEvent
from apps.access.tests.test_access import appoint, client_for, custom_role, member_account
from apps.accounts.models import User
from apps.announcements.models import PopupMessage
from apps.core.choices import Role
from apps.core.permissions import Capability
from apps.geography.models import State
from apps.membership import identity
from apps.membership.tests.test_members_exco import complete
from apps.messaging import services as messaging
from apps.messaging.models import IN_APP, Broadcast, Channel, Delivery
from conftest import PASSWORD

pytestmark = pytest.mark.django_db
C = Capability
POPUPS = "/api/v1/popups/"
SHOWING = "/api/v1/me/popups/"
INBOX = "/api/v1/me/messages/"
NIGERIA = {"level": "country"}
KD = {"level": "state", "stateId": "KD"}


def picture(kind="PNG"):
    saved = BytesIO()
    Image.new("RGB", (40, 30), (0, 120, 60)).save(saved, kind)
    return SimpleUploadedFile(f"rally.{kind.lower()}", saved.getvalue())


def details(**extra):
    return {"title": "Voters' week", "body": "Check your name.", "frequency": "daily", **extra}


@pytest.fixture(autouse=True)
def media(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    settings.SUMMARY_CACHE_SECONDS = 0


@pytest.fixture
def no_services(settings):
    """As production stands: neither SMS nor phone notifications are set up."""
    settings.SMS_BACKEND = "termii"
    settings.TERMII_API_KEY = ""
    settings.PUSH_BACKEND = "fcm"
    settings.FIREBASE_SERVICE_ACCOUNT_FILE = ""


@pytest.fixture
def admin():
    return User.objects.create_user(
        "NA-1", PASSWORD, full_name="National Admin", role=Role.NATIONAL_ADMINISTRATOR
    )


@pytest.fixture
def secretary(admin):
    """The National Secretary, once the administrator gave the role the power."""
    role = custom_role(admin, "National Secretary", [C.POST_POPUPS], levels=("country",))
    member = member_account("08033331001", name="Aisha Bello")
    assert appoint(admin, member, role, [NIGERIA]).status_code == 201
    return member.user


def post(user, **extra):
    return client_for(user).post(POPUPS, details(**extra), format="multipart")


# --- who may post ----------------------------------------------------------------------


def test_the_administrator_and_the_national_secretary_post_to_everyone(admin, secretary):
    assert post(admin).status_code == 201
    posted = post(secretary, title="Rally on Saturday")
    assert posted.status_code == 201, posted.content
    assert posted.json()["author"] == "Aisha Bello"
    assert posted.json()["state"] == "showing"

    plain = member_account("08033331002", name="Plain Member", verified=False)
    showing = client_for(plain.user).get(SHOWING).json()
    # Newest first, with only what the person needs.
    assert [item["title"] for item in showing] == ["Rally on Saturday", "Voters' week"]
    assert set(showing[0]) == {
        "id",
        "title",
        "body",
        "has_picture",
        "link_url",
        "link_label",
        "frequency",
    }

    event = AuditEvent.objects.filter(action="popup.posted").latest("id")
    assert event.actor == secretary and event.changes["frequency"] == "daily"


def test_everyone_else_is_refused(admin):
    plain = member_account("08033331003", name="Plain Member")
    assert post(plain.user).status_code == 403
    assert client_for(plain.user).get(POPUPS).status_code == 403
    assert client_for(None).post(POPUPS, details()).status_code == 401
    assert client_for(None).get(SHOWING).status_code == 401
    assert not PopupMessage.objects.exists()


def test_the_power_must_be_held_for_all_of_nigeria(admin):
    """It reaches everyone, so a state officer given the power cannot use it."""
    role = custom_role(admin, "State Publicity", [C.POST_POPUPS])
    member = member_account("08033331004", name="Sani Garba")
    assert appoint(admin, member, role, [KD]).status_code == 201
    assert post(member.user).status_code == 403
    assert not PopupMessage.objects.exists()


# --- what is posted --------------------------------------------------------------------


def test_a_picture_and_a_button_come_with_it(admin):
    posted = post(
        admin,
        picture=picture(),
        link_url="https://tgcg.online/rally",
        link_label="Read more",
        frequency="once",
    )
    assert posted.status_code == 201, posted.content
    body = posted.json()
    assert body["has_picture"] is True and body["frequency"] == "once"
    assert body["link_url"] == "https://tgcg.online/rally" and body["link_label"] == "Read more"

    popup = PopupMessage.objects.get()
    assert "rally" not in popup.picture.name  # the uploaded name is not kept
    plain = member_account("08033331005", name="Plain Member", verified=False)
    fetched = client_for(plain.user).get(f"{POPUPS}{popup.uuid}/picture/")
    assert fetched.status_code == 200 and fetched["Content-Type"] == "image/png"
    assert fetched["Cache-Control"] == "private, no-store"
    assert client_for(None).get(f"{POPUPS}{popup.uuid}/picture/").status_code == 401


@pytest.mark.parametrize(
    ("extra", "field"),
    [
        ({"frequency": "hourly"}, "frequency"),
        ({"title": ""}, "title"),
        ({"body": ""}, "body"),
        ({"link_url": "https://tgcg.online"}, "link_label"),
        ({"link_label": "Read more"}, "link_url"),
        ({"link_url": "http://tgcg.online", "link_label": "Read"}, "link_url"),
        ({"picture": SimpleUploadedFile("a.png", b"not a picture")}, "picture"),
    ],
)
def test_a_wrong_message_is_refused(admin, extra, field):
    answer = post(admin, **extra)
    assert answer.status_code == 400, answer.content
    assert field in answer.json()
    assert not PopupMessage.objects.exists()


def test_its_end_must_come_after_its_start(admin):
    now = timezone.now()
    answer = post(
        admin,
        starts_at=(now + timedelta(days=2)).isoformat(),
        ends_at=(now + timedelta(days=1)).isoformat(),
    )
    assert answer.status_code == 400
    assert post(admin, ends_at=(now - timedelta(hours=1)).isoformat()).status_code == 400


def test_it_shows_only_between_its_dates(admin):
    now = timezone.now()
    client = client_for(member_account("08033331006", name="Plain Member", verified=False).user)
    assert client.get(SHOWING).json() == []  # nothing posted: the app keeps its daily reminder

    assert post(admin, starts_at=(now + timedelta(days=1)).isoformat()).json()["state"] == (
        "scheduled"
    )
    assert client.get(SHOWING).json() == []

    PopupMessage.objects.update(starts_at=now - timedelta(days=2))
    assert len(client.get(SHOWING).json()) == 1

    PopupMessage.objects.update(ends_at=now - timedelta(days=1))
    assert client.get(SHOWING).json() == []
    assert client_for(admin).get(POPUPS).json()["results"][0]["state"] == "ended"


def test_a_picture_is_not_seen_before_its_message(admin):
    post(admin, picture=picture(), starts_at=(timezone.now() + timedelta(days=1)).isoformat())
    popup = PopupMessage.objects.get()
    plain = member_account("08033331007", name="Plain Member", verified=False)
    assert client_for(plain.user).get(f"{POPUPS}{popup.uuid}/picture/").status_code == 404
    assert client_for(admin).get(f"{POPUPS}{popup.uuid}/picture/").status_code == 200


def test_stopping_takes_it_away_and_keeps_the_record(admin, secretary):
    posted = post(admin, picture=picture()).json()
    plain = member_account("08033331008", name="Plain Member", verified=False)
    stopped = client_for(secretary).post(f"{POPUPS}{posted['id']}/stop/")
    assert stopped.status_code == 200 and stopped.json()["state"] == "stopped"

    assert client_for(plain.user).get(SHOWING).json() == []
    assert client_for(plain.user).get(f"{POPUPS}{posted['id']}/picture/").status_code == 404
    popup = PopupMessage.objects.get()
    assert popup.stopped_by == secretary
    assert AuditEvent.objects.get(action="popup.stopped").actor == secretary

    # Stopping again changes nothing.
    assert client_for(admin).post(f"{POPUPS}{posted['id']}/stop/").status_code == 200
    assert PopupMessage.objects.get().stopped_by == secretary
    assert AuditEvent.objects.filter(action="popup.stopped").count() == 1

    assert client_for(plain.user).post(f"{POPUPS}{posted['id']}/stop/").status_code == 403


def test_a_posted_message_cannot_be_changed_or_removed(admin):
    posted = post(admin).json()
    client = client_for(admin)
    assert client.patch(f"{POPUPS}{posted['id']}/", {"title": "Other"}).status_code in (404, 405)
    assert client.delete(f"{POPUPS}{posted['id']}/").status_code in (404, 405)


# --- the inbox -------------------------------------------------------------------------


def test_those_approved_or_in_a_position_keep_it_in_their_inbox(admin):
    posted = post(admin, picture=picture(), title="Rally on Saturday").json()
    approved = member_account("08033331009", name="Approved Member")
    waiting = member_account("08033331010", name="Waiting Member", verified=False)

    [message] = [
        row for row in client_for(approved.user).get(INBOX).json() if row["id"] == posted["id"]
    ]
    assert message["title"] == "Rally on Saturday" and message["body"] == "Check your name."
    assert message["popup"] == posted["id"]
    assert client_for(waiting.user).get(INBOX).json() == []

    client_for(admin).post(f"{POPUPS}{posted['id']}/stop/")
    assert posted["id"] not in {row["id"] for row in client_for(approved.user).get(INBOX).json()}


def test_it_stays_in_the_inbox_after_its_end(admin):
    posted = post(admin).json()
    PopupMessage.objects.update(ends_at=timezone.now() - timedelta(days=1))
    approved = member_account("08033331011", name="Approved Member")
    assert posted["id"] in {row["id"] for row in client_for(approved.user).get(INBOX).json()}


def test_the_inbox_is_newest_first(admin):
    old = post(admin, title="Old").json()
    PopupMessage.objects.update(starts_at=timezone.now() - timedelta(days=3))
    member = complete(member_account("08033331012", name="Approved Member", verified=False))
    role = custom_role(admin, "Ward Mobiliser", [C.SEND_BROADCAST])
    identity.approve(actor=admin, member=member, notes="Seen at the ward office.")
    assert appoint(admin, member, role, [KD]).status_code == 201

    rows = client_for(member.user).get(INBOX).json()
    assert [row["title"] for row in rows] == [
        "You have been given a position",
        "Your identity is approved",
        "Old",
    ]
    assert rows[2]["id"] == old["id"] and rows[2]["popup"] is None


# --- notices ---------------------------------------------------------------------------


def test_someone_approved_is_told_with_no_sms_or_phone_service(admin, no_services):
    member = complete(member_account("08033331013", name="Ada Obi", verified=False))
    identity.approve(actor=admin, member=member, notes="Seen at the ward office.")

    [message] = client_for(member.user).get(INBOX).json()
    assert message["title"] == "Your identity is approved"
    notice = Broadcast.objects.get()
    assert notice.kind == Broadcast.Kind.NOTICE and notice.sender == admin
    assert notice.status == Broadcast.Status.DONE and notice.channels == [IN_APP]


def test_approvals_and_positions_never_cost_an_sms(admin):
    """A chapter's list can appoint thousands at once: SMS is ready in these tests."""
    member = complete(member_account("08033331021", name="Ada Obi", verified=False))
    identity.approve(actor=admin, member=member, notes="Seen at the ward office.")
    role = custom_role(admin, "Ward Mobiliser", [C.SEND_BROADCAST])
    assert appoint(admin, member, role, [KD]).status_code == 201
    assert Broadcast.objects.count() == 2
    assert not Delivery.objects.filter(channel=Channel.SMS).exists()


def test_someone_given_a_position_is_told_which_and_where(admin):
    role = custom_role(admin, "Ward Mobiliser", [C.SEND_BROADCAST])
    member = member_account("08033331014", name="Ada Obi")
    assert appoint(admin, member, role, [KD]).status_code == 201

    [message] = client_for(member.user).get(INBOX).json()
    assert message["title"] == "You have been given a position"
    assert message["body"] == (
        f"You are appointed Ward Mobiliser for {State.objects.get(code='KD').name}. "
        "Accept or decline it in Roles and access."
    )


def test_a_position_for_all_of_nigeria_says_so(admin):
    role = custom_role(admin, "National Mobiliser", [C.SEND_BROADCAST], levels=("country",))
    member = member_account("08033331015", name="Ada Obi")
    assert appoint(admin, member, role, [NIGERIA]).status_code == 201
    [message] = client_for(member.user).get(INBOX).json()
    assert "National Mobiliser for Nigeria" in message["body"]


def test_someone_approved_by_the_system_is_told_too(admin, monkeypatch):
    monkeypatch.setattr(identity, "meets_policy", lambda member, results: True)
    member = member_account("08033331016", name="Ada Obi", verified=False)
    identity.record_checks(member)
    notice = Broadcast.objects.get()
    assert notice.sender is None and notice.title == "Your identity is approved"


def test_someone_with_no_account_yet_finds_it_when_they_sign_in(admin):
    member = member_account("08033331017", name="Ada Obi")
    account, member.user = member.user, None
    member.save()
    role = custom_role(admin, "Ward Mobiliser", [C.SEND_BROADCAST])
    assert appoint(admin, member, role, [KD]).status_code == 201

    member.user = account
    member.save()
    [message] = client_for(account).get(INBOX).json()
    assert message["title"] == "You have been given a position"


def test_a_rejection_or_a_repeat_check_sends_no_approval(admin):
    member = member_account("08033331018", name="Ada Obi", verified=False)
    identity.reject(actor=admin, member=member, reason="The card is someone else's.")
    assert not Broadcast.objects.exists()


def test_notices_also_go_out_where_the_server_can_carry_them(admin):
    member = member_account("08033331020", name="Ada Obi")
    assert messaging.notify(sender=admin, member=member, title="Notice", body="For you.")
    notice = Broadcast.objects.get()
    assert notice.status == Broadcast.Status.SENDING and notice.channels[0] == IN_APP
    assert messaging.deliver() >= 1
    assert Broadcast.objects.get().status == Broadcast.Status.DONE
    [message] = client_for(member.user).get(INBOX).json()  # once, on however many channels
    assert message["title"] == "Notice"


def test_the_inbox_delivery_is_not_sent_or_counted_as_a_channel(admin, no_services):
    assert IN_APP not in Channel.values
    member = member_account("08033331019", name="Ada Obi")
    messaging.notify(sender=admin, member=member, title="Notice", body="For you.")
    row = Delivery.objects.get()
    assert row.channel == IN_APP and row.status == Delivery.Status.SENT
    assert messaging.deliver() == 0

    # Notices stay out of the sender's own list of messages.
    assert client_for(admin).get("/api/v1/broadcasts/").json()["results"] == []
    # And nobody can choose it when sending.
    refused = client_for(admin).post(
        "/api/v1/broadcasts/", {"body": "Hello", "channels": [IN_APP], "groups": []}, format="json"
    )
    assert refused.status_code == 400

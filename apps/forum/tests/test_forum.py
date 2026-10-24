"""The Social Forum: every member posts, comments and likes, at once;
editors (everyone holding a role or position) hide and restore anywhere."""

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework.test import APIClient

from apps.access.models import AuditEvent
from apps.access.tests.test_access import appoint, custom_role, member_account
from apps.accounts.models import User
from apps.core.choices import MembershipState, Role
from apps.core.permissions import Capability
from apps.forum.models import Attachment, Post
from apps.geography.models import Lga, PollingUnit, Ward
from conftest import PASSWORD

pytestmark = pytest.mark.django_db

POSTS = "/api/v1/forum/posts/"
REPLIES = "/api/v1/forum/replies/"
REPORTS = "/api/v1/forum/reports/"
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
MP4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64


@pytest.fixture(autouse=True)
def media(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path


@pytest.fixture
def area(db):
    lga = Lga.objects.get(code="KD-KADUNA-NORTH")
    ward = Ward.objects.create(code="KD-KN-W01", name="Ward 01", lga=lga)
    return {
        "lga": lga,
        "ward": ward,
        "unit": PollingUnit.objects.create(code="KD-KN-W01-PU001", name="Unit 1", ward=ward),
    }


def client(user):
    api = APIClient()
    api.force_authenticate(user)
    return api


@pytest.fixture
def people(area):
    admin = User.objects.create_user(
        "NA-1", PASSWORD, full_name="National Admin", role=Role.NATIONAL_ADMINISTRATOR
    )
    # An officer in Kano, holding a position: an editor everywhere.
    officer = member_account("08030000010", name="Kano Officer", lga="KN-AJINGI")
    role = custom_role(admin, "State Women Leader", [Capability.VIEW_GEOGRAPHY])
    assert appoint(admin, officer, role, [{"level": "state", "stateId": "KN"}]).status_code == 201
    suspended = member_account("08030000013", name="Suspended Member")
    suspended.membership_state = MembershipState.SUSPENDED
    suspended.save()
    return {
        "admin": admin,
        "officer": officer.user,
        # Ordinary members, their identity not yet checked: they take part.
        "member": member_account("08030000011", name="Hauwa Sani", verified=False).user,
        "other": member_account("08030000012", name="Bala Musa", verified=False).user,
        "suspended": suspended.user,
        # An account with neither a membership nor a role.
        "nobody": User.objects.create_user(
            "TGCG-000999", PASSWORD, full_name="No Membership", role=Role.MEMBER
        ),
    }


def post(user, **data):
    return client(user).post(POSTS, data, format="multipart")


def feed(user, **query):
    response = client(user).get(POSTS, query)
    assert response.status_code == 200, response.content
    return [item["id"] for item in response.json()["results"]]


def item(user, key):
    response = client(user).get(f"{POSTS}{key}/")
    assert response.status_code == 200, response.content
    return response.json()


def test_any_member_posts_and_everyone_sees_it_at_once(people):
    made = post(
        people["member"],
        body="Posters are up at the market.",
        assignment="Market  rally",
        level="state",
        code="LA",  # an older app's area is ignored: posts come from the author's own
        files=[SimpleUploadedFile("poster.jpg", JPEG, content_type="image/jpeg")],
    )
    assert made.status_code == 201, made.content
    created = made.json()
    assert created["status"] == "published"
    assert created["assignment"] == "Market rally"
    assert created["author_name"] == "Hauwa Sani" and created["author_role"] == ""
    assert created["scope"]["lgaId"] == "KD-KADUNA-NORTH"
    assert created["can_edit"] and not created["can_report"] and not created["can_hide"]
    key = created["id"]

    for viewer in ("other", "officer", "admin"):
        assert key in feed(people[viewer]), viewer
    # A suspended membership, or an account with neither membership nor role, is kept out.
    for outsider in ("suspended", "nobody"):
        assert client(people[outsider]).get(POSTS).status_code == 403, outsider
        assert post(people[outsider], body="Hello").status_code == 403, outsider

    # A post by someone holding a position names it.
    officer = post(people["officer"], body="From Kano").json()
    assert officer["author_role"] == "State Women Leader"
    assert officer["scope"]["lgaId"] == "KN-AJINGI"


def test_comments_and_answers_to_comments_one_level_deep(people):
    key = post(people["member"], body="Rally at noon.").json()["id"]
    comment = client(people["other"]).post(f"{POSTS}{key}/replies/", {"body": "Well done."})
    assert comment.status_code == 201, comment.content
    comment_id = comment.json()["id"]
    assert comment.json()["parent"] is None

    answer = client(people["member"]).post(
        f"{POSTS}{key}/replies/", {"body": "Thank you.", "parent": comment_id}, format="json"
    )
    assert answer.status_code == 201, answer.content
    assert answer.json()["parent"] == comment_id
    # An answer to an answer joins the same comment.
    again = client(people["officer"]).post(
        f"{POSTS}{key}/replies/", {"body": "Agreed.", "parent": answer.json()["id"]}, format="json"
    )
    assert again.json()["parent"] == comment_id

    listed = client(people["admin"]).get(f"{POSTS}{key}/replies/").json()
    assert [(r["body"], r["parent"]) for r in listed] == [
        ("Well done.", None),
        ("Thank you.", comment_id),
        ("Agreed.", comment_id),
    ]
    assert item(people["other"], key)["replies"] == 3

    other_post = post(people["other"], body="Elsewhere").json()["id"]
    wrong = client(people["member"]).post(
        f"{POSTS}{other_post}/replies/", {"body": "No", "parent": comment_id}, format="json"
    )
    assert wrong.status_code == 400


def test_likes_on_posts_and_comments(people):
    key = post(people["member"], body="Like me").json()["id"]
    like = f"{POSTS}{key}/like/"
    assert client(people["other"]).post(like).json() == {"liked": True, "likes": 1}
    assert client(people["other"]).post(like).json() == {"liked": True, "likes": 1}  # once
    assert client(people["officer"]).post(like).json() == {"liked": True, "likes": 2}
    seen = item(people["other"], key)
    assert seen["likes"] == 2 and seen["liked"] is True
    assert item(people["admin"], key)["liked"] is False
    assert client(people["other"]).delete(like).json() == {"liked": False, "likes": 1}

    comment = client(people["other"]).post(f"{POSTS}{key}/replies/", {"body": "Nice"}).json()
    liked = client(people["member"]).post(f"{REPLIES}{comment['id']}/like/")
    assert liked.json() == {"liked": True, "likes": 1}
    listed = client(people["member"]).get(f"{POSTS}{key}/replies/").json()
    assert listed[0]["likes"] == 1 and listed[0]["liked"] is True

    # A hidden post cannot be liked.
    client(people["officer"]).post(f"{POSTS}{key}/hide/", {"reason": "Spam"})
    assert client(people["member"]).post(like).status_code == 400


def test_authors_edit_and_delete_their_own_posts_and_comments(people):
    key = post(people["member"], body="Fist draft", assignment="Rally").json()["id"]
    edited = client(people["member"]).patch(
        f"{POSTS}{key}/", {"body": "First draft"}, format="json"
    )
    assert edited.status_code == 200, edited.content
    assert edited.json()["body"] == "First draft" and edited.json()["edited_at"]
    assert edited.json()["assignment"] == "Rally"
    event = AuditEvent.objects.get(action="forum.postEdited", target_id=key)
    assert event.changes == {"body": {"from": "Fist draft", "to": "First draft"}}
    other = client(people["other"]).patch(f"{POSTS}{key}/", {"body": "Mine now"}, format="json")
    assert other.status_code == 403

    comment = client(people["other"]).post(f"{POSTS}{key}/replies/", {"body": "Typo"}).json()
    fixed = client(people["other"]).patch(
        f"{REPLIES}{comment['id']}/", {"body": "Fixed"}, format="json"
    )
    assert fixed.json()["body"] == "Fixed" and fixed.json()["edited_at"]
    refused = client(people["member"]).patch(
        f"{REPLIES}{comment['id']}/", {"body": "No"}, format="json"
    )
    assert refused.status_code == 403
    assert client(people["other"]).delete(f"{REPLIES}{comment['id']}/").status_code == 204
    assert client(people["member"]).get(f"{POSTS}{key}/replies/").json() == []


def test_authors_delete_their_own_posts_and_files(
    people, tmp_path, django_capture_on_commit_callbacks
):
    made = post(
        people["member"], body="Wrong photo", files=[SimpleUploadedFile("wrong.jpg", JPEG)]
    ).json()
    stored = Attachment.objects.get().file.path
    assert client(people["other"]).delete(f"{POSTS}{made['id']}/").status_code == 403
    # The files go once the deletion is committed.
    with django_capture_on_commit_callbacks(execute=True):
        assert client(people["member"]).delete(f"{POSTS}{made['id']}/").status_code == 204
    assert made["id"] not in feed(people["other"])
    assert not Attachment.objects.exists()
    assert Post.objects.get().body == ""
    assert not tmp_path.joinpath(stored).exists()


def test_editors_hide_and_restore_anywhere_and_never_edit(people):
    key = post(people["member"], body="Off-message post").json()["id"]
    assert item(people["officer"], key)["can_hide"] is True
    assert item(people["other"], key)["can_hide"] is False
    # Ordinary members cannot hide.
    refused = client(people["other"]).post(f"{POSTS}{key}/hide/", {"reason": "No"})
    assert refused.status_code == 403

    # An officer in Kano hides a post from Kaduna, with a reason.
    hidden = client(people["officer"]).post(f"{POSTS}{key}/hide/", {"reason": "Not our work"})
    assert hidden.status_code == 200, hidden.content
    assert hidden.json()["status"] == "hidden" and hidden.json()["can_restore"]
    assert key not in feed(people["other"])
    assert client(people["other"]).get(f"{POSTS}{key}/").status_code == 404
    mine = client(people["member"]).get(POSTS, {"show": "mine"}).json()["results"]
    assert mine[0]["status"] == "hidden" and mine[0]["review_reason"] == "Not our work"
    assert feed(people["admin"], show="hidden") == [key]
    # An editor never changes another person's words.
    assert (
        client(people["officer"]).patch(f"{POSTS}{key}/", {"body": "X"}, format="json").status_code
        == 403
    )

    restored = client(people["admin"]).post(f"{POSTS}{key}/restore/")
    assert restored.json()["status"] == "published" and restored.json()["review_reason"] == ""
    assert key in feed(people["other"])
    assert AuditEvent.objects.filter(action="forum.postHidden", target_id=key).exists()
    assert AuditEvent.objects.filter(action="forum.postRestored", target_id=key).exists()

    comment = client(people["other"]).post(f"{POSTS}{key}/replies/", {"body": "Rude"}).json()
    url = f"{REPLIES}{comment['id']}/"
    assert client(people["member"]).post(f"{url}hide/", {"reason": "No"}).status_code == 403
    assert client(people["officer"]).post(f"{url}hide/", {"reason": "Rude"}).status_code == 200
    assert client(people["member"]).get(f"{POSTS}{key}/replies/").json() == []
    own = client(people["other"]).get(f"{POSTS}{key}/replies/").json()
    assert own[0]["status"] == "hidden" and own[0]["hide_reason"] == "Rude"
    assert client(people["officer"]).post(f"{url}restore/").json()["status"] == "visible"
    assert len(client(people["member"]).get(f"{POSTS}{key}/replies/").json()) == 1


def test_reports_go_to_the_editors(people):
    key = post(people["member"], body="Something wrong").json()["id"]
    reported = client(people["other"]).post(f"{POSTS}{key}/report/", {"reason": "Insulting"})
    assert reported.status_code == 201, reported.content
    report = reported.json()
    assert report["target"] == "post" and report["excerpt"] == "Something wrong"
    # The same person's second report is the same report.
    again = client(people["other"]).post(f"{POSTS}{key}/report/", {"reason": "Still"}).json()
    assert again["id"] == report["id"]
    assert (
        client(people["member"]).post(f"{POSTS}{key}/report/", {"reason": "x"}).status_code == 400
    )

    assert client(people["member"]).get(REPORTS).status_code == 403
    listed = client(people["officer"]).get(REPORTS).json()["results"]
    assert [(r["id"], r["reason"], r["reporter"]) for r in listed] == [
        (report["id"], "Insulting", "Bala Musa")
    ]

    # Hiding the post closes its reports.
    client(people["officer"]).post(f"{POSTS}{key}/hide/", {"reason": "Insulting"})
    assert client(people["officer"]).get(REPORTS).json()["results"] == []
    closed = client(people["officer"]).get(REPORTS, {"status": "closed"}).json()["results"]
    assert closed[0]["outcome"] == "hidden"

    # A comment reported and found fine is dismissed.
    other_post = post(people["other"], body="Fine").json()["id"]
    comment = client(people["other"]).post(f"{POSTS}{other_post}/replies/", {"body": "OK"}).json()
    made = client(people["member"]).post(f"{REPLIES}{comment['id']}/report/", {"reason": "Spam?"})
    assert made.json()["target"] == "reply" and made.json()["reply"] == comment["id"]
    dismissed = client(people["admin"]).post(f"{REPORTS}{made.json()['id']}/dismiss/")
    assert dismissed.json()["outcome"] == "dismissed"
    assert AuditEvent.objects.filter(action="forum.reportDismissed").count() == 1


def test_older_apps_see_no_approval(people):
    settings = client(people["member"]).get("/api/v1/forum/settings/")
    assert settings.json() == {"approval_required": False, "can_change": False, "updated_at": None}
    assert (
        client(people["admin"])
        .patch("/api/v1/forum/settings/", {"approval_required": True}, format="json")
        .status_code
        == 405
    )
    post(people["member"], body="Hi")
    assert feed(people["officer"], show="review") == []
    assert client(people["member"]).get("/api/v1/forum/me/").json() == {
        "takes_part": True,
        "editor": False,
    }
    assert client(people["officer"]).get("/api/v1/forum/me/").json()["editor"] is True


def test_pictures_and_videos_reach_whoever_sees_the_post(people):
    made = post(
        people["member"],
        body="",
        files=[
            SimpleUploadedFile("rally.jpg", JPEG, content_type="image/jpeg"),
            SimpleUploadedFile("rally.mp4", MP4, content_type="video/mp4"),
        ],
    ).json()
    assert [entry["kind"] for entry in made["attachments"]] == ["photo", "video"]
    video = made["attachments"][1]["url"]
    download = client(people["other"]).get(video)
    assert download.status_code == 200
    assert b"".join(download.streaming_content) == MP4
    assert download["Cache-Control"] == "private, no-store"
    assert client(people["nobody"]).get(video).status_code == 403


@pytest.mark.parametrize(
    ("name", "content"),
    [
        ("renamed.jpg", b"%PDF-1.4 not a picture"),
        ("notes.pdf", b"%PDF-1.4"),
        ("empty.mp4", b""),
    ],
)
def test_only_real_pictures_and_videos_are_accepted(people, name, content):
    made = post(people["member"], body="Look", files=[SimpleUploadedFile(name, content)])
    assert made.status_code == 400, made.content
    assert "files" in made.json() or "file" in made.json()
    assert not Post.objects.exists() and not Attachment.objects.exists()


def test_a_post_needs_words_or_a_file_and_at_most_the_limit_of_files(people, settings):
    assert post(people["member"], body="  ").status_code == 400
    settings.FORUM_MAX_ATTACHMENTS = 1
    too_many = post(
        people["member"],
        body="Two",
        files=[SimpleUploadedFile(f"{n}.jpg", JPEG) for n in range(2)],
    )
    assert too_many.status_code == 400
    settings.FORUM_MAX_ATTACHMENTS = 10
    settings.FORUM_MAX_POST_MB = 0
    too_big = post(people["member"], body="Big", files=[SimpleUploadedFile("big.jpg", JPEG)])
    assert too_big.status_code == 400
    assert not Post.objects.exists()


def test_assignments_are_tags_everyone_can_follow(people):
    first = post(people["member"], body="Day one", assignment="Market rally").json()["id"]
    second = post(people["officer"], body="Day two", assignment="market rally").json()["id"]
    post(people["other"], body="Untagged")

    tags = client(people["other"]).get("/api/v1/forum/assignments/").json()
    assert tags == [{"tag": "Market rally", "posts": 2}]
    assert set(feed(people["other"], assignment="MARKET RALLY")) == {first, second}
    assert len(feed(people["other"], assignments="true")) == 2

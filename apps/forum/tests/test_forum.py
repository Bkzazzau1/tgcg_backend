"""The forum: for everyone with a role; polling unit posts wait for the ward
coordinator while approval is on; replies appear at once."""

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework.test import APIClient

from apps.access.models import AuditEvent
from apps.accounts.models import User
from apps.core.choices import GeographyLevel, Role
from apps.forum.models import Attachment, Post
from apps.geography.models import Lga, PollingUnit, Ward
from conftest import PASSWORD, verified_member

pytestmark = pytest.mark.django_db

POSTS = "/api/v1/forum/posts/"
SETTINGS = "/api/v1/forum/settings/"
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
MP4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64


@pytest.fixture(autouse=True)
def media(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path


@pytest.fixture
def area(db):
    lga = Lga.objects.get(code="KD-KADUNA-NORTH")
    first = Ward.objects.create(code="KD-KN-W01", name="Ward 01", lga=lga)
    second = Ward.objects.create(code="KD-KN-W02", name="Ward 02", lga=lga)
    return {
        "lga": lga,
        "ward": first,
        "other_ward": second,
        "unit": PollingUnit.objects.create(code="KD-KN-W01-PU001", name="Unit 1", ward=first),
    }


def client(user):
    api = APIClient()
    api.force_authenticate(user)
    return api


def person(access_id, role, **scope):
    return User.objects.create_user(
        access_id, PASSWORD, full_name=f"Person {access_id}", role=role, **scope
    )


@pytest.fixture
def people(area):
    agent = person(
        "AG-1",
        Role.POLLING_UNIT_AGENT,
        scope_level=GeographyLevel.POLLING_UNIT,
        polling_unit=area["unit"],
        phone_number="08030000001",
    )
    verified_member(agent)
    return {
        "admin": person("NA-1", Role.NATIONAL_ADMINISTRATOR),
        "agent": agent,
        "ward": person(
            "WC-1", Role.WARD_COORDINATOR, scope_level=GeographyLevel.WARD, ward=area["ward"]
        ),
        "other_ward": person(
            "WC-2",
            Role.WARD_COORDINATOR,
            scope_level=GeographyLevel.WARD,
            ward=area["other_ward"],
        ),
        "lga": person(
            "LGA-1", Role.LGA_COORDINATOR, scope_level=GeographyLevel.LGA, lga=area["lga"]
        ),
        "member": person("TGCG-000999", Role.MEMBER),
    }


def post(user, **data):
    return client(user).post(POSTS, data, format="multipart")


def feed(user, **query):
    response = client(user).get(POSTS, query)
    assert response.status_code == 200, response.content
    return [item["id"] for item in response.json()["results"]]


def test_a_polling_unit_post_waits_for_its_own_ward_coordinator(people):
    made = post(
        people["agent"],
        body="Posters are up at the market.",
        assignment="Market  rally",
        files=[SimpleUploadedFile("poster.jpg", JPEG, content_type="image/jpeg")],
    )
    assert made.status_code == 201, made.content
    created = made.json()
    assert created["status"] == "pending"
    assert created["assignment"] == "Market rally"
    assert created["author_role"] == "Polling unit agent"
    assert created["scope"]["pollingUnitId"] == "KD-KN-W01-PU001"
    key = created["id"]

    # Waiting: only its author and its own ward coordinator see it.
    assert key in feed(people["agent"], show="mine")
    assert key not in feed(people["lga"])
    assert key not in feed(people["other_ward"], show="review")
    assert client(people["other_ward"]).post(f"{POSTS}{key}/approve/").status_code == 404
    # An LGA coordinator does not approve.
    assert client(people["lga"]).get(f"{POSTS}{key}/").status_code == 404
    assert client(people["lga"]).post(f"{POSTS}{key}/approve/").status_code == 403
    assert feed(people["ward"], show="review") == [key]

    approved = client(people["ward"]).post(f"{POSTS}{key}/approve/")
    assert approved.status_code == 200, approved.content
    assert approved.json()["status"] == "published"
    assert AuditEvent.objects.filter(action="forum.postApproved", target_id=key).exists()

    # Now everyone with a role sees it, across the country; never an ordinary member.
    for viewer in ("lga", "other_ward", "admin"):
        assert key in feed(people[viewer]), viewer
    assert client(people["member"]).get(POSTS).status_code == 403


def test_coordinators_and_national_roles_post_at_once(people):
    for author in ("ward", "lga", "admin"):
        made = post(people[author], body=f"Update from {author}.")
        assert made.status_code == 201, made.content
        assert made.json()["status"] == "published", author
    assert post(people["member"], body="Hello").status_code == 403


def test_only_the_administrator_switches_approval_off(people):
    assert client(people["ward"]).get(SETTINGS).json() == {
        "approval_required": True,
        "can_change": False,
        "updated_at": client(people["ward"]).get(SETTINGS).json()["updated_at"],
    }
    refused = client(people["ward"]).patch(SETTINGS, {"approval_required": False}, format="json")
    assert refused.status_code == 403

    switched = client(people["admin"]).patch(SETTINGS, {"approval_required": False}, format="json")
    assert switched.status_code == 200, switched.content
    assert switched.json()["approval_required"] is False
    assert switched.json()["can_change"] is True
    assert AuditEvent.objects.filter(action="forum.settingsChanged").count() == 1

    # Polling unit posts now appear at once.
    assert post(people["agent"], body="Straight in.").json()["status"] == "published"


def test_pictures_and_videos_reach_whoever_sees_the_post(people):
    made = post(
        people["agent"],
        body="",
        files=[
            SimpleUploadedFile("rally.jpg", JPEG, content_type="image/jpeg"),
            SimpleUploadedFile("rally.mp4", MP4, content_type="video/mp4"),
        ],
    ).json()
    assert [item["kind"] for item in made["attachments"]] == ["photo", "video"]
    video = made["attachments"][1]["url"]

    # Waiting: the coordinator of another ward cannot open it.
    assert client(people["other_ward"]).get(video).status_code == 404
    client(people["ward"]).post(f"{POSTS}{made['id']}/approve/")
    download = client(people["other_ward"]).get(video)
    assert download.status_code == 200
    assert b"".join(download.streaming_content) == MP4
    assert download["Cache-Control"] == "private, no-store"


@pytest.mark.parametrize(
    ("name", "content"),
    [
        ("renamed.jpg", b"%PDF-1.4 not a picture"),
        ("notes.pdf", b"%PDF-1.4"),
        ("empty.mp4", b""),
    ],
)
def test_only_real_pictures_and_videos_are_accepted(people, name, content):
    made = post(people["agent"], body="Look", files=[SimpleUploadedFile(name, content)])
    assert made.status_code == 400, made.content
    assert "files" in made.json() or "file" in made.json()
    assert not Post.objects.exists() and not Attachment.objects.exists()


def test_a_post_needs_words_or_a_file_and_at_most_the_limit_of_files(people, settings):
    assert post(people["agent"], body="  ").status_code == 400
    settings.FORUM_MAX_ATTACHMENTS = 1
    too_many = post(
        people["agent"],
        body="Two",
        files=[SimpleUploadedFile(f"{n}.jpg", JPEG) for n in range(2)],
    )
    assert too_many.status_code == 400
    settings.FORUM_MAX_ATTACHMENTS = 10
    settings.FORUM_MAX_POST_MB = 0
    too_big = post(people["agent"], body="Big", files=[SimpleUploadedFile("big.jpg", JPEG)])
    assert too_big.status_code == 400
    assert not Post.objects.exists()


def test_replies_appear_at_once_and_the_ward_coordinator_hides_them(people):
    key = post(people["agent"], body="Rally at noon.").json()["id"]
    early = client(people["lga"]).post(f"{POSTS}{key}/replies/", {"body": "Too soon"})
    assert early.status_code == 404  # still waiting: not visible to them yet
    client(people["ward"]).post(f"{POSTS}{key}/approve/")

    reply = client(people["lga"]).post(f"{POSTS}{key}/replies/", {"body": "Well done."})
    assert reply.status_code == 201, reply.content
    reply_id = reply.json()["id"]
    assert [r["body"] for r in client(people["agent"]).get(f"{POSTS}{key}/replies/").json()] == [
        "Well done."
    ]

    hidden = client(people["ward"]).post(
        f"/api/v1/forum/replies/{reply_id}/hide/", {"reason": "Off topic"}
    )
    assert hidden.status_code == 200, hidden.content
    assert client(people["agent"]).get(f"{POSTS}{key}/replies/").json() == []
    own = client(people["lga"]).get(f"{POSTS}{key}/replies/").json()
    assert own[0]["status"] == "hidden" and own[0]["hide_reason"] == "Off topic"
    # Another ward's coordinator cannot hide replies here.
    assert (
        client(people["other_ward"])
        .post(f"/api/v1/forum/replies/{reply_id}/hide/", {"reason": "No"})
        .status_code
        == 403
    )


def test_a_refused_post_tells_its_author_why(people):
    key = post(people["agent"], body="Off-message post").json()["id"]
    refused = client(people["ward"]).post(f"{POSTS}{key}/reject/", {"reason": "Not about our work"})
    assert refused.json()["status"] == "rejected"
    mine = client(people["agent"]).get(POSTS, {"show": "mine"}).json()["results"]
    assert mine[0]["review_reason"] == "Not about our work"
    assert key not in feed(people["lga"])


def test_authors_delete_their_own_posts_and_files(
    people, tmp_path, django_capture_on_commit_callbacks
):
    made = post(
        people["ward"],
        body="Wrong photo",
        files=[SimpleUploadedFile("wrong.jpg", JPEG)],
    ).json()
    stored = Attachment.objects.get().file.path
    assert client(people["lga"]).delete(f"{POSTS}{made['id']}/").status_code == 403
    # The files go once the deletion is committed.
    with django_capture_on_commit_callbacks(execute=True):
        assert client(people["ward"]).delete(f"{POSTS}{made['id']}/").status_code == 204
    assert made["id"] not in feed(people["lga"])
    assert not Attachment.objects.exists()
    assert Post.objects.get().body == ""
    assert not tmp_path.joinpath(stored).exists()


def test_assignments_are_tags_everyone_can_follow(people):
    client(people["admin"]).patch(SETTINGS, {"approval_required": False}, format="json")
    first = post(people["agent"], body="Day one", assignment="Market rally").json()["id"]
    second = post(people["ward"], body="Day two", assignment="market rally").json()["id"]
    post(people["lga"], body="Untagged")

    tags = client(people["lga"]).get("/api/v1/forum/assignments/").json()
    assert tags == [{"tag": "Market rally", "posts": 2}]
    assert set(feed(people["lga"], assignment="MARKET RALLY")) == {first, second}
    assert len(feed(people["lga"], assignments="true")) == 2

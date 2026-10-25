"""Ask Me (25 October 2026): answers only from TGCG's approved library."""

import json
import socket
from datetime import timedelta
from io import StringIO

import pytest
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.utils import timezone

from apps.access.models import AuditEvent
from apps.access.tests.test_access import KD, appoint, client_for, custom_role, member_account
from apps.accounts.models import User
from apps.askme import pages
from apps.askme.models import AskMeSettings, KnowledgeItem, Question, SourcePage, TrustedSite
from apps.core.choices import Role
from apps.core.permissions import Capability
from conftest import PASSWORD

pytestmark = pytest.mark.django_db

ASK = "/api/v1/askme/ask/"
ME = "/api/v1/askme/me/"
ITEMS = "/api/v1/askme/items/"
REVIEW = "/api/v1/askme/review/"
SETTINGS = "/api/v1/askme/settings/"
PAGE_URL = "https://statehouse.gov.ng/news/student-loans"


@pytest.fixture
def admin():
    return User.objects.create_user(
        "NA-1", PASSWORD, full_name="National Admin", role=Role.NATIONAL_ADMINISTRATOR
    )


@pytest.fixture
def reviewer(admin):
    role = custom_role(admin, "Ask Me Reviewer", [Capability.REVIEW_ASK_ME])
    member = member_account("08035550101", name="Hausa Reviewer")
    assert appoint(admin, member, role, [KD]).status_code == 201
    return member.user


@pytest.fixture
def member():
    return member_account("08035550102", name="Ada Obi").user


@pytest.fixture
def library(admin):
    site = TrustedSite.objects.create(domain="statehouse.gov.ng", name="State House")
    page = SourcePage.objects.create(site=site, url=PAGE_URL, title="Student loans", text="...")
    loans = KnowledgeItem.objects.create(
        title="Student loans",
        fact="NELFUND has paid loans to 300,000 students.",
        argument="Poor students can finish school.",
        source_page=page,
        source_url=PAGE_URL,
        status=KnowledgeItem.Status.APPROVED,
    )
    pvc = KnowledgeItem.objects.create(
        topic="voting",
        title="Collecting your PVC",
        fact="Collect your PVC at your INEC local government office.",
        status=KnowledgeItem.Status.APPROVED,
    )
    draft = KnowledgeItem.objects.create(title="Unapproved claim", fact="Not checked yet.")
    return {"site": site, "page": page, "loans": loans, "pvc": pvc, "draft": draft}


@pytest.fixture
def open_ask_me():
    AskMeSettings.objects.update_or_create(pk=1, defaults={"enabled": True})


@pytest.fixture
def ai(monkeypatch, settings):
    """A stand-in for Gemini: answers what the test sets, and remembers prompts."""
    settings.GEMINI_API_KEY = "test-key"
    calls = []
    state = {"answer": {}}

    def generate_json(*, system, text, schema, temperature=0.3):
        calls.append({"system": system, "text": text})
        return state["answer"]

    monkeypatch.setattr("apps.askme.gemini.generate_json", generate_json)
    return {"calls": calls, "state": state}


def ask(user, text, language=""):
    response = client_for(user).post(ASK, {"text": text, "language": language}, format="json")
    assert response.status_code == 201, response.content
    return response.json()


def test_a_member_is_answered_only_from_approved_items(member, library, open_ask_me, ai):
    loans = library["loans"]
    ai["state"]["answer"] = {
        "status": "answered",
        "language": "ha",
        "answer": "Gwamnati ta ba dalibai 300,000 lamuni. https://example.com",
        "cited": [loans.pk],
    }
    answer = ask(member, "Me gwamnati ta yi wa dalibai?", "ha")

    assert answer["status"] == "answered"
    assert answer["answer"] == "Gwamnati ta ba dalibai 300,000 lamuni."  # links are dropped
    assert answer["sources"] == [{"title": "Student loans", "url": PAGE_URL, "date": None}]
    prompt = ai["calls"][0]["system"]
    assert "Student loans" in prompt and "Collecting your PVC" in prompt
    assert "Unapproved claim" not in prompt
    assert "Reply in Hausa." in prompt
    assert "never pretend to be a person, the President, INEC" in prompt
    question = Question.objects.get()
    assert question.route == "ai" and list(question.items.all()) == [loans]


@pytest.mark.parametrize(
    ("answer", "cited", "why"),
    [
        ("Over 900,000 students got loans.", "loans", "numbers not in the library"),
        ("Students got loans.", "draft", "an item it was not given"),
        ("Students got loans.", None, "no library item"),
    ],
)
def test_the_server_holds_back_an_answer_the_library_does_not_support(
    member, library, open_ask_me, ai, answer, cited, why
):
    ai["state"]["answer"] = {
        "status": "answered",
        "language": "en",
        "answer": answer,
        "cited": [library[cited].pk] if cited else [],
    }
    shown = ask(member, "What has the government done for students?")
    assert shown["status"] == "not_yet"
    assert "TGCG will add it" in shown["answer"]
    assert why in Question.objects.get().check_failed


def test_without_a_key_questions_wait_for_a_reviewer_whose_answer_is_reused(
    member, reviewer, library, open_ask_me, settings
):
    settings.GEMINI_API_KEY = ""
    first = ask(member, "How do I collect my PVC?")
    assert first["status"] == "not_yet"

    queue = client_for(reviewer).get(REVIEW).json()["results"]
    assert [row["text"] for row in queue] == ["How do I collect my PVC?"]
    assert "asker" not in queue[0] and "Ada" not in json.dumps(queue)
    decided = client_for(reviewer).post(
        f"{REVIEW}{queue[0]['id']}/decide/",
        {"decision": "correct", "corrected_answer": "Go to your INEC local government office."},
        format="json",
    )
    assert decided.status_code == 200, decided.content

    other = member_account("08035550103", name="Bala Umar").user
    again = ask(other, "how do i collect my PVC")
    assert again["status"] == "answered"
    assert again["answer"] == "Go to your INEC local government office."
    assert Question.objects.filter(route="ready").count() == 1
    # A reused answer is not waiting for review.
    assert client_for(reviewer).get(REVIEW).json()["count"] == 0


def test_ask_me_is_off_for_members_until_opened_but_reviewers_can_test(
    member, reviewer, library, settings
):
    settings.GEMINI_API_KEY = ""
    assert ask(member, "Hello?")["status"] == "off"
    assert client_for(member).get(ME).json()["can_ask"] is False
    assert ask(reviewer, "Hello?")["status"] == "not_yet"
    me = client_for(reviewer).get(ME).json()
    assert me["can_ask"] and me["can_review"] and not me["can_approve"]


def test_members_have_a_daily_limit(member, library, open_ask_me, settings):
    settings.GEMINI_API_KEY = ""
    AskMeSettings.objects.filter(pk=1).update(daily_limit=1)
    assert ask(member, "First?")["status"] == "not_yet"
    assert ask(member, "Second?")["status"] == "limit"


def test_campaign_silence_leaves_achievements_out(member, library, open_ask_me, ai):
    now = timezone.now()
    AskMeSettings.objects.filter(pk=1).update(
        silence_starts_at=now - timedelta(hours=1), silence_ends_at=now + timedelta(hours=23)
    )
    ai["state"]["answer"] = {
        "status": "not_covered",
        "language": "en",
        "answer": "I don't have that yet.",
        "cited": [],
    }
    ask(member, "What has Tinubu done?")
    prompt = ai["calls"][0]["system"]
    assert "Student loans" not in prompt
    assert "Collecting your PVC" in prompt
    assert "Campaign silence is in force" in prompt


def test_reviewers_build_the_library_and_only_the_administrator_approves(
    admin, reviewer, member, monkeypatch
):
    assert client_for(member).get(ITEMS).status_code == 403
    site = {"domain": "https://www.statehouse.gov.ng/", "name": "State House"}
    assert client_for(reviewer).post("/api/v1/askme/sites/", site, format="json").status_code == 403
    added = client_for(admin).post("/api/v1/askme/sites/", site, format="json")
    assert added.status_code == 201 and added.json()["domain"] == "statehouse.gov.ng"

    monkeypatch.setattr(
        "apps.askme.services.pages.read",
        lambda url, sites: {
            "title": "Loans",
            "text": "NELFUND paid 300,000 students.",
            "hash": "a",
        },
    )
    imported = client_for(reviewer).post("/api/v1/askme/pages/", {"url": PAGE_URL}, format="json")
    assert imported.status_code == 201, imported.content
    elsewhere = client_for(reviewer).post(
        "/api/v1/askme/pages/", {"url": "https://example.com/news"}, format="json"
    )
    assert elsewhere.status_code == 400

    item = client_for(reviewer).post(
        ITEMS,
        {
            "title": "Student loans",
            "fact": "NELFUND paid 300,000 students.",
            "source_page": imported.json()["id"],
        },
        format="json",
    )
    assert item.status_code == 201, item.content
    body = item.json()
    assert body["status"] == "draft" and body["source_url"] == PAGE_URL
    approve = f"{ITEMS}{body['id']}/approve/"
    assert client_for(reviewer).post(approve).status_code == 403
    assert client_for(admin).post(approve).json()["status"] == "approved"
    edited = client_for(reviewer).patch(f"{ITEMS}{body['id']}/", {"argument": "It matters."})
    assert edited.json()["status"] == "draft"  # every edit is approved again
    assert AuditEvent.objects.filter(action="askme.itemApproved").count() == 1


def test_a_changed_page_marks_its_items_for_review(library, monkeypatch):
    from apps.askme import services

    page, loans = library["page"], library["loans"]
    page.content_hash = "old"
    page.save()
    monkeypatch.setattr(
        "apps.askme.services.pages.read",
        lambda url, sites: {"title": "Loans", "text": "New words.", "hash": "new"},
    )
    assert services.recheck_page(page) == "changed"
    loans.refresh_from_db()
    assert loans.review_note.startswith("Its source page changed")
    assert loans.status == KnowledgeItem.Status.APPROVED  # it keeps answering meanwhile
    out = StringIO()
    call_command("check_askme_sources", stdout=out)
    assert "unchanged 1" in out.getvalue()


def test_ai_drafts_from_a_page_drop_numbers_the_page_does_not_have(reviewer, library, ai):
    page = library["page"]
    page.text = "NELFUND has paid loans to 300,000 students since 2024."
    page.save()
    ai["state"]["answer"] = {
        "items": [
            {"title": "Loans", "topic": "achievement", "fact": "300,000 students got loans."},
            {"title": "Invented", "topic": "achievement", "fact": "500,000 students got loans."},
        ]
    }
    made = client_for(reviewer).post(f"/api/v1/askme/pages/{page.uuid}/suggest/")
    assert made.status_code == 201, made.content
    assert [item["title"] for item in made.json()] == ["Loans"]
    assert made.json()[0]["status"] == "draft"


def test_page_links_must_be_safe_and_trusted(monkeypatch):
    sites = [TrustedSite(domain="statehouse.gov.ng", name="State House", active=True)]
    for bad in (
        "http://statehouse.gov.ng/a",
        "https://statehouse.gov.ng:8443/a",
        "https://statehouse.gov.ng.evil.com/a",
        "https://example.com/a",
    ):
        with pytest.raises(ValidationError):
            pages.trusted_site(bad, sites)
    assert pages.trusted_site("https://www.statehouse.gov.ng/a", sites) is sites[0]
    monkeypatch.setattr(
        socket, "getaddrinfo", lambda *args, **kwargs: [(None, None, None, "", ("10.0.0.5", 443))]
    )
    with pytest.raises(ValidationError, match="not a public website"):
        pages.read("https://statehouse.gov.ng/a", sites)


def test_page_text_keeps_the_words_and_drops_menus_and_scripts():
    title, text = pages.page_text(
        "<html><head><title>Student Loans</title><script>var x=1</script></head><body>"
        "<nav>Home About</nav><h1>Loans</h1><p>NELFUND paid <b>300,000</b> students.</p>"
        "<footer>Copyright</footer></body></html>"
    )
    assert title == "Student Loans"
    assert text == "Loans\n\nNELFUND paid 300,000 students."


def test_consent_rating_and_the_training_export(
    member, reviewer, library, open_ask_me, ai, tmp_path
):
    assert client_for(member).get(ME).json()["training_ok"] is None
    assert client_for(member).patch(ME, {"training_ok": True}, format="json").json()["training_ok"]
    ai["state"]["answer"] = {
        "status": "answered",
        "language": "en",
        "answer": "NELFUND has paid loans to 300,000 students.",
        "cited": [library["loans"].pk],
    }
    asked = ask(member, "What about student loans?")
    rated = client_for(member).post(
        f"/api/v1/askme/questions/{asked['id']}/rate/",
        {"rating": -1, "reason": "Too short"},
        format="json",
    )
    assert rated.status_code == 200 and rated.json()["rating"] == -1
    flagged = client_for(reviewer).get(f"{REVIEW}?kind=flagged").json()["results"]
    assert flagged[0]["flag_reason"] == "Too short" and flagged[0]["training_ok"] is True
    client_for(reviewer).post(f"{REVIEW}{asked['id']}/decide/", {"decision": "approve"})

    out = tmp_path / "training.jsonl"
    call_command("export_askme_training", "--out", str(out), stdout=StringIO())
    rows = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
    assert rows == [
        {
            "language": "en",
            "question": "What about student loans?",
            "answer": "NELFUND has paid loans to 300,000 students.",
            "status": "answered",
            "items": [
                {
                    "title": "Student loans",
                    "fact": "NELFUND has paid loans to 300,000 students.",
                    "argument": "Poor students can finish school.",
                }
            ],
        }
    ]
    assert "Ada" not in out.read_text(encoding="utf-8")


def test_only_the_administrator_changes_the_settings(admin, reviewer):
    change = {"enabled": True, "tone": "passionate", "not_yet_messages": {"ha": "Ba mu da shi ba."}}
    assert client_for(reviewer).patch(SETTINGS, change, format="json").status_code == 403
    saved = client_for(admin).patch(SETTINGS, change, format="json")
    assert saved.status_code == 200, saved.content
    assert saved.json()["enabled"] and saved.json()["tone"] == "passionate"
    assert AuditEvent.objects.filter(action="askme.settingsChanged").exists()
    bad = client_for(admin).patch(SETTINGS, {"not_yet_messages": {"fr": "x"}}, format="json")
    assert bad.status_code == 400

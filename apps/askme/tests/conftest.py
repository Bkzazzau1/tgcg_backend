"""Fixtures shared by Ask Me's tests."""

import pytest

from apps.access.tests.test_access import KD, appoint, custom_role, member_account
from apps.accounts.models import User
from apps.askme.models import AskMeSettings, KnowledgeItem, SourcePage, TrustedSite
from apps.core.choices import Role
from apps.core.permissions import Capability
from conftest import PASSWORD

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

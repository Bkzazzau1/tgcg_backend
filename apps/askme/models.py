"""Ask Me (25 October 2026): members ask in English, Hausa, Yoruba, Igbo or
Pidgin, and an AI answers only from TGCG's approved library, built from pages
of trusted official websites. Every question is kept for review; reviewed
answers are reused for the same question and, with the asker's consent,
become training data for TGCG's own model.
"""

from django.conf import settings
from django.db import models

from apps.core.models import TimeStampedModel


class Language(models.TextChoices):
    ENGLISH = "en", "English"
    HAUSA = "ha", "Hausa"
    YORUBA = "yo", "Yoruba"
    IGBO = "ig", "Igbo"
    PIDGIN = "pcm", "Nigerian Pidgin"


class Topic(models.TextChoices):
    ACHIEVEMENT = "achievement", "Government achievement"
    VOTING = "voting", "Voting and taking part"
    TGCG = "tgcg", "TGCG and its app"


class TrustedSite(TimeStampedModel):
    """An official website pages may be imported from (it and its subdomains)."""

    domain = models.CharField(max_length=120, unique=True, help_text="e.g. statehouse.gov.ng")
    name = models.CharField(max_length=150)
    active = models.BooleanField(default=True)
    added_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, related_name="+"
    )

    class Meta:
        ordering = ["domain"]

    def __str__(self):
        return self.domain

    def covers(self, host: str) -> bool:
        host = host.lower().rstrip(".")
        return host == self.domain or host.endswith(f".{self.domain}")


class SourcePage(TimeStampedModel):
    """A copy of an official page, kept so answers do not depend on the site
    being up; re-read weekly, and its items are marked for review when it
    changes."""

    class Status(models.TextChoices):
        OK = "ok", "Read"
        FAILED = "failed", "Could not be read"

    site = models.ForeignKey(TrustedSite, on_delete=models.PROTECT, related_name="pages")
    url = models.URLField(max_length=500, unique=True)
    title = models.CharField(max_length=300, blank=True)
    text = models.TextField(blank=True)
    content_hash = models.CharField(max_length=64, blank=True)
    status = models.CharField(max_length=8, choices=Status.choices, default=Status.OK)
    error = models.CharField(max_length=300, blank=True)
    fetched_at = models.DateTimeField(null=True, blank=True, help_text="Last successful read.")
    checked_at = models.DateTimeField(null=True, blank=True)
    changed_at = models.DateTimeField(
        null=True, blank=True, help_text="When a re-read found different words."
    )
    added_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, related_name="+"
    )

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.url


class KnowledgeItem(TimeStampedModel):
    """One approved piece of what Ask Me may say: a fact with its source, the
    argument for it, and the approved replies to common complaints."""

    class Status(models.TextChoices):
        DRAFT = "draft", "Waiting for approval"
        APPROVED = "approved", "Approved"
        RETIRED = "retired", "Retired"

    topic = models.CharField(max_length=16, choices=Topic.choices, default=Topic.ACHIEVEMENT)
    title = models.CharField(max_length=200)
    fact = models.TextField(max_length=2000)
    argument = models.TextField(max_length=2000, blank=True)
    objections = models.TextField(
        max_length=3000,
        blank=True,
        help_text="Common complaints and the approved reply to each.",
    )
    keywords = models.CharField(
        max_length=500, blank=True, help_text="Words in any language that point to this item."
    )
    source_page = models.ForeignKey(
        SourcePage, on_delete=models.SET_NULL, null=True, blank=True, related_name="items"
    )
    source_url = models.URLField(max_length=500, blank=True)
    fact_date = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.DRAFT)
    review_note = models.CharField(
        max_length=300, blank=True, help_text="Why it needs another look (e.g. its page changed)."
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, related_name="+"
    )
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    approved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["topic", "title"]
        indexes = [models.Index(fields=["status", "topic"])]

    def __str__(self):
        return self.title


class AskMeSettings(models.Model):
    """The one row of switches a National Administrator controls."""

    class Tone(models.TextChoices):
        CALM = "calm", "Calm explainer"
        PASSIONATE = "passionate", "Passionate advocate"

    enabled = models.BooleanField(
        default=False, help_text="Members may ask. Reviewers can always test."
    )
    tone = models.CharField(max_length=12, choices=Tone.choices, default=Tone.CALM)
    daily_limit = models.PositiveIntegerField(default=20, help_text="Questions per member a day.")
    monthly_ai_limit = models.PositiveIntegerField(
        default=5000, help_text="AI answers a month; reviewed answers do not count."
    )
    silence_starts_at = models.DateTimeField(
        null=True, blank=True, help_text="Campaign silence: voting help only, no persuasion."
    )
    silence_ends_at = models.DateTimeField(null=True, blank=True)
    not_yet_messages = models.JSONField(
        default=dict,
        blank=True,
        help_text="What Ask Me says when it has no approved answer, by language code.",
    )
    voice_enabled = models.BooleanField(
        default=True, help_text="Members may speak questions and hear answers (needs Spitch)."
    )
    voices = models.JSONField(
        default=dict, blank=True, help_text="Spitch's voice for each language code."
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, related_name="+"
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Ask Me settings"
        verbose_name_plural = "Ask Me settings"

    def __str__(self):
        return "Ask Me settings"


class Preference(models.Model):
    """Whether a person lets TGCG use their questions to train its own model."""

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="askme_preference"
    )
    training_ok = models.BooleanField(default=False)
    voice_ok = models.BooleanField(
        null=True, help_text="May keep their spoken questions to train TGCG's listening AI."
    )
    decided_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Ask Me consent of {self.user_id}"


class Question(TimeStampedModel):
    """A question and what Ask Me answered. Reviewers see it without the
    asker's name; a reviewed answer is reused for the same question."""

    class Status(models.TextChoices):
        ANSWERED = "answered", "Answered"
        NOT_YET = "not_yet", "Not in the library yet"
        REFUSED = "refused", "Refused"
        LIMIT = "limit", "Over the limit"
        OFF = "off", "Ask Me is off"
        ERROR = "error", "Could not answer"

    class Route(models.TextChoices):
        READY = "ready", "A reviewed answer"
        AI = "ai", "AI from the library"
        NONE = "none", "No answer"

    class Review(models.TextChoices):
        UNREVIEWED = "unreviewed", "Not reviewed"
        APPROVED = "approved", "Approved"
        CORRECTED = "corrected", "Corrected"
        REJECTED = "rejected", "Rejected"

    asker = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+"
    )

    class Channel(models.TextChoices):
        TEXT = "text", "Typed"
        VOICE = "voice", "Spoken"

    language = models.CharField(max_length=4, choices=Language.choices, blank=True)
    channel = models.CharField(max_length=6, choices=Channel.choices, default=Channel.TEXT)
    text = models.TextField(
        max_length=1000, help_text="As typed, or as the voice service heard it."
    )
    corrected_text = models.TextField(
        max_length=1000, blank=True, help_text="What was really said, written by a reviewer."
    )
    recording = models.FileField(
        upload_to="askme/questions/%Y/%m/",
        blank=True,
        help_text="Kept only when the asker allowed it, to train TGCG's listening AI.",
    )
    normalized = models.CharField(max_length=1000, db_index=True)
    status = models.CharField(max_length=10, choices=Status.choices)
    route = models.CharField(max_length=6, choices=Route.choices, default=Route.NONE)
    answer = models.TextField(blank=True)
    items = models.ManyToManyField(KnowledgeItem, blank=True, related_name="questions")
    check_failed = models.CharField(
        max_length=200, blank=True, help_text="Why the server held back the AI's answer."
    )
    latency_ms = models.PositiveIntegerField(null=True, blank=True)
    rating = models.SmallIntegerField(null=True, blank=True, help_text="1 helpful, -1 not.")
    flag_reason = models.CharField(max_length=300, blank=True)
    review = models.CharField(max_length=10, choices=Review.choices, default=Review.UNREVIEWED)
    corrected_answer = models.TextField(blank=True)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    training_ok = models.BooleanField(
        default=False, help_text="The asker allowed TGCG to train on it."
    )

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["review", "status"])]

    @property
    def shown_answer(self) -> str:
        return self.corrected_answer or self.answer


class SpokenAnswer(models.Model):
    """An answer read aloud, kept so the same words in the same voice are
    made once."""

    key = models.CharField(max_length=64, unique=True, help_text="sha256 of language, voice, text.")
    language = models.CharField(max_length=4, choices=Language.choices)
    voice = models.CharField(max_length=20)
    audio = models.FileField(upload_to="askme/speech/%Y/%m/")
    size = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.language} {self.voice} {self.key[:8]}"

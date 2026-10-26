"""Ask Me: answers only from TGCG's approved library (decided 25 October 2026).

A question gets, in order:
1. a reviewed answer to the same question, when there is one (fast, and it
   needs no AI);
2. otherwise Gemini's answer, written only from the approved items Ask Me
   sends it, which the server then checks: it must cite items it was given,
   and every number in it must appear in those items or in the question. An
   answer that fails is held back and the member is told "not yet".

Without a Gemini key every new question is "not yet" and waits for a
reviewer, whose answer becomes the ready answer. Reviewers see questions
without the asker's name.
"""

import hashlib
import re
import time
import unicodedata
import uuid
from datetime import date
from pathlib import Path

from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.base import ContentFile
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from apps.access import audit
from apps.core.permissions import Capability, has_capability
from apps.forum.services import takes_part

from . import gemini, pages, spitch
from .models import (
    AskMeSettings,
    KnowledgeItem,
    Language,
    Preference,
    Question,
    SourcePage,
    SpokenAnswer,
    Topic,
    TrustedSite,
)

C = Capability
S = Question.Status
R = Question.Route
REVIEWED = (Question.Review.APPROVED, Question.Review.CORRECTED)
# Small libraries go to Gemini whole; bigger ones are narrowed first.
WHOLE_LIBRARY = 120
NARROWED_TO = 40
NOT_YET = (
    "I don't have an approved answer to that yet. TGCG will add it; please ask something else."
)
OFF = "Ask Me is not open yet."
LIMIT = "You have asked the most questions allowed today. Please come back tomorrow."
BUSY = "Ask Me is very busy this month. Please try again later."
FAILED = "Ask Me could not answer just now. Please try again."
REFUSED = "I can't help with that. Ask me about the government's work or how to vote."


# --- who may do what -------------------------------------------------------------


def can_review(user) -> bool:
    return has_capability(user, C.REVIEW_ASK_ME, C.APPROVE_ASK_ME)


def can_approve(user) -> bool:
    return has_capability(user, C.APPROVE_ASK_ME)


def _require(allowed: bool, message: str):
    if not allowed:
        raise PermissionDenied(message)


def current_settings() -> AskMeSettings:
    found, _ = AskMeSettings.objects.get_or_create(pk=1)
    return found


def in_silence(config: AskMeSettings, now=None) -> bool:
    now = now or timezone.now()
    starts, ends = config.silence_starts_at, config.silence_ends_at
    return bool(starts and ends and starts <= now < ends)


def _audit(actor, action, target_type, target_id, label="", **fields):
    audit.record(
        actor=actor,
        action=action,
        target_type=target_type,
        target_id=target_id,
        target_label=str(label)[:200],
        changes=fields or None,
    )


# --- matching ----------------------------------------------------------------------


def normalize(text: str) -> str:
    """Lower case, no tone marks or punctuation: the same question typed
    differently matches its reviewed answer."""
    plain = unicodedata.normalize("NFKD", text.lower())
    plain = "".join(char for char in plain if not unicodedata.combining(char))
    plain = re.sub(r"[^\w\s]", " ", plain)
    return re.sub(r"\s+", " ", plain).strip()[:1000]


def _words(text: str) -> set[str]:
    return {word for word in normalize(text).split() if len(word) > 2}


def select_items(question: str, items: list[KnowledgeItem]) -> list[KnowledgeItem]:
    """The items Gemini sees: all of a small library, else the closest ones."""
    if len(items) <= WHOLE_LIBRARY:
        return items
    asked = _words(question)

    def score(item):
        strong = _words(f"{item.title} {item.keywords}")
        return 3 * len(asked & strong) + len(asked & _words(item.fact))

    ranked = sorted(items, key=score, reverse=True)
    return ranked[:NARROWED_TO]


def library(*, silence: bool) -> list[KnowledgeItem]:
    items = KnowledgeItem.objects.filter(status=KnowledgeItem.Status.APPROVED)
    if silence:
        items = items.exclude(topic=Topic.ACHIEVEMENT)
    return list(items.order_by("pk"))


def _item_text(item: KnowledgeItem) -> str:
    parts = [f"[{item.pk}] ({item.get_topic_display()}) {item.title}", f"Fact: {item.fact}"]
    if item.argument:
        parts.append(f"Argument: {item.argument}")
    if item.objections:
        parts.append(f"Complaints and replies: {item.objections}")
    if item.source_url:
        dated = f" ({item.fact_date.isoformat()})" if item.fact_date else ""
        parts.append(f"Source: {item.source_url}{dated}")
    return "\n".join(parts)


# --- the prompt and the checks -----------------------------------------------------

TONES = {
    AskMeSettings.Tone.CALM: "Calm and clear, like a patient teacher.",
    AskMeSettings.Tone.PASSIONATE: (
        "Confident and passionate, like a committed campaigner who believes in the "
        "government's work, but always respectful."
    ),
}

ANSWER_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "status": {"type": "STRING", "enum": ["answered", "not_covered", "refused"]},
        "language": {"type": "STRING", "enum": list(Language.values)},
        "answer": {"type": "STRING"},
        "cited": {"type": "ARRAY", "items": {"type": "INTEGER"}},
    },
    "required": ["status", "language", "answer", "cited"],
}


def system_prompt(config: AskMeSettings, *, language: str, silence: bool, items) -> str:
    reply_in = (
        f"Reply in {Language(language).label}."
        if language
        else "Reply in the language of the question (English, Hausa, Yoruba, Igbo or "
        "Nigerian Pidgin)."
    )
    rules = [
        "You are TGCG Ask Me, an AI assistant of The Game Changer Group (TGCG), which "
        "supports the government of President Bola Ahmed Tinubu. You are an AI: never "
        "pretend to be a person, the President, INEC or a news outlet, and say you are "
        "TGCG's AI if asked.",
        "Answer ONLY from the LIBRARY below. Never use other knowledge. Never guess or "
        "invent numbers, dates, names, quotes or events.",
        reply_in,
        f"Style: {TONES.get(config.tone, TONES[AskMeSettings.Tone.CALM])}",
        "When the person raises a concern or complaint, first acknowledge it honestly, "
        "then make the case with facts and arguments from the library. Be respectful: no "
        "insults, nothing about anyone's private life, nothing ethnic or religious.",
        "Keep it short: two to four sentences. Where it fits, end with one action they "
        "can take: check their registration, collect their PVC, know their polling unit, "
        "or volunteer with TGCG.",
        "Refuse, briefly, anything about buying or selling votes, gifts for votes, voting "
        "more than once or for someone else, violence, intimidation, rigging or false "
        'results; set status "refused" and mention a lawful way to help instead.',
        'If the library does not answer the question, set status "not_covered" and say '
        "briefly, in the reply language, that you do not have that yet and TGCG will add it.",
        'List in "cited" the number of every library item you used. Do not include links.',
    ]
    if silence:
        rules.append(
            "Campaign silence is in force: do not argue for any candidate or party; give "
            "only voting information."
        )
    library_text = "\n\n".join(_item_text(item) for item in items) or "(empty)"
    return "\n".join(rules) + "\n\nLIBRARY:\n\n" + library_text


NUMBER = re.compile(r"\d[\d,.]*")


def _numbers(text: str) -> set[str]:
    return {match.replace(",", "").rstrip(".") for match in NUMBER.findall(text)}


def check(result: dict, sent: dict[int, KnowledgeItem], question: str):
    """(status, answer, cited items, why it was held back or "")."""
    status = result.get("status")
    answer = re.sub(r"https?://\S+", "", str(result.get("answer") or "")).strip()
    if status not in ("answered", "not_covered", "refused") or not answer:
        return S.NOT_YET, "", [], "The AI's answer was incomplete."
    if len(answer) > 1500:
        return S.NOT_YET, "", [], "The AI's answer was too long."
    if status == "refused":
        return S.REFUSED, answer, [], ""
    if status == "not_covered":
        return S.NOT_YET, answer, [], ""
    cited_ids = []
    for value in result.get("cited") or []:
        try:
            cited_ids.append(int(value))
        except (TypeError, ValueError):
            return S.NOT_YET, "", [], "The AI cited something that is not a library item."
    if not cited_ids:
        return S.NOT_YET, "", [], "The AI cited no library item."
    if any(pk not in sent for pk in cited_ids):
        return S.NOT_YET, "", [], "The AI cited an item it was not given."
    cited = [sent[pk] for pk in dict.fromkeys(cited_ids)]
    allowed = _numbers(question)
    for item in cited:
        allowed |= _numbers(_item_text(item))
    unsupported = _numbers(answer) - allowed
    if unsupported:
        shown = ", ".join(sorted(unsupported)[:3])
        return S.NOT_YET, "", [], f"The AI used numbers not in the library: {shown}."
    return S.ANSWERED, answer, cited, ""


# --- asking ---------------------------------------------------------------------------


def _not_yet(config: AskMeSettings, language: str) -> str:
    return (config.not_yet_messages or {}).get(language or "en") or NOT_YET


def _ready_answer(normalized: str, language: str, silence: bool):
    reviewed = Question.objects.filter(normalized=normalized, review__in=REVIEWED).exclude(
        corrected_answer="", answer=""
    )
    if language:
        reviewed = reviewed.filter(language=language)
    if silence:
        reviewed = reviewed.exclude(items__topic=Topic.ACHIEVEMENT)
    return reviewed.prefetch_related("items").order_by("-reviewed_at").first()


def _training_ok(user) -> bool:
    preference = Preference.objects.filter(user=user).first()
    return bool(preference and preference.training_ok)


def asked_today(user) -> int:
    start = timezone.localtime().replace(hour=0, minute=0, second=0, microsecond=0)
    return (
        Question.objects.filter(asker=user, created_at__gte=start)
        .exclude(status__in=(S.LIMIT, S.OFF))
        .count()
    )


def ai_answers_this_month() -> int:
    start = timezone.localtime().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return Question.objects.filter(route=R.AI, created_at__gte=start).count()


def _held(user, config: AskMeSettings, reviewer: bool):
    """(status, message) when Ask Me is off or the member is over the day's
    limit, else None. Reviewers can always test."""
    if not config.enabled and not reviewer:
        return S.OFF, OFF
    if not reviewer and asked_today(user) >= config.daily_limit:
        return S.LIMIT, LIMIT
    return None


def ask(
    *,
    user,
    text: str,
    language: str = "",
    channel: str = Question.Channel.TEXT,
    recording: tuple[bytes, str] | None = None,
) -> Question:
    """Answers a question; `recording` (audio, extension) is the spoken
    question, kept when the asker allowed it."""
    _require(takes_part(user), "Only members with an active membership can ask.")
    text = (text or "").strip()
    if not text:
        raise ValidationError({"text": "Ask a question."})
    if language and language not in Language.values:
        raise ValidationError({"language": "Choose English, Hausa, Yoruba, Igbo or Pidgin."})
    started = time.monotonic()
    config = current_settings()
    reviewer = can_review(user)
    question = Question(
        asker=user,
        language=language,
        channel=channel,
        text=text[:1000],
        normalized=normalize(text),
        training_ok=_training_ok(user),
    )
    cited: list[KnowledgeItem] = []
    if recording is not None:
        audio, extension = recording
        question.recording.save(f"{uuid.uuid4().hex}{extension}", ContentFile(audio), save=False)

    held = _held(user, config, reviewer)
    if held:
        question.status, question.answer = held
    else:
        silence = in_silence(config)
        ready = _ready_answer(question.normalized, language, silence)
        if ready is not None:
            question.status, question.route = S.ANSWERED, R.READY
            question.answer = ready.shown_answer
            question.language = question.language or ready.language
            cited = list(ready.items.all())
        elif not gemini.configured():
            question.status, question.answer = S.NOT_YET, _not_yet(config, language)
            question.check_failed = "No AI key on the server: a reviewer answers it."
        elif not reviewer and ai_answers_this_month() >= config.monthly_ai_limit:
            question.status, question.answer = S.LIMIT, BUSY
        else:
            sent = select_items(text, library(silence=silence))
            question.route = R.AI
            try:
                result = gemini.generate_json(
                    system=system_prompt(config, language=language, silence=silence, items=sent),
                    text=text,
                    schema=ANSWER_SCHEMA,
                )
            except gemini.Blocked:
                question.status, question.answer = S.REFUSED, REFUSED
            except gemini.GeminiError as error:
                question.status, question.answer = S.ERROR, FAILED
                question.check_failed = str(error)[:200]
            else:
                status, answer, cited, why = check(result, {item.pk: item for item in sent}, text)
                question.status = status
                question.answer = answer or _not_yet(config, language)
                question.check_failed = why
                if result.get("language") in Language.values and not language:
                    question.language = result["language"]

    question.latency_ms = int((time.monotonic() - started) * 1000)
    with transaction.atomic():
        question.save()
        if cited:
            question.items.set(cited)
    return question


def rate(*, user, question: Question, rating: int, reason: str = "") -> Question:
    if question.asker_id != user.pk:
        raise PermissionDenied("You can rate only your own questions.")
    if rating not in (1, -1):
        raise ValidationError({"rating": "Rate 1 (helpful) or -1 (not helpful)."})
    question.rating = rating
    question.flag_reason = reason.strip()[:300] if rating == -1 else ""
    question.save(update_fields=["rating", "flag_reason", "updated_at"])
    return question


def set_consent(*, user, **choices) -> Preference:
    """`training_ok` (questions may train TGCG's model) and `voice_ok` (spoken
    questions may be kept to train its listening AI), either or both."""
    fields = {name: value for name, value in choices.items() if name in ("training_ok", "voice_ok")}
    if not fields:
        raise ValidationError({"training_ok": "Say yes or no."})
    preference, _ = Preference.objects.update_or_create(user=user, defaults=fields)
    return preference


# --- voice ----------------------------------------------------------------------------

# Recordings Ask Me accepts, by file extension.
VOICE_TYPES = {
    ".wav": "audio/wav",
    ".m4a": "audio/mp4",
    ".mp4": "audio/mp4",
    ".aac": "audio/aac",
    ".mp3": "audio/mpeg",
    ".ogg": "audio/ogg",
    ".webm": "audio/webm",
}
SPOKEN = "(spoken question)"


class VoiceUnavailable(Exception):
    """Voice is switched off, or the voice service is not set up."""


def voice_ready(config: AskMeSettings | None = None) -> bool:
    config = config or current_settings()
    return config.voice_enabled and spitch.configured()


def voice_for(language: str, config: AskMeSettings | None = None) -> str:
    config = config or current_settings()
    chosen = (config.voices or {}).get(language)
    known = {voice for voices in spitch.VOICES.values() for voice in voices}
    return chosen if chosen in known else spitch.DEFAULT_VOICES.get(language, "jude")


def ask_by_voice(*, user, audio, language: str) -> Question:
    """A spoken question: the voice service writes it down, then it is
    answered like a typed one. The recording is kept only if the asker
    allowed it."""
    _require(takes_part(user), "Only members with an active membership can ask.")
    if language not in Language.values:
        raise ValidationError({"language": "Choose the language you speak."})
    extension = Path(getattr(audio, "name", "") or "").suffix.lower()
    if extension not in VOICE_TYPES:
        raise ValidationError({"audio": "Record the question again."})
    if audio.size > settings.ASKME_VOICE_MAX_BYTES:
        raise ValidationError({"audio": "Keep the question under a minute."})
    config = current_settings()
    held = _held(user, config, can_review(user))
    if held:  # nothing is sent to the voice service
        question = Question(
            asker=user,
            language=language,
            channel=Question.Channel.VOICE,
            text=SPOKEN,
            normalized="",
            status=held[0],
            answer=held[1],
            training_ok=_training_ok(user),
        )
        question.save()
        return question
    if not voice_ready(config):
        raise VoiceUnavailable
    data = audio.read()
    heard = spitch.transcribe(
        data, filename=f"question{extension}", mime_type=VOICE_TYPES[extension], language=language
    )
    if not heard:
        raise ValidationError(
            {"audio": "Nothing was heard. Speak closer to the phone and try again."}
        )
    preference = Preference.objects.filter(user=user).first()
    keep = bool(preference and preference.voice_ok)
    return ask(
        user=user,
        text=heard,
        language=language,
        channel=Question.Channel.VOICE,
        recording=(data, extension) if keep else None,
    )


def spoken_answer(question: Question) -> SpokenAnswer:
    """The question's answer read aloud, made once for the same words and voice."""
    config = current_settings()
    if not voice_ready(config):
        raise VoiceUnavailable
    text = question.shown_answer.strip()
    if not text:
        raise ValidationError({"answer": "There is nothing to read aloud."})
    language = question.language if question.language in Language.values else "en"
    voice = voice_for(language, config)
    key = hashlib.sha256(f"{language}|{voice}|{text}".encode()).hexdigest()
    found = SpokenAnswer.objects.filter(key=key).first()
    if found is not None:
        return found
    audio = spitch.speak(text, language=language, voice=voice)
    spoken = SpokenAnswer(key=key, language=language, voice=voice, size=len(audio))
    spoken.audio.save(f"{key}.mp3", ContentFile(audio), save=False)
    try:
        with transaction.atomic():
            spoken.save()
    except IntegrityError:  # made meanwhile by another request
        spoken.audio.delete(save=False)
        return SpokenAnswer.objects.get(key=key)
    return spoken


# --- the library ---------------------------------------------------------------------


def add_site(*, actor, domain: str, name: str) -> TrustedSite:
    _require(can_approve(actor), "Only a National Administrator adds trusted websites.")
    domain = domain.strip().lower().removeprefix("https://").removeprefix("www.").strip("/")
    if not re.fullmatch(r"[a-z0-9-]+(\.[a-z0-9-]+)+", domain):
        raise ValidationError({"domain": "Give the site's address, like statehouse.gov.ng."})
    site, created = TrustedSite.objects.get_or_create(
        domain=domain, defaults={"name": name.strip()[:150] or domain, "added_by": actor}
    )
    if not created and not site.active:
        site.active = True
        site.save(update_fields=["active", "updated_at"])
    _audit(actor, "askme.siteTrusted", "askmeSite", site.uuid, domain)
    return site


def set_site_active(*, actor, site: TrustedSite, active: bool) -> TrustedSite:
    _require(can_approve(actor), "Only a National Administrator changes trusted websites.")
    site.active = active
    site.save(update_fields=["active", "updated_at"])
    _audit(actor, "askme.siteChanged", "askmeSite", site.uuid, site.domain, active=active)
    return site


def import_page(*, actor, url: str) -> SourcePage:
    _require(can_review(actor), "Only Ask Me reviewers import pages.")
    url = url.strip()
    sites = list(TrustedSite.objects.filter(active=True))
    site = pages.trusted_site(url, sites)
    read = pages.read(url, sites)
    now = timezone.now()
    page, created = SourcePage.objects.get_or_create(
        url=url, defaults={"site": site, "added_by": actor}
    )
    changed = not created and page.content_hash and page.content_hash != read["hash"]
    page.site = site
    page.title = read["title"]
    page.text = read["text"]
    page.content_hash = read["hash"]
    page.status, page.error = SourcePage.Status.OK, ""
    page.fetched_at = page.checked_at = now
    if changed:
        page.changed_at = now
    page.save()
    if changed:
        _items_need_review(page)
    _audit(actor, "askme.pageImported", "askmePage", page.uuid, url)
    return page


def _items_need_review(page: SourcePage):
    note = f"Its source page changed on {timezone.localdate():%d %B %Y}."
    page.items.exclude(status=KnowledgeItem.Status.RETIRED).update(
        review_note=note, updated_at=timezone.now()
    )


def recheck_page(page: SourcePage) -> str:
    """Re-reads a page: "same", "changed" or "failed" (items are marked when it changed)."""
    sites = list(TrustedSite.objects.filter(active=True))
    now = timezone.now()
    try:
        read = pages.read(page.url, sites)
    except ValidationError as error:
        page.status = SourcePage.Status.FAILED
        page.error = "; ".join(error.messages)[:300]
        page.checked_at = now
        page.save(update_fields=["status", "error", "checked_at", "updated_at"])
        return "failed"
    changed = read["hash"] != page.content_hash
    page.status, page.error = SourcePage.Status.OK, ""
    page.checked_at = page.fetched_at = now
    if changed:
        page.title, page.text, page.content_hash, page.changed_at = (
            read["title"],
            read["text"],
            read["hash"],
            now,
        )
    page.save()
    if changed:
        _items_need_review(page)
    return "changed" if changed else "same"


SUGGEST_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "items": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "title": {"type": "STRING"},
                    "topic": {"type": "STRING", "enum": list(Topic.values)},
                    "fact": {"type": "STRING"},
                    "argument": {"type": "STRING"},
                    "fact_date": {"type": "STRING"},
                    "keywords": {"type": "STRING"},
                },
                "required": ["title", "topic", "fact"],
            },
        }
    },
    "required": ["items"],
}


def suggest_items(*, actor, page: SourcePage) -> list[KnowledgeItem]:
    """Gemini proposes up to eight draft items from a page; a suggestion with a
    number not on the page is dropped. Nothing is used until approved."""
    _require(can_review(actor), "Only Ask Me reviewers draft library items.")
    if not page.text:
        raise ValidationError({"page": "This page has no words to work from."})
    if not gemini.configured():
        raise gemini.NotConfigured
    result = gemini.generate_json(
        system=(
            "You help TGCG, a group supporting President Tinubu's government, build a "
            "library of facts. From the PAGE only, list up to 8 clear facts about the "
            "government's work or about voting. Each fact must be stated on the page; copy "
            "numbers and dates exactly; never add anything that is not on the page. For each, "
            "give a short title, the topic, the fact in plain English, a one-sentence argument "
            "for why it matters, the date it refers to as YYYY-MM-DD if the page gives one "
            "(else empty), and keywords in English, Hausa, Yoruba, Igbo and Pidgin."
        ),
        text=f"PAGE ({page.url}):\n\n{page.text[:30000]}",
        schema=SUGGEST_SCHEMA,
        temperature=0.2,
    )
    on_page = _numbers(page.text)
    made = []
    for proposal in (result.get("items") or [])[:8]:
        fact = str(proposal.get("fact") or "").strip()[:2000]
        title = str(proposal.get("title") or "").strip()[:200]
        if not fact or not title or _numbers(fact) - on_page:
            continue
        topic = (
            proposal.get("topic") if proposal.get("topic") in Topic.values else Topic.ACHIEVEMENT
        )
        try:
            when = date.fromisoformat(str(proposal.get("fact_date") or "")) or None
        except ValueError:
            when = None
        made.append(
            KnowledgeItem.objects.create(
                topic=topic,
                title=title,
                fact=fact,
                argument=str(proposal.get("argument") or "").strip()[:2000],
                keywords=str(proposal.get("keywords") or "").strip()[:500],
                source_page=page,
                source_url=page.url,
                fact_date=when,
                created_by=actor,
            )
        )
    _audit(actor, "askme.itemsSuggested", "askmePage", page.uuid, page.url, drafts=len(made))
    return made


ITEM_FIELDS = (
    "topic",
    "title",
    "fact",
    "argument",
    "objections",
    "keywords",
    "source_url",
    "fact_date",
)


def save_item(*, actor, item: KnowledgeItem | None = None, **fields) -> KnowledgeItem:
    """Creates or edits an item. Any edit sends it back for approval."""
    _require(can_review(actor), "Only Ask Me reviewers write library items.")
    page = fields.pop("source_page", None)
    item = item or KnowledgeItem(created_by=actor)
    if page is not None:
        item.source_page = page
        fields.setdefault("source_url", page.url)
    for name in ITEM_FIELDS:
        if name in fields:
            setattr(item, name, fields[name])
    if item.source_url:
        pages.trusted_site(item.source_url, list(TrustedSite.objects.filter(active=True)))
    item.status = KnowledgeItem.Status.DRAFT
    item.approved_by, item.approved_at, item.review_note = None, None, ""
    item.full_clean()
    item.save()
    _audit(actor, "askme.itemSaved", "askmeItem", item.uuid, item.title)
    return item


def approve_item(*, actor, item: KnowledgeItem) -> KnowledgeItem:
    _require(can_approve(actor), "Only a National Administrator approves library items.")
    if item.status == KnowledgeItem.Status.RETIRED:
        raise ValidationError({"status": "A retired item cannot be approved; edit it first."})
    item.status = KnowledgeItem.Status.APPROVED
    item.approved_by, item.approved_at, item.review_note = actor, timezone.now(), ""
    item.save(update_fields=["status", "approved_by", "approved_at", "review_note", "updated_at"])
    _audit(actor, "askme.itemApproved", "askmeItem", item.uuid, item.title)
    return item


def retire_item(*, actor, item: KnowledgeItem, reason: str = "") -> KnowledgeItem:
    _require(can_approve(actor), "Only a National Administrator retires library items.")
    item.status = KnowledgeItem.Status.RETIRED
    item.save(update_fields=["status", "updated_at"])
    _audit(actor, "askme.itemRetired", "askmeItem", item.uuid, item.title, reason=reason)
    return item


# --- review and settings ---------------------------------------------------------------


def review(
    *,
    actor,
    question: Question,
    decision: str,
    corrected_answer: str = "",
    corrected_text: str = "",
) -> Question:
    """approve (the answer stands), correct (the reviewer's words replace it) or
    reject. Approved and corrected answers are reused for the same question.
    `corrected_text`: what a spoken question really said, where the voice
    service misheard it (for training the listening AI)."""
    _require(can_review(actor), "Only Ask Me reviewers review answers.")
    corrected_answer = corrected_answer.strip()
    if corrected_text.strip():
        question.corrected_text = corrected_text.strip()[:1000]
    if decision == "approve":
        if not question.answer or question.status not in (S.ANSWERED, S.REFUSED):
            raise ValidationError({"decision": "Write the answer it should have given instead."})
        question.review = Question.Review.APPROVED
    elif decision == "correct":
        if not corrected_answer:
            raise ValidationError({"corrected_answer": "Write the right answer."})
        question.review = Question.Review.CORRECTED
        question.corrected_answer = corrected_answer[:1500]
    elif decision == "reject":
        question.review = Question.Review.REJECTED
    else:
        raise ValidationError({"decision": "approve, correct or reject."})
    question.reviewed_by, question.reviewed_at = actor, timezone.now()
    question.save(
        update_fields=[
            "review",
            "corrected_answer",
            "corrected_text",
            "reviewed_by",
            "reviewed_at",
            "updated_at",
        ]
    )
    action = {"approve": "Approved", "correct": "Corrected", "reject": "Rejected"}[decision]
    _audit(actor, f"askme.answer{action}", "askmeQuestion", question.uuid)
    return question


def correct_heard(*, actor, question: Question, text: str) -> Question:
    """A reviewer writes what a spoken question really said, where the voice
    service misheard it, for training TGCG's listening AI."""
    _require(can_review(actor), "Only Ask Me reviewers correct what was heard.")
    if question.channel != Question.Channel.VOICE:
        raise ValidationError({"text": "Only spoken questions are heard."})
    text = (text or "").strip()
    if not text:
        raise ValidationError({"text": "Write what was really said."})
    question.corrected_text = text[:1000]
    question.save(update_fields=["corrected_text", "updated_at"])
    _audit(actor, "askme.heardCorrected", "askmeQuestion", question.uuid)
    return question


SETTING_FIELDS = (
    "enabled",
    "tone",
    "daily_limit",
    "monthly_ai_limit",
    "silence_starts_at",
    "silence_ends_at",
    "not_yet_messages",
    "voice_enabled",
    "voices",
)


def update_settings(*, actor, **fields) -> AskMeSettings:
    _require(can_approve(actor), "Only a National Administrator changes Ask Me's settings.")
    config = current_settings()
    before = {name: str(getattr(config, name)) for name in SETTING_FIELDS if name in fields}
    for name in SETTING_FIELDS:
        if name in fields:
            setattr(config, name, fields[name])
    starts, ends = config.silence_starts_at, config.silence_ends_at
    if bool(starts) != bool(ends) or (starts and ends and ends <= starts):
        raise ValidationError({"silence_ends_at": "Give a start and an end after it."})
    messages = config.not_yet_messages or {}
    if set(messages) - set(Language.values):
        raise ValidationError({"not_yet_messages": "Use the language codes en, ha, yo, ig, pcm."})
    voices = config.voices or {}
    known = {voice for names in spitch.VOICES.values() for voice in names}
    if set(voices) - set(Language.values) or set(voices.values()) - known:
        raise ValidationError(
            {"voices": "Choose one of the voice service's voices for each language."}
        )
    config.updated_by = actor
    config.save()
    after = {name: str(getattr(config, name)) for name in before}
    _audit(actor, "askme.settingsChanged", "askmeSettings", 1, "Ask Me", before=before, after=after)
    return config


def review_queue(kind: str = ""):
    """Questions for reviewers: "flagged" (rated unhelpful), "not_yet", "held"
    (answers the server held back) or, by default, everything unreviewed."""
    questions = Question.objects.exclude(status__in=(S.OFF, S.LIMIT))
    if kind == "flagged":
        questions = questions.filter(rating=-1)
    elif kind == "not_yet":
        questions = questions.filter(status=S.NOT_YET)
    elif kind == "held":
        questions = questions.exclude(check_failed="")
    elif kind == "reviewed":
        return questions.filter(review__in=REVIEWED)
    return questions.filter(review=Question.Review.UNREVIEWED).filter(
        Q(route=R.AI) | Q(route=R.NONE)
    )

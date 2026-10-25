from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from .models import AskMeSettings, KnowledgeItem, Language, Question, SourcePage, Topic, TrustedSite


class SourceSerializer(serializers.Serializer):
    """Where an answer's facts come from, for the member to open."""

    title = serializers.CharField()
    url = serializers.CharField()
    date = serializers.DateField(allow_null=True)


def _sources(question) -> list[dict]:
    seen, sources = set(), []
    for item in question.items.all():
        if item.source_url and item.source_url not in seen:
            seen.add(item.source_url)
            sources.append({"title": item.title, "url": item.source_url, "date": item.fact_date})
    return sources


class AnswerSerializer(serializers.ModelSerializer):
    """A question as its asker sees it."""

    id = serializers.UUIDField(source="uuid", read_only=True)
    answer = serializers.CharField(source="shown_answer")
    sources = serializers.SerializerMethodField()

    class Meta:
        model = Question
        fields = ["id", "text", "language", "status", "answer", "sources", "rating", "created_at"]
        read_only_fields = fields

    @extend_schema_field(SourceSerializer(many=True))
    def get_sources(self, question):
        return SourceSerializer(_sources(question), many=True).data


class AskSerializer(serializers.Serializer):
    text = serializers.CharField(max_length=1000)
    language = serializers.ChoiceField(
        choices=[("", "Detect it"), *Language.choices], required=False, allow_blank=True, default=""
    )


class RateSerializer(serializers.Serializer):
    rating = serializers.ChoiceField(choices=[1, -1])
    reason = serializers.CharField(max_length=300, required=False, allow_blank=True, default="")


class MeSerializer(serializers.Serializer):
    can_ask = serializers.BooleanField()
    enabled = serializers.BooleanField(help_text="Open to members (reviewers can always test).")
    training_ok = serializers.BooleanField(
        allow_null=True, help_text="Null until the person has decided."
    )
    asked_today = serializers.IntegerField()
    daily_limit = serializers.IntegerField()
    can_review = serializers.BooleanField()
    can_approve = serializers.BooleanField()
    ai_ready = serializers.BooleanField(help_text="A Gemini key is set on the server.")


class ConsentSerializer(serializers.Serializer):
    training_ok = serializers.BooleanField()


class TrustedSiteSerializer(serializers.ModelSerializer):
    id = serializers.UUIDField(source="uuid", read_only=True)
    pages = serializers.IntegerField(source="pages.count", read_only=True)

    class Meta:
        model = TrustedSite
        fields = ["id", "domain", "name", "active", "pages", "created_at"]
        read_only_fields = ["id", "pages", "created_at"]


class TrustedSiteInputSerializer(serializers.Serializer):
    domain = serializers.CharField(max_length=120, help_text="e.g. statehouse.gov.ng")
    name = serializers.CharField(max_length=150, required=False, allow_blank=True, default="")


class SourcePageSerializer(serializers.ModelSerializer):
    id = serializers.UUIDField(source="uuid", read_only=True)
    site = serializers.CharField(source="site.domain", read_only=True)
    items = serializers.IntegerField(source="items.count", read_only=True)

    class Meta:
        model = SourcePage
        fields = [
            "id",
            "url",
            "site",
            "title",
            "status",
            "error",
            "fetched_at",
            "checked_at",
            "changed_at",
            "items",
        ]
        read_only_fields = fields


class SourcePageDetailSerializer(SourcePageSerializer):
    class Meta(SourcePageSerializer.Meta):
        fields = [*SourcePageSerializer.Meta.fields, "text"]
        read_only_fields = fields


class ImportPageSerializer(serializers.Serializer):
    url = serializers.URLField(max_length=500)


class KnowledgeItemSerializer(serializers.ModelSerializer):
    id = serializers.UUIDField(source="uuid", read_only=True)
    source_page = serializers.SlugRelatedField(
        slug_field="uuid",
        queryset=SourcePage.objects.all(),
        required=False,
        allow_null=True,
    )
    approved_by = serializers.CharField(
        source="approved_by.full_name", default=None, read_only=True
    )

    class Meta:
        model = KnowledgeItem
        fields = [
            "id",
            "topic",
            "title",
            "fact",
            "argument",
            "objections",
            "keywords",
            "source_page",
            "source_url",
            "fact_date",
            "status",
            "review_note",
            "approved_by",
            "approved_at",
            "updated_at",
        ]
        read_only_fields = [
            "id",
            "status",
            "review_note",
            "approved_by",
            "approved_at",
            "updated_at",
        ]
        extra_kwargs = {"topic": {"choices": Topic.choices}}


class ReasonSerializer(serializers.Serializer):
    reason = serializers.CharField(max_length=300, required=False, allow_blank=True, default="")


class ReviewQuestionSerializer(serializers.ModelSerializer):
    """A question for reviewers: no asker's name."""

    id = serializers.UUIDField(source="uuid", read_only=True)
    sources = serializers.SerializerMethodField()
    items = serializers.SerializerMethodField()

    class Meta:
        model = Question
        fields = [
            "id",
            "text",
            "language",
            "status",
            "route",
            "answer",
            "corrected_answer",
            "sources",
            "items",
            "check_failed",
            "rating",
            "flag_reason",
            "review",
            "reviewed_at",
            "training_ok",
            "latency_ms",
            "created_at",
        ]
        read_only_fields = fields

    @extend_schema_field(SourceSerializer(many=True))
    def get_sources(self, question):
        return SourceSerializer(_sources(question), many=True).data

    def get_items(self, question) -> list[str]:
        return [str(item.uuid) for item in question.items.all()]


class ReviewDecisionSerializer(serializers.Serializer):
    decision = serializers.ChoiceField(choices=["approve", "correct", "reject"])
    corrected_answer = serializers.CharField(
        max_length=1500, required=False, allow_blank=True, default=""
    )


class AskMeSettingsSerializer(serializers.ModelSerializer):
    in_silence = serializers.SerializerMethodField()
    ai_answers_this_month = serializers.SerializerMethodField()

    class Meta:
        model = AskMeSettings
        fields = [
            "enabled",
            "tone",
            "daily_limit",
            "monthly_ai_limit",
            "silence_starts_at",
            "silence_ends_at",
            "not_yet_messages",
            "in_silence",
            "ai_answers_this_month",
            "updated_at",
        ]
        read_only_fields = ["in_silence", "ai_answers_this_month", "updated_at"]

    def get_in_silence(self, config) -> bool:
        from .services import in_silence

        return in_silence(config)

    def get_ai_answers_this_month(self, config) -> int:
        from .services import ai_answers_this_month

        return ai_answers_this_month()

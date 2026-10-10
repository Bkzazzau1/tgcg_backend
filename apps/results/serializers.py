from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from apps.core.choices import RecordOrigin, SubmissionSource
from apps.field.serializers import EvidenceSerializer
from apps.geography.models import PollingUnit
from apps.geography.scope import ScopeField

from .models import Election, Party, ResultEvent, ResultSubmission


class PartySerializer(serializers.ModelSerializer):
    class Meta:
        model = Party
        fields = ["acronym", "name"]


class ElectionSerializer(serializers.ModelSerializer):
    scope = ScopeField(read_only=True)
    parties = PartySerializer(many=True, read_only=True)

    class Meta:
        model = Election
        fields = ["code", "name", "type", "election_date", "status", "scope", "parties"]


def party_votes_input(**kwargs):
    """`{"APC": 120, "PDP": 98}` — the client's partyVotes map."""
    return serializers.DictField(child=serializers.IntegerField(min_value=0), **kwargs)


@extend_schema_field(
    {"type": "object", "additionalProperties": {"type": "integer"}, "example": {"APC": 120}}
)
class PartyVotesField(serializers.Field):
    """Read-only partyVotes map for a submission."""

    def __init__(self, **kwargs):
        kwargs["read_only"] = True
        super().__init__(**kwargs)

    def get_attribute(self, instance):
        return instance

    def to_representation(self, submission):
        return {row.party.acronym: row.votes for row in submission.party_votes.all()}


class ValidationSummarySerializer(serializers.ModelSerializer):
    requires_human_review = serializers.BooleanField(read_only=True)
    notes = serializers.ListField(source="validation_notes", read_only=True)

    class Meta:
        model = ResultSubmission
        fields = [
            "arithmetic_valid",
            "duplicate_suspected",
            "polling_unit_matched",
            "agent_scope_matched",
            "ocr_confidence",
            "ocr_matched_manual_entry",
            "notes",
            "requires_human_review",
        ]


class ResultSubmissionSerializer(serializers.ModelSerializer):
    id = serializers.UUIDField(source="uuid", read_only=True)
    election = serializers.SlugRelatedField(slug_field="code", read_only=True)
    scope = ScopeField(read_only=True)
    party_votes = PartyVotesField()
    calculated_party_votes = serializers.SerializerMethodField()
    validation = ValidationSummarySerializer(source="*", read_only=True)
    submitted_by = serializers.SlugRelatedField(slug_field="access_id", read_only=True)
    verified_by = serializers.SlugRelatedField(slug_field="access_id", read_only=True)
    result_form = serializers.SerializerMethodField()
    official = serializers.SerializerMethodField()

    class Meta:
        model = ResultSubmission
        fields = [
            "id",
            "reference",
            "election",
            "status",
            "scope",
            "party_votes",
            "calculated_party_votes",
            "total_valid_votes",
            "accredited_voters",
            "rejected_votes",
            "registered_voters",
            "validation",
            "source",
            "source_reference",
            "origin",
            "client_id",
            "submitted_by",
            "submitted_at",
            "verified_by",
            "verified_at",
            "review_reason",
            "result_form",
            "official",
            "created_at",
        ]
        read_only_fields = fields

    def get_calculated_party_votes(self, submission) -> int:
        return sum(row.votes for row in submission.party_votes.all())

    def get_result_form(self, submission) -> dict | None:
        forms = [e for e in submission.evidence.all() if e.type == "resultForm"]
        if not forms:
            return None
        return EvidenceSerializer(forms[-1], context=self.context).data

    def get_official(self, submission) -> bool:
        return False


class ResultSubmitSerializer(serializers.Serializer):
    election = serializers.SlugRelatedField(
        slug_field="code", queryset=Election.objects.prefetch_related("parties")
    )
    polling_unit = serializers.SlugRelatedField(
        slug_field="code", queryset=PollingUnit.objects.select_related("ward__lga__state__zone")
    )
    party_votes = party_votes_input()
    total_valid_votes = serializers.IntegerField(min_value=0)
    accredited_voters = serializers.IntegerField(min_value=0)
    rejected_votes = serializers.IntegerField(min_value=0, required=False, allow_null=True)
    registered_voters = serializers.IntegerField(min_value=0, required=False, allow_null=True)
    source = serializers.ChoiceField(choices=SubmissionSource.choices, default=SubmissionSource.APP)
    source_reference = serializers.CharField(required=False, allow_blank=True, max_length=120)
    submitted_at = serializers.DateTimeField(required=False)
    origin = serializers.ChoiceField(choices=RecordOrigin.choices, required=False)
    client_id = serializers.CharField(required=False, allow_null=True, max_length=64)
    ocr_party_votes = party_votes_input(required=False, allow_null=True)
    ocr_confidence = serializers.FloatField(
        required=False, allow_null=True, min_value=0, max_value=1
    )


class ReviewSerializer(serializers.Serializer):
    note = serializers.CharField(required=False, allow_blank=True, max_length=2000)


class ReasonSerializer(serializers.Serializer):
    reason = serializers.CharField(max_length=2000)


class ResultEventSerializer(serializers.ModelSerializer):
    actor = serializers.SlugRelatedField(slug_field="access_id", read_only=True)

    class Meta:
        model = ResultEvent
        fields = ["from_status", "to_status", "note", "actor", "created_at"]

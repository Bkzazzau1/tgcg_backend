from django.db.models import Prefetch
from django.http import FileResponse
from django.shortcuts import get_object_or_404
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.permissions import BasePermission
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.forum.services import takes_part

from . import gemini, services, spitch
from .models import KnowledgeItem, Preference, Question, SourcePage, TrustedSite
from .serializers import (
    AnswerSerializer,
    AskMeSettingsSerializer,
    AskSerializer,
    ConsentSerializer,
    ImportPageSerializer,
    KnowledgeItemSerializer,
    MeSerializer,
    RateSerializer,
    ReasonSerializer,
    ReviewDecisionSerializer,
    ReviewQuestionSerializer,
    SourcePageDetailSerializer,
    SourcePageSerializer,
    TrustedSiteInputSerializer,
    TrustedSiteSerializer,
    VoiceAskSerializer,
)


class TakesPart(BasePermission):
    message = "Only members with an active membership can use Ask Me."

    def has_permission(self, request, view):
        return takes_part(request.user)


class IsReviewer(BasePermission):
    message = "Only Ask Me reviewers can do this."

    def has_permission(self, request, view):
        return services.can_review(request.user)


def _with_items(questions):
    return questions.prefetch_related(
        Prefetch("items", queryset=KnowledgeItem.objects.order_by("pk"))
    )


def _ai_unavailable(error):
    if isinstance(error, gemini.NotConfigured):
        return Response(
            {"detail": "The AI is not set up on the server yet."},
            status=status.HTTP_503_SERVICE_UNAVAILABLE,
        )
    return Response({"detail": str(error)}, status=status.HTTP_502_BAD_GATEWAY)


def _voice_unavailable(error):
    if isinstance(error, (services.VoiceUnavailable, spitch.NotConfigured)):
        return Response(
            {"detail": "Voice is not available yet."},
            status=status.HTTP_503_SERVICE_UNAVAILABLE,
        )
    return Response({"detail": str(error)}, status=status.HTTP_502_BAD_GATEWAY)


def _audio(file_field, content_type):
    response = FileResponse(file_field.open("rb"), content_type=content_type)
    response["Cache-Control"] = "private, no-store"
    return response


class AskView(APIView):
    """Ask a question; the answer comes only from TGCG's approved library.
    `language` (en, ha, yo, ig, pcm) or blank to answer in the question's own."""

    permission_classes = [TakesPart]

    @extend_schema(request=AskSerializer, responses={201: AnswerSerializer})
    def post(self, request):
        data = AskSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        question = services.ask(user=request.user, **data.validated_data)
        question = _with_items(Question.objects).get(pk=question.pk)
        return Response(AnswerSerializer(question).data, status=status.HTTP_201_CREATED)


class VoiceAskView(APIView):
    """Ask by voice (multipart `audio`, `language`): the voice service writes
    the question down, and it is answered like a typed one. The recording is
    kept only if the asker allowed it (`voice_ok`)."""

    permission_classes = [TakesPart]
    parser_classes = [MultiPartParser, FormParser]

    @extend_schema(
        request={"multipart/form-data": VoiceAskSerializer}, responses={201: AnswerSerializer}
    )
    def post(self, request):
        data = VoiceAskSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        try:
            question = services.ask_by_voice(user=request.user, **data.validated_data)
        except (services.VoiceUnavailable, spitch.NotConfigured, spitch.SpitchError) as error:
            return _voice_unavailable(error)
        question = _with_items(Question.objects).get(pk=question.pk)
        return Response(AnswerSerializer(question).data, status=status.HTTP_201_CREATED)


class MeView(APIView):
    """What this account may do in Ask Me, and its training consent (PATCH)."""

    permission_classes = [TakesPart]

    def _me(self, request):
        config = services.current_settings()
        preference = Preference.objects.filter(user=request.user).first()
        reviewer = services.can_review(request.user)
        return Response(
            {
                "can_ask": config.enabled or reviewer,
                "enabled": config.enabled,
                "training_ok": preference.training_ok if preference else None,
                "asked_today": services.asked_today(request.user),
                "daily_limit": config.daily_limit,
                "can_review": reviewer,
                "can_approve": services.can_approve(request.user),
                "ai_ready": gemini.configured(),
                "voice_ready": services.voice_ready(config),
                "voice_ok": preference.voice_ok if preference else None,
            }
        )

    @extend_schema(responses=MeSerializer)
    def get(self, request):
        return self._me(request)

    @extend_schema(request=ConsentSerializer, responses=MeSerializer)
    def patch(self, request):
        data = ConsentSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        services.set_consent(user=request.user, **data.validated_data)
        return self._me(request)


class MyQuestionViewSet(mixins.ListModelMixin, viewsets.GenericViewSet):
    """The account's own questions and answers, newest first; `.../rate/`."""

    permission_classes = [TakesPart]
    serializer_class = AnswerSerializer
    lookup_field = "uuid"

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return Question.objects.none()
        return _with_items(Question.objects.filter(asker=self.request.user))

    @extend_schema(request=RateSerializer, responses=AnswerSerializer)
    @action(detail=True, methods=["post"])
    def rate(self, request, uuid=None):
        data = RateSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        question = services.rate(
            user=request.user, question=self.get_object(), **data.validated_data
        )
        return Response(AnswerSerializer(question).data)

    @extend_schema(responses={(200, "audio/mpeg"): bytes})
    @action(detail=True, methods=["get"])
    def audio(self, request, uuid=None):
        """The answer read aloud (MP3), made once and kept for the same words."""
        try:
            spoken = services.spoken_answer(self.get_object())
        except (services.VoiceUnavailable, spitch.NotConfigured, spitch.SpitchError) as error:
            return _voice_unavailable(error)
        return _audio(spoken.audio, "audio/mpeg")


class TrustedSiteViewSet(
    mixins.ListModelMixin,
    mixins.CreateModelMixin,
    mixins.UpdateModelMixin,
    viewsets.GenericViewSet,
):
    """Official websites pages may be imported from. A National Administrator
    adds them and switches them off (`active`)."""

    permission_classes = [IsReviewer]
    serializer_class = TrustedSiteSerializer
    lookup_field = "uuid"
    http_method_names = ["get", "post", "patch", "head", "options"]
    queryset = TrustedSite.objects.all()

    @extend_schema(request=TrustedSiteInputSerializer, responses={201: TrustedSiteSerializer})
    def create(self, request):
        data = TrustedSiteInputSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        site = services.add_site(
            actor=request.user,
            domain=data.validated_data["domain"],
            name=data.validated_data.get("name", ""),
        )
        return Response(TrustedSiteSerializer(site).data, status=status.HTTP_201_CREATED)

    def partial_update(self, request, uuid=None):
        active = request.data.get("active")
        if not isinstance(active, bool):
            return Response({"active": ["true or false."]}, status=status.HTTP_400_BAD_REQUEST)
        site = services.set_site_active(actor=request.user, site=self.get_object(), active=active)
        return Response(TrustedSiteSerializer(site).data)


class SourcePageViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    viewsets.GenericViewSet,
):
    """Copies of official pages. POST `{"url"}` reads one from a trusted site;
    `.../recheck/` reads it again; `.../suggest/` has the AI draft items
    from it (drafts wait for approval)."""

    permission_classes = [IsReviewer]
    lookup_field = "uuid"
    search_fields = ["title", "url"]

    def get_queryset(self):
        return SourcePage.objects.select_related("site")

    def get_serializer_class(self):
        return SourcePageDetailSerializer if self.action == "retrieve" else SourcePageSerializer

    @extend_schema(request=ImportPageSerializer, responses={201: SourcePageDetailSerializer})
    def create(self, request):
        data = ImportPageSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        page = services.import_page(actor=request.user, url=data.validated_data["url"])
        return Response(SourcePageDetailSerializer(page).data, status=status.HTTP_201_CREATED)

    @extend_schema(request=None, responses=SourcePageDetailSerializer)
    @action(detail=True, methods=["post"])
    def recheck(self, request, uuid=None):
        page = self.get_object()
        services.recheck_page(page)
        page.refresh_from_db()
        return Response(SourcePageDetailSerializer(page).data)

    @extend_schema(request=None, responses=KnowledgeItemSerializer(many=True))
    @action(detail=True, methods=["post"])
    def suggest(self, request, uuid=None):
        try:
            made = services.suggest_items(actor=request.user, page=self.get_object())
        except (gemini.NotConfigured, gemini.GeminiError, gemini.Blocked) as error:
            return _ai_unavailable(error)
        return Response(
            KnowledgeItemSerializer(made, many=True).data, status=status.HTTP_201_CREATED
        )


class KnowledgeItemViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    mixins.UpdateModelMixin,
    viewsets.GenericViewSet,
):
    """The library. Reviewers write and edit items (an edit sends an item back
    for approval); a National Administrator approves or retires them.
    `?status=draft|approved|retired`, `?topic=`, `?needs_review=true`."""

    permission_classes = [IsReviewer]
    serializer_class = KnowledgeItemSerializer
    lookup_field = "uuid"
    http_method_names = ["get", "post", "patch", "head", "options"]
    filterset_fields = {"status": ["exact"], "topic": ["exact"]}
    search_fields = ["title", "fact", "keywords"]

    def get_queryset(self):
        items = KnowledgeItem.objects.select_related("approved_by", "source_page")
        if self.request.query_params.get("needs_review") == "true":
            items = items.exclude(review_note="")
        return items

    def _save(self, request, item=None, code=status.HTTP_200_OK):
        data = KnowledgeItemSerializer(item, data=request.data, partial=item is not None)
        data.is_valid(raise_exception=True)
        saved = services.save_item(actor=request.user, item=item, **data.validated_data)
        return Response(KnowledgeItemSerializer(saved).data, status=code)

    def create(self, request):
        return self._save(request, code=status.HTTP_201_CREATED)

    def partial_update(self, request, uuid=None):
        return self._save(request, self.get_object())

    @extend_schema(request=None, responses=KnowledgeItemSerializer)
    @action(detail=True, methods=["post"])
    def approve(self, request, uuid=None):
        item = services.approve_item(actor=request.user, item=self.get_object())
        return Response(KnowledgeItemSerializer(item).data)

    @extend_schema(request=ReasonSerializer, responses=KnowledgeItemSerializer)
    @action(detail=True, methods=["post"])
    def retire(self, request, uuid=None):
        data = ReasonSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        item = services.retire_item(
            actor=request.user, item=self.get_object(), reason=data.validated_data["reason"]
        )
        return Response(KnowledgeItemSerializer(item).data)


class ReviewViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    """Members' questions for reviewers, without the asker's name.
    `?kind=` unreviewed (default), not_yet, flagged, held or reviewed;
    `?language=`. `.../decide/` approves, corrects or rejects an answer;
    approved and corrected answers are reused for the same question."""

    permission_classes = [IsReviewer]
    serializer_class = ReviewQuestionSerializer
    lookup_field = "uuid"
    filterset_fields = {"language": ["exact"]}
    search_fields = ["text"]

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return Question.objects.none()
        kind = self.request.query_params.get("kind", "")
        return _with_items(services.review_queue(kind if self.action == "list" else "reviewed"))

    def get_object(self):
        return get_object_or_404(_with_items(Question.objects), uuid=self.kwargs["uuid"])

    @extend_schema(
        parameters=[
            OpenApiParameter(
                "kind", str, enum=["unreviewed", "not_yet", "flagged", "held", "reviewed"]
            )
        ]
    )
    def list(self, request, *args, **kwargs):
        return super().list(request, *args, **kwargs)

    @extend_schema(request=ReviewDecisionSerializer, responses=ReviewQuestionSerializer)
    @action(detail=True, methods=["post"])
    def decide(self, request, uuid=None):
        data = ReviewDecisionSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        services.review(actor=request.user, question=self.get_object(), **data.validated_data)
        return Response(ReviewQuestionSerializer(self.get_object()).data)

    @extend_schema(responses={(200, "application/octet-stream"): bytes})
    @action(detail=True, methods=["get"])
    def recording(self, request, uuid=None):
        """The spoken question, when the asker allowed it to be kept."""
        question = self.get_object()
        if not question.recording:
            return Response({"detail": "No recording was kept."}, status=status.HTTP_404_NOT_FOUND)
        extension = "." + question.recording.name.rsplit(".", 1)[-1].lower()
        return _audio(
            question.recording, services.VOICE_TYPES.get(extension, "application/octet-stream")
        )


class AskMeSettingsView(APIView):
    """Ask Me's switches: open to members, tone, limits, campaign silence and
    the "not yet" wording per language. A National Administrator changes them."""

    permission_classes = [IsReviewer]

    @extend_schema(responses=AskMeSettingsSerializer)
    def get(self, request):
        return Response(AskMeSettingsSerializer(services.current_settings()).data)

    @extend_schema(request=AskMeSettingsSerializer, responses=AskMeSettingsSerializer)
    def patch(self, request):
        data = AskMeSettingsSerializer(services.current_settings(), data=request.data, partial=True)
        data.is_valid(raise_exception=True)
        config = services.update_settings(actor=request.user, **data.validated_data)
        return Response(AskMeSettingsSerializer(config).data)

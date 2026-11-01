from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404
from django.utils import timezone
from drf_spectacular.utils import extend_schema
from PIL import Image
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import BasePermission, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.access import audit, engine
from apps.core.caching import remembered
from apps.core.permissions import Capability, requires
from apps.geography.scope import (
    SCOPE_FIELDS,
    covering_q,
    default_to_actor_scope,
    scope_contains,
    within_scope_q,
)
from apps.membership.registration import validate_image
from apps.membership.serializers import flatten_scope

from .models import Announcement, PopupMessage, popups_showing
from .serializers import (
    AnnouncementSerializer,
    NewPopupSerializer,
    PopupSerializer,
    PostedPopupSerializer,
)

# A picture wider and taller than this is not a picture for a pop-up.
POPUP_MAX_PIXELS = 40_000_000


class AnnouncementViewSet(viewsets.ModelViewSet):
    """Staff publish announcements to an area inside their own scope."""

    serializer_class = AnnouncementSerializer
    lookup_field = "uuid"
    permission_classes = [requires(Capability.SEND_BROADCAST)]
    http_method_names = ["get", "post", "patch", "delete", "head", "options"]

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return Announcement.objects.none()
        user = self.request.user
        return Announcement.objects.filter(
            within_scope_q(user, permission=Capability.SEND_BROADCAST)
        ).select_related("author", *SCOPE_FIELDS)

    def _save(self, serializer, **extra):
        data = flatten_scope(serializer.validated_data)
        if serializer.instance is None:
            default_to_actor_scope(self.request.user, data, Capability.SEND_BROADCAST)
        announcement = serializer.instance or Announcement(author=self.request.user)
        for name, value in {**data, **extra}.items():
            setattr(announcement, name, value)
        announcement.full_clean()
        if not scope_contains(
            self.request.user, announcement, permission=Capability.SEND_BROADCAST
        ):
            raise PermissionDenied("You can only address areas inside your own scope.")
        announcement.save()
        serializer.instance = announcement

    def perform_create(self, serializer):
        self._save(serializer)

    def perform_update(self, serializer):
        self._save(serializer)


def announcements_for(target):
    """Published announcements that apply to `target` (a member)."""
    return (
        Announcement.objects.filter(covering_q(target))
        .filter(is_published=True, published_at__lte=timezone.now())
        .select_related("author", *SCOPE_FIELDS)
    )


def posts_popups(user) -> bool:
    """A pop-up goes to everyone, so the power must be held for all of Nigeria."""
    return any(
        Capability.POST_POPUPS in grant.permissions and grant.is_national
        for grant in engine.active_grants(user)
    )


class PostsPopups(BasePermission):
    message = "Your role does not permit this action."

    def has_permission(self, request, view):
        return posts_popups(request.user)


def _picture_type(upload) -> str:
    """The checked picture's media type."""
    validate_image(upload, "picture", required=False)
    if not upload:
        return ""
    image = Image.open(upload)
    width, height = image.size
    media_type = Image.MIME[image.format]
    upload.seek(0)
    if width * height > POPUP_MAX_PIXELS:
        raise ValidationError({"picture": "This picture is too large."})
    return media_type


class PopupViewSet(mixins.ListModelMixin, viewsets.GenericViewSet):
    """Pop-up messages to everyone: posted by whoever holds postPopups
    nationwide, newest first. One that was posted can be stopped."""

    serializer_class = PostedPopupSerializer
    permission_classes = [PostsPopups]
    parser_classes = [MultiPartParser, FormParser, JSONParser]
    lookup_field = "uuid"
    queryset = PopupMessage.objects.select_related("author")

    @extend_schema(request=NewPopupSerializer, responses={201: PostedPopupSerializer})
    def create(self, request):
        data = NewPopupSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        values = dict(data.validated_data)
        upload = values.pop("picture", None)
        picture_type = _picture_type(upload)
        with transaction.atomic():
            popup = PopupMessage.objects.create(
                author=request.user,
                picture=upload or "",
                picture_type=picture_type,
                **values,
            )
            audit.record(
                actor=request.user,
                action="popup.posted",
                target_type="popup",
                target_id=popup.uuid,
                target_label=popup.title,
                changes={
                    "frequency": popup.frequency,
                    "starts_at": popup.starts_at.isoformat(),
                    "ends_at": popup.ends_at.isoformat() if popup.ends_at else None,
                },
            )
        return Response(PostedPopupSerializer(popup).data, status=status.HTTP_201_CREATED)

    @extend_schema(request=None, responses=PostedPopupSerializer)
    @action(detail=True, methods=["post"])
    def stop(self, request, uuid=None):
        """Stop showing it. It stays on record."""
        with transaction.atomic():
            popup = PopupMessage.objects.select_for_update(of=("self",)).get(
                pk=self.get_object().pk
            )
            if popup.stopped_at is None:
                popup.stopped_at = timezone.now()
                popup.stopped_by = request.user
                popup.save(update_fields=["stopped_at", "stopped_by", "updated_at"])
                audit.record(
                    actor=request.user,
                    action="popup.stopped",
                    target_type="popup",
                    target_id=popup.uuid,
                    target_label=popup.title,
                )
        return Response(PostedPopupSerializer(popup).data)


class MyPopupsView(APIView):
    """What pops up now, newest first, the same for everyone. How often each
    person sees it is kept on their device."""

    permission_classes = [IsAuthenticated]

    @extend_schema(responses={200: PopupSerializer(many=True)})
    def get(self, request):
        answer = remembered(
            "popups",
            {},
            lambda: {"items": [PopupSerializer(item).data for item in popups_showing()]},
        )
        return Response(answer["items"])


class PopupPictureView(APIView):
    """A pop-up's picture, for anyone signed in; those who post also see the
    picture of one that was stopped."""

    permission_classes = [IsAuthenticated]

    @extend_schema(responses={(200, "application/octet-stream"): bytes})
    def get(self, request, uuid):
        popup = get_object_or_404(PopupMessage, uuid=uuid)
        hidden = popup.stopped_at or popup.starts_at > timezone.now()
        if not popup.picture or (hidden and not posts_popups(request.user)):
            raise Http404
        response = FileResponse(popup.picture.open("rb"), content_type=popup.picture_type)
        response["X-Content-Type-Options"] = "nosniff"
        response["Cache-Control"] = "private, no-store"
        return response

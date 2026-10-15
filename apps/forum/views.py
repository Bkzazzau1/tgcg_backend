from django.db.models import Count, Q
from django.http import FileResponse
from django.shortcuts import get_object_or_404
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.access import engine
from apps.core.permissions import Capability, requires
from apps.geography.scope import SCOPE_FIELDS, ScopeRef, within_scope_q

from . import services
from .models import Attachment, ForumSettings, Post, Reply
from .serializers import (
    AssignmentTagSerializer,
    ForumReasonSerializer,
    ForumSettingsSerializer,
    PostCreateSerializer,
    PostSerializer,
    ReplyCreateSerializer,
    ReplySerializer,
)

C = Capability
P = Post.Status
MODERATING = ("approve", "reject", "hide")


class PostViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.DestroyModelMixin,
    viewsets.GenericViewSet,
):
    """The forum, for everyone with a role (never ordinary members).

    The feed holds published posts from all of Nigeria, newest first. A post
    made at a polling unit waits for the ward coordinator while approval is
    on (`/forum/settings/`); `?show=review` lists those an account approves,
    `?show=mine` the account's own, with their status.
    """

    serializer_class = PostSerializer
    lookup_field = "uuid"
    parser_classes = [MultiPartParser, FormParser, JSONParser]
    filterset_fields = {
        "state__code": ["exact"],
        "lga__code": ["exact"],
        "ward__code": ["exact"],
        "polling_unit__code": ["exact"],
    }

    def get_permissions(self):
        if self.action == "create":
            return [requires(C.CREATE_DISCUSSION_THREAD)()]
        if self.action in MODERATING:
            return [requires(C.MODERATE_DISCUSSION)()]
        if self.action == "replies" and self.request.method == "POST":
            return [requires(C.POST_DISCUSSION_REPLY)()]
        return [requires(C.VIEW_DISCUSSION_ROOM)()]

    def _posts(self):
        return (
            services.visible_posts(self.request.user)
            .select_related(*SCOPE_FIELDS)
            .prefetch_related("attachments")
            .annotate(
                reply_count=Count(
                    "replies", filter=Q(replies__status=Reply.Status.VISIBLE), distinct=True
                )
            )
        )

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return Post.objects.none()
        posts = self._posts()
        if self.action != "list":
            return posts
        params = self.request.query_params
        show = params.get("show", "feed")
        if show == "mine":
            posts = posts.filter(author=self.request.user).order_by("-created_at")
        elif show == "review":
            posts = posts.filter(
                within_scope_q(self.request.user, permission=C.MODERATE_DISCUSSION),
                status=P.PENDING,
            ).order_by("created_at")
        else:
            posts = posts.filter(status=P.PUBLISHED).order_by("-published_at", "-id")
        if tag := params.get("assignment", "").strip():
            posts = posts.filter(assignment__iexact=tag)
        elif params.get("assignments") == "true":
            posts = posts.exclude(assignment="")
        return posts

    @extend_schema(
        parameters=[
            OpenApiParameter("show", str, enum=["feed", "mine", "review"]),
            OpenApiParameter("assignment", str, description="Posts tagged with this assignment"),
            OpenApiParameter("assignments", bool, description="Only posts tagged with any"),
        ]
    )
    def list(self, request, *args, **kwargs):
        return super().list(request, *args, **kwargs)

    def _respond(self, post, code=status.HTTP_200_OK):
        post = self._posts().get(pk=post.pk)
        return Response(self.get_serializer(post).data, status=code)

    @extend_schema(
        request={"multipart/form-data": PostCreateSerializer}, responses={201: PostSerializer}
    )
    def create(self, request):
        data = PostCreateSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        fields = dict(data.validated_data)
        files = fields.pop("files", [])
        level, code = fields.pop("level", None), fields.pop("code", None)
        if level:
            area = ScopeRef.resolve(level, code)
            fields["scope_level"] = area.scope_level
            fields.update({name: getattr(area, name) for name in SCOPE_FIELDS})
        post = services.create_post(actor=request.user, files=files, **fields)
        return self._respond(post, status.HTTP_201_CREATED)

    def destroy(self, request, uuid=None):
        """The author deletes their own post."""
        services.delete_post(actor=request.user, post=self.get_object())
        return Response(status=status.HTTP_204_NO_CONTENT)

    @extend_schema(request=None, responses=PostSerializer)
    @action(detail=True, methods=["post"])
    def approve(self, request, uuid=None):
        return self._respond(services.approve_post(actor=request.user, post=self.get_object()))

    @extend_schema(request=ForumReasonSerializer, responses=PostSerializer)
    @action(detail=True, methods=["post"])
    def reject(self, request, uuid=None):
        data = ForumReasonSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        post = services.reject_post(
            actor=request.user, post=self.get_object(), reason=data.validated_data["reason"]
        )
        return self._respond(post)

    @extend_schema(request=ForumReasonSerializer, responses=PostSerializer)
    @action(detail=True, methods=["post"])
    def hide(self, request, uuid=None):
        data = ForumReasonSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        post = services.hide_post(
            actor=request.user, post=self.get_object(), reason=data.validated_data["reason"]
        )
        return self._respond(post)

    @extend_schema(methods=["GET"], request=None, responses=ReplySerializer(many=True))
    @extend_schema(
        methods=["POST"], request=ReplyCreateSerializer, responses={201: ReplySerializer}
    )
    @action(detail=True, methods=["get", "post"])
    def replies(self, request, uuid=None):
        """Replies on a post, oldest first; they appear at once."""
        post = self.get_object()
        if request.method == "GET":
            replies = services.visible_replies(request.user, post).select_related("post")
            return Response(ReplySerializer(replies, many=True, context={"request": request}).data)
        data = ReplyCreateSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        reply = services.add_reply(actor=request.user, post=post, body=data.validated_data["body"])
        return Response(
            ReplySerializer(reply, context={"request": request}).data,
            status=status.HTTP_201_CREATED,
        )


class ReplyViewSet(mixins.DestroyModelMixin, viewsets.GenericViewSet):
    """A reply: its author deletes it; a moderator of the post's area hides it."""

    serializer_class = ReplySerializer
    lookup_field = "uuid"

    def get_permissions(self):
        if self.action == "hide":
            return [requires(C.MODERATE_DISCUSSION)()]
        return [requires(C.VIEW_DISCUSSION_ROOM)()]

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return Reply.objects.none()
        return (
            Reply.objects.filter(post__in=services.visible_posts(self.request.user))
            .exclude(status=Reply.Status.DELETED)
            .select_related("post")
        )

    def destroy(self, request, uuid=None):
        services.delete_reply(actor=request.user, reply=self.get_object())
        return Response(status=status.HTTP_204_NO_CONTENT)

    @extend_schema(request=ForumReasonSerializer, responses=ReplySerializer)
    @action(detail=True, methods=["post"])
    def hide(self, request, uuid=None):
        data = ForumReasonSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        reply = services.hide_reply(
            actor=request.user, reply=self.get_object(), reason=data.validated_data["reason"]
        )
        return Response(ReplySerializer(reply, context={"request": request}).data)


class AttachmentView(APIView):
    """A picture or video, for whoever may see its post."""

    permission_classes = [requires(C.VIEW_DISCUSSION_ROOM)]

    @extend_schema(responses={(200, "application/octet-stream"): bytes})
    def get(self, request, uuid):
        attachment = get_object_or_404(
            Attachment, uuid=uuid, post__in=services.visible_posts(request.user)
        )
        response = FileResponse(attachment.file.open("rb"), content_type=attachment.mime_type)
        response["X-Content-SHA256"] = attachment.content_hash
        response["Cache-Control"] = "private, no-store"
        return response


class ForumSettingsView(APIView):
    """Whether posts from polling units wait for the ward coordinator. Only a
    National Administrator changes it."""

    permission_classes = [requires(C.VIEW_DISCUSSION_ROOM)]

    def _body(self, request, current):
        return ForumSettingsSerializer(
            {
                "approval_required": current.approval_required,
                "can_change": engine.is_national_administrator(request.user),
                "updated_at": current.updated_at,
            }
        ).data

    @extend_schema(responses=ForumSettingsSerializer)
    def get(self, request):
        return Response(self._body(request, ForumSettings.current()))

    @extend_schema(request=ForumSettingsSerializer, responses=ForumSettingsSerializer)
    def patch(self, request):
        data = ForumSettingsSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        current = services.set_approval_required(
            actor=request.user, required=data.validated_data["approval_required"]
        )
        return Response(self._body(request, current))


class AssignmentTagsView(APIView):
    """Assignments tagged on published posts, the most recent first."""

    permission_classes = [requires(C.VIEW_DISCUSSION_ROOM)]

    @extend_schema(responses=AssignmentTagSerializer(many=True))
    def get(self, request):
        return Response(services.assignment_tags(request.user))

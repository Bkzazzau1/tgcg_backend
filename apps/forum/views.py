from django.db.models import Count, Exists, IntegerField, OuterRef, Q, Subquery, Value
from django.db.models.functions import Coalesce
from django.http import FileResponse
from django.shortcuts import get_object_or_404
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import BasePermission
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.geography.scope import SCOPE_FIELDS

from . import services
from .models import Attachment, Like, Post, Reply, Report
from .serializers import (
    AssignmentTagSerializer,
    ForumMeSerializer,
    ForumReasonSerializer,
    ForumSettingsSerializer,
    LikesSerializer,
    OptionalReasonSerializer,
    PostCreateSerializer,
    PostSerializer,
    PostUpdateSerializer,
    ReplyCreateSerializer,
    ReplySerializer,
    ReplyUpdateSerializer,
    ReportSerializer,
)

P = Post.Status


class TakesPart(BasePermission):
    """Every member with an active membership, and every account with a role."""

    message = "Only members with an active membership take part in the forum."

    def has_permission(self, request, view):
        return services.takes_part(request.user)


class IsEditor(BasePermission):
    """Everyone holding a role or position."""

    message = "Only someone holding a role or position can do this."

    def has_permission(self, request, view):
        return services.is_editor(request.user)


def _likes(model, field, user):
    """Like counts and this account's like, without joining the likes in."""
    counted = (
        Like.objects.filter(**{field: OuterRef("pk")})
        .values(field)
        .annotate(n=Count("pk"))
        .values("n")
    )
    return {
        "like_count": Coalesce(Subquery(counted, output_field=IntegerField()), Value(0)),
        "liked": Exists(Like.objects.filter(**{field: OuterRef("pk")}, user=user)),
    }


class _LikeActions:
    """POST likes; DELETE takes the like back."""

    def _like(self, request, **target):
        if request.method == "DELETE":
            count = services.unlike(actor=request.user, **target)
            return Response({"liked": False, "likes": count})
        count = services.like(actor=request.user, **target)
        return Response({"liked": True, "likes": count})


class PostViewSet(
    _LikeActions,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.DestroyModelMixin,
    viewsets.GenericViewSet,
):
    """The Social Forum, for every member (and every account with a role).

    The feed holds published posts from all of Nigeria, newest first; they
    appear at once. `?show=mine` lists the account's own posts with their
    status; `?show=hidden`, for editors, the hidden ones. Authors edit
    (PATCH) and delete their own posts. Editors (everyone holding a role or
    position) hide and restore any post.
    """

    serializer_class = PostSerializer
    lookup_field = "uuid"
    parser_classes = [MultiPartParser, FormParser, JSONParser]
    permission_classes = [TakesPart]
    http_method_names = ["get", "post", "patch", "delete", "head", "options"]
    filterset_fields = {
        "state__code": ["exact"],
        "lga__code": ["exact"],
        "ward__code": ["exact"],
        "polling_unit__code": ["exact"],
    }

    def get_permissions(self):
        if self.action in ("hide", "restore"):
            return [TakesPart(), IsEditor()]
        return super().get_permissions()

    def _posts(self):
        return (
            services.visible_posts(self.request.user)
            .select_related(*SCOPE_FIELDS)
            .prefetch_related("attachments")
            .annotate(
                reply_count=Count(
                    "replies", filter=Q(replies__status=Reply.Status.VISIBLE), distinct=True
                ),
                **_likes(Post, "post", self.request.user),
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
        elif show == "hidden":
            posts = posts.filter(status=P.HIDDEN).order_by("-reviewed_at", "-id")
        elif show == "review":
            posts = posts.none()  # nothing waits for approval any more (older apps ask)
        else:
            posts = posts.filter(status=P.PUBLISHED).order_by("-published_at", "-id")
        if tag := params.get("assignment", "").strip():
            posts = posts.filter(assignment__iexact=tag)
        elif params.get("assignments") == "true":
            posts = posts.exclude(assignment="")
        return posts

    @extend_schema(
        parameters=[
            OpenApiParameter("show", str, enum=["feed", "mine", "hidden"]),
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
        fields.pop("level", None)
        fields.pop("code", None)
        post = services.create_post(actor=request.user, files=files, **fields)
        return self._respond(post, status.HTTP_201_CREATED)

    @extend_schema(request=PostUpdateSerializer, responses=PostSerializer)
    def partial_update(self, request, uuid=None):
        """Its author changes the words or the assignment tag."""
        data = PostUpdateSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        post = services.edit_post(actor=request.user, post=self.get_object(), **data.validated_data)
        return self._respond(post)

    def destroy(self, request, uuid=None):
        """The author deletes their own post."""
        services.delete_post(actor=request.user, post=self.get_object())
        return Response(status=status.HTTP_204_NO_CONTENT)

    @extend_schema(request=ForumReasonSerializer, responses=PostSerializer)
    @action(detail=True, methods=["post"])
    def hide(self, request, uuid=None):
        data = ForumReasonSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        post = services.hide_post(
            actor=request.user, post=self.get_object(), reason=data.validated_data["reason"]
        )
        return self._respond(post)

    @extend_schema(request=None, responses=PostSerializer)
    @action(detail=True, methods=["post"])
    def restore(self, request, uuid=None):
        return self._respond(services.restore_post(actor=request.user, post=self.get_object()))

    @extend_schema(request=None, responses=LikesSerializer)
    @action(detail=True, methods=["post", "delete"])
    def like(self, request, uuid=None):
        return self._like(request, post=self.get_object())

    @extend_schema(request=ForumReasonSerializer, responses={201: ReportSerializer})
    @action(detail=True, methods=["post"])
    def report(self, request, uuid=None):
        data = ForumReasonSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        made = services.report(
            actor=request.user, post=self.get_object(), reason=data.validated_data["reason"]
        )
        return Response(ReportSerializer(made).data, status=status.HTTP_201_CREATED)

    @extend_schema(methods=["GET"], request=None, responses=ReplySerializer(many=True))
    @extend_schema(
        methods=["POST"], request=ReplyCreateSerializer, responses={201: ReplySerializer}
    )
    @action(detail=True, methods=["get", "post"])
    def replies(self, request, uuid=None):
        """Comments on a post, oldest first, with their answers (`parent`)."""
        post = self.get_object()
        context = {"request": request}
        if request.method == "GET":
            replies = (
                services.visible_replies(request.user, post)
                .select_related("post", "parent")
                .annotate(**_likes(Reply, "reply", request.user))
            )
            return Response(ReplySerializer(replies, many=True, context=context).data)
        data = ReplyCreateSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        parent = None
        if parent_id := data.validated_data.get("parent"):
            parent = services.visible_replies(request.user, post).filter(uuid=parent_id).first()
            if parent is None:
                return Response(
                    {"parent": ["Answer a comment on this post."]},
                    status=status.HTTP_400_BAD_REQUEST,
                )
        reply = services.add_reply(
            actor=request.user, post=post, body=data.validated_data["body"], parent=parent
        )
        return Response(
            ReplySerializer(reply, context=context).data, status=status.HTTP_201_CREATED
        )


class ReplyViewSet(_LikeActions, mixins.DestroyModelMixin, viewsets.GenericViewSet):
    """A comment: its author edits (PATCH) or deletes it; anyone likes or
    reports it; an editor hides or restores it."""

    serializer_class = ReplySerializer
    lookup_field = "uuid"
    permission_classes = [TakesPart]
    http_method_names = ["post", "patch", "delete", "options"]

    def get_permissions(self):
        if self.action in ("hide", "restore"):
            return [TakesPart(), IsEditor()]
        return super().get_permissions()

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return Reply.objects.none()
        user = self.request.user
        replies = Reply.objects.filter(post__in=services.visible_posts(user)).exclude(
            status=Reply.Status.DELETED
        )
        if not services.is_editor(user):
            replies = replies.filter(Q(status=Reply.Status.VISIBLE) | Q(author=user))
        return replies.select_related("post", "parent")

    def _respond(self, reply):
        reply = (
            self.get_queryset()
            .annotate(**_likes(Reply, "reply", self.request.user))
            .get(pk=reply.pk)
        )
        return Response(ReplySerializer(reply, context={"request": self.request}).data)

    @extend_schema(request=ReplyUpdateSerializer, responses=ReplySerializer)
    def partial_update(self, request, uuid=None):
        data = ReplyUpdateSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        reply = services.edit_reply(
            actor=request.user, reply=self.get_object(), body=data.validated_data["body"]
        )
        return self._respond(reply)

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
        return self._respond(reply)

    @extend_schema(request=None, responses=ReplySerializer)
    @action(detail=True, methods=["post"])
    def restore(self, request, uuid=None):
        return self._respond(services.restore_reply(actor=request.user, reply=self.get_object()))

    @extend_schema(request=None, responses=LikesSerializer)
    @action(detail=True, methods=["post", "delete"])
    def like(self, request, uuid=None):
        return self._like(request, reply=self.get_object())

    @extend_schema(request=ForumReasonSerializer, responses={201: ReportSerializer})
    @action(detail=True, methods=["post"])
    def report(self, request, uuid=None):
        data = ForumReasonSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        reply = self.get_object()
        made = services.report(
            actor=request.user,
            post=reply.post,
            reply=reply,
            reason=data.validated_data["reason"],
        )
        return Response(ReportSerializer(made).data, status=status.HTTP_201_CREATED)


class ReportViewSet(mixins.ListModelMixin, viewsets.GenericViewSet):
    """Reports for editors (everyone holding a role or position), newest
    first: `?status=open` (the default) or `closed`. Hiding the post or
    comment closes its reports; `.../dismiss/` closes one with nothing wrong."""

    serializer_class = ReportSerializer
    lookup_field = "uuid"
    permission_classes = [TakesPart, IsEditor]

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return Report.objects.none()
        wanted = self.request.query_params.get("status", Report.Status.OPEN)
        return Report.objects.filter(status=wanted).select_related("post", "reply", "reporter")

    @extend_schema(parameters=[OpenApiParameter("status", str, enum=["open", "closed"])])
    def list(self, request, *args, **kwargs):
        return super().list(request, *args, **kwargs)

    @extend_schema(request=OptionalReasonSerializer, responses=ReportSerializer)
    @action(detail=True, methods=["post"])
    def dismiss(self, request, uuid=None):
        data = OptionalReasonSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        report = get_object_or_404(Report, uuid=uuid)
        closed = services.dismiss_report(
            actor=request.user, report=report, reason=data.validated_data["reason"]
        )
        return Response(ReportSerializer(closed).data)


class AttachmentView(APIView):
    """A picture or video, for whoever may see its post."""

    permission_classes = [TakesPart]

    @extend_schema(responses={(200, "application/octet-stream"): bytes})
    def get(self, request, uuid):
        attachment = get_object_or_404(
            Attachment, uuid=uuid, post__in=services.visible_posts(request.user)
        )
        response = FileResponse(attachment.file.open("rb"), content_type=attachment.mime_type)
        response["X-Content-SHA256"] = attachment.content_hash
        response["Cache-Control"] = "private, no-store"
        return response


class ForumMeView(APIView):
    """What this account may do in the forum."""

    permission_classes = [TakesPart]

    @extend_schema(responses=ForumMeSerializer)
    def get(self, request):
        return Response({"takes_part": True, "editor": services.is_editor(request.user)})


class ForumSettingsView(APIView):
    """Kept for older apps: posts no longer wait for approval (24 October 2026)."""

    permission_classes = [TakesPart]

    @extend_schema(responses=ForumSettingsSerializer)
    def get(self, request):
        return Response({"approval_required": False, "can_change": False, "updated_at": None})


class AssignmentTagsView(APIView):
    """Assignments tagged on published posts, the most recent first."""

    permission_classes = [TakesPart]

    @extend_schema(responses=AssignmentTagSerializer(many=True))
    def get(self, request):
        return Response(services.assignment_tags(request.user))

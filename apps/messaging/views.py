from django.shortcuts import get_object_or_404
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.access import engine
from apps.core.permissions import Capability, requires
from apps.projects.models import Project
from apps.projects.services import require_visible

from . import services
from .models import Broadcast, MessageList
from .serializers import (
    BroadcastSerializer,
    DeviceSerializer,
    InboxSerializer,
    MessageListDetailSerializer,
    MessageListSerializer,
    MessageListUpdateSerializer,
    MessageListWriteSerializer,
    MessageSerializer,
    PreviewSerializer,
)

C = Capability
SENDING = (C.SEND_SMS, C.SEND_WHATSAPP, C.SEND_PUSH_NOTIFICATIONS)


def resolve_groups(user, groups) -> list[dict]:
    """Projects the user sees and lists they own, in place of their ids."""
    resolved = []
    for group in groups:
        group = dict(group)
        if group["type"] == "project":
            project = get_object_or_404(Project, uuid=group["project"])
            require_visible(user, project)
            group["project"] = project
        elif group["type"] == "list":
            message_list = get_object_or_404(MessageList, uuid=group["list"])
            services.require_owner(user, message_list)
            group["list"] = message_list
        resolved.append(group)
    return resolved


class BroadcastViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    """Bulk messages: SMS, WhatsApp and app notifications to groups of members.

    Each channel needs its own power (sendSms, sendWhatsApp,
    sendPushNotifications), and reaches only members in the areas where the
    sender holds it. `preview/` counts who a message would reach; POST sends
    it at once. Senders see what they sent (an administrator, everything).
    """

    serializer_class = BroadcastSerializer
    lookup_field = "uuid"
    permission_classes = [requires(*SENDING)]

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return Broadcast.objects.none()
        broadcasts = Broadcast.objects.select_related("sender")
        if engine.is_national_administrator(self.request.user):
            return broadcasts
        return broadcasts.filter(sender=self.request.user)

    @extend_schema(request=MessageSerializer, responses={201: BroadcastSerializer})
    def create(self, request):
        data = MessageSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        message = data.validated_data
        broadcast = services.send(
            sender=request.user,
            channels=message["channels"],
            title=message["title"],
            body=message["body"],
            groups=resolve_groups(request.user, message["groups"]),
            record={"groups": request.data.get("groups")},
        )
        return Response(BroadcastSerializer(broadcast).data, status=status.HTTP_201_CREATED)

    @extend_schema(request=PreviewSerializer, responses={200: OpenApiTypes.OBJECT})
    @action(detail=False, methods=["post"])
    def preview(self, request):
        """`{"people", "channels": {channel: {"reachable", "unreachable"}}, "sms_pages"}`."""
        data = PreviewSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        message = data.validated_data
        return Response(
            services.preview(
                sender=request.user,
                channels=message["channels"],
                groups=resolve_groups(request.user, message["groups"]),
                body=message["body"],
            )
        )


class MessageListViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.DestroyModelMixin,
    viewsets.GenericViewSet,
):
    """Saved lists of members, each the sender's own. Members are added by
    membership number, or as the people some groups reach now."""

    lookup_field = "uuid"
    permission_classes = [requires(*SENDING)]

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return MessageList.objects.none()
        lists = MessageList.objects.all()
        if engine.is_national_administrator(self.request.user):
            return lists
        return lists.filter(owner=self.request.user)

    def get_serializer_class(self):
        return MessageListDetailSerializer if self.action == "retrieve" else MessageListSerializer

    @extend_schema(request=MessageListWriteSerializer, responses={201: MessageListDetailSerializer})
    def create(self, request):
        data = MessageListWriteSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        message_list = services.create_list(
            owner=request.user,
            name=data.validated_data["name"],
            numbers=data.validated_data.get("members", []),
            groups=resolve_groups(request.user, data.validated_data.get("groups", [])),
        )
        return Response(
            MessageListDetailSerializer(message_list).data, status=status.HTTP_201_CREATED
        )

    @extend_schema(request=MessageListUpdateSerializer, responses=MessageListDetailSerializer)
    def partial_update(self, request, uuid=None):
        data = MessageListUpdateSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        message_list = services.update_list(
            user=request.user, message_list=self.get_object(), **data.validated_data
        )
        return Response(MessageListDetailSerializer(message_list).data)

    def perform_destroy(self, instance):
        services.delete_list(user=self.request.user, message_list=instance)


class MyDevicesView(APIView):
    """POST: this phone receives the account's app notifications. DELETE (with
    the same body): it stops, e.g. on signing out."""

    permission_classes = [IsAuthenticated]

    @extend_schema(request=DeviceSerializer, responses={204: None})
    def post(self, request):
        data = DeviceSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        services.register_device(user=request.user, **data.validated_data)
        return Response(status=status.HTTP_204_NO_CONTENT)

    @extend_schema(request=DeviceSerializer, responses={204: None})
    def delete(self, request):
        token = request.data.get("token") or ""
        if not token:
            raise ValidationError({"token": "Missing."})
        services.unregister_device(user=request.user, token=token)
        return Response(status=status.HTTP_204_NO_CONTENT)


class MyMessagesView(APIView):
    """Messages sent to the signed-in member on any channel, newest first."""

    permission_classes = [IsAuthenticated]

    @extend_schema(responses=InboxSerializer(many=True))
    def get(self, request):
        member = getattr(request.user, "member", None)
        if member is None:
            return Response([])
        messages = (
            Broadcast.objects.filter(deliveries__member=member).distinct().order_by("-created_at")
        )[:200]
        return Response(InboxSerializer(messages, many=True).data)

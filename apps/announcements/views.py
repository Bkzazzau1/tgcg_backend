from django.core.exceptions import PermissionDenied
from django.utils import timezone
from rest_framework import viewsets

from apps.core.permissions import Capability, requires
from apps.geography.scope import (
    SCOPE_FIELDS,
    covering_q,
    default_to_actor_scope,
    scope_contains,
    within_scope_q,
)
from apps.membership.serializers import flatten_scope

from .models import Announcement
from .serializers import AnnouncementSerializer


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

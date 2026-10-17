from rest_framework import serializers

from apps.geography.scope import ScopeField

from . import services
from .models import SecurityDispatch

S = SecurityDispatch.Status


class DispatchSendSerializer(serializers.Serializer):
    officer = serializers.CharField(help_text="The officer's access ID, from the nearest list.")
    note = serializers.CharField(required=False, allow_blank=True, max_length=1000, default="")


class DispatchCancelSerializer(serializers.Serializer):
    reason = serializers.CharField(max_length=1000)


class DispatchRespondSerializer(serializers.Serializer):
    step = serializers.ChoiceField(
        choices=[S.ACCEPTED, S.DECLINED, S.ON_THE_WAY, S.ARRIVED, S.RESOLVED]
    )
    note = serializers.CharField(required=False, allow_blank=True, max_length=1000, default="")


class CandidateSerializer(serializers.Serializer):
    """One officer in the nearest list."""

    access_id = serializers.CharField(source="officer.user.access_id")
    name = serializers.CharField(source="officer.user.full_name")
    roles = serializers.ListField(source="officer.roles", child=serializers.CharField())
    distance_m = serializers.IntegerField(allow_null=True)
    located_by = serializers.CharField(help_text="live, area, or blank when unknown.")
    area = serializers.SerializerMethodField(help_text="The area measured from.")
    seen_at = serializers.DateTimeField(
        allow_null=True, help_text="When their live location was taken."
    )
    busy = serializers.BooleanField(help_text="Another incident is with them now.")

    def get_area(self, candidate) -> str | None:
        return str(candidate.area) if candidate.area is not None else None


class DispatchSerializer(serializers.ModelSerializer):
    """An incident sent to an officer, as the Situation Room follows it."""

    id = serializers.UUIDField(source="uuid", read_only=True)
    incident = serializers.CharField(source="incident.reference")
    officer = serializers.CharField(source="officer.access_id")
    officer_name = serializers.CharField(source="officer.full_name")
    officer_phone = serializers.SerializerMethodField(
        help_text="For the Situation Room, which may call them."
    )
    sent_by = serializers.CharField(source="sent_by.full_name")
    sent_at = serializers.DateTimeField(source="created_at")
    cancelled_by = serializers.CharField(
        source="cancelled_by.full_name", default=None, allow_null=True
    )

    class Meta:
        model = SecurityDispatch
        fields = [
            "id",
            "incident",
            "status",
            "officer",
            "officer_name",
            "officer_phone",
            "sent_by",
            "sent_at",
            "note",
            "distance_m",
            "located_by",
            "notified",
            "accepted_at",
            "on_the_way_at",
            "arrived_at",
            "finished_at",
            "outcome",
            "cancelled_by",
            "cancel_reason",
        ]
        read_only_fields = fields

    def get_officer_phone(self, dispatch) -> str | None:
        if not self.context.get("can_send"):
            return None
        member = getattr(dispatch.officer, "member", None)
        return dispatch.officer.phone_number or (member.phone_number if member else None)


class OfficerIncidentSerializer(serializers.Serializer):
    """What the officer needs to get there and help."""

    reference = serializers.CharField()
    title = serializers.CharField()
    category = serializers.CharField()
    severity = serializers.CharField()
    status = serializers.CharField()
    summary = serializers.CharField()
    reported_at = serializers.DateTimeField()
    scope = ScopeField(read_only=True)
    place = serializers.SerializerMethodField()
    latitude = serializers.SerializerMethodField()
    longitude = serializers.SerializerMethodField()
    gps = serializers.SerializerMethodField(
        help_text="True when the point is the reporter's GPS fix, false for the place."
    )
    reporter_name = serializers.CharField(source="reporter.full_name")
    reporter_phone = serializers.SerializerMethodField()

    def _point(self, incident):
        cache = self.context.setdefault("points", {})
        if incident.pk not in cache:
            cache[incident.pk] = services.incident_point(incident)
        return cache[incident.pk]

    def get_place(self, incident) -> str:
        return services.place_label(incident)

    def get_latitude(self, incident) -> float | None:
        point = self._point(incident)
        return point[0] if point else None

    def get_longitude(self, incident) -> float | None:
        point = self._point(incident)
        return point[1] if point else None

    def get_gps(self, incident) -> bool:
        return incident.latitude is not None and not incident.gps_is_mocked

    def get_reporter_phone(self, incident) -> str:
        return services.reporter_phone(incident)


class MyDispatchSerializer(serializers.ModelSerializer):
    """An incident sent to the signed-in officer."""

    id = serializers.UUIDField(source="uuid", read_only=True)
    incident = OfficerIncidentSerializer()
    sent_by = serializers.CharField(source="sent_by.full_name")
    sent_at = serializers.DateTimeField(source="created_at")
    next = serializers.SerializerMethodField(help_text="The steps the officer may take now.")

    class Meta:
        model = SecurityDispatch
        fields = [
            "id",
            "status",
            "next",
            "incident",
            "sent_by",
            "sent_at",
            "note",
            "distance_m",
            "located_by",
            "accepted_at",
            "on_the_way_at",
            "arrived_at",
            "finished_at",
            "outcome",
            "cancel_reason",
        ]
        read_only_fields = fields

    def get_next(self, dispatch) -> list[str]:
        return list(services.NEXT.get(dispatch.status, ()))

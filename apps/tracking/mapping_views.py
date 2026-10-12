import json
import mimetypes

from django.db.models import Count, Q
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema
from rest_framework import generics, serializers, status
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core.permissions import Capability, has_capability, requires
from apps.geography.gps import GpsFixSerializer, GpsReadSerializer
from apps.geography.models import PollingUnit
from apps.geography.scope import LEVEL_FIELD, ScopeRef, any_of, scope_payload
from apps.results.collation import CHILD_LEVEL, PU_PATH, _pu_filter, _units_within

from . import mapping
from .models import PollingUnitPin
from .views import AREA_PARAMS, _area

C = Capability
MAPPERS = (C.CONFIRM_AGENT_LOCATION,)
MAPPING_VIEWERS = (C.CONFIRM_AGENT_LOCATION, C.VIEW_SITUATION_ROOM)
STATUSES = ("unmapped", "pinned", "confirmed", "conflict", "problem")


class MappingUnitSerializer(serializers.ModelSerializer):
    ward = serializers.CharField(source="ward.code")
    ward_name = serializers.CharField(source="ward.name")
    pins = serializers.IntegerField(source="pin_count", read_only=True)

    class Meta:
        model = PollingUnit
        fields = [
            "code",
            "name",
            "location",
            "ward",
            "ward_name",
            "mapping_status",
            "latitude",
            "longitude",
            "location_source",
            "location_accuracy_m",
            "location_note",
            "pins",
        ]


class PinSerializer(serializers.ModelSerializer):
    id = serializers.UUIDField(source="uuid", read_only=True)
    polling_unit = serializers.CharField(source="polling_unit.code")
    pinned_by = serializers.CharField(source="pinned_by.access_id")
    pinned_by_name = serializers.CharField(source="pinned_by.full_name")
    gps = GpsReadSerializer(source="*")
    has_photo = serializers.SerializerMethodField()

    class Meta:
        model = PollingUnitPin
        fields = [
            "id",
            "polling_unit",
            "status",
            "problem",
            "note",
            "gps",
            "has_photo",
            "pinned_by",
            "pinned_by_name",
            "created_at",
            "resolved_at",
        ]

    def get_has_photo(self, pin) -> bool:
        return bool(pin.photo)


class PinRequestSerializer(serializers.Serializer):
    gps = serializers.JSONField(help_text="GPS fix object, sent as JSON text in the form.")
    photo = serializers.FileField(help_text="Photo of the unit's signboard or building.")
    at_entrance = serializers.BooleanField()
    name_matches = serializers.BooleanField()
    note = serializers.CharField(required=False, allow_blank=True, max_length=1000)

    def validate_gps(self, value):
        if isinstance(value, str):
            value = json.loads(value)
        fix = GpsFixSerializer(data=value)
        fix.is_valid(raise_exception=True)
        return fix.validated_data


class ProblemSerializer(serializers.Serializer):
    problem = serializers.ChoiceField(choices=PollingUnitPin.Problem.choices)
    note = serializers.CharField(max_length=1000)


def _units_for(user):
    """Polling units in every area where the user may view mapping."""
    from apps.access.engine import areas_for

    conditions = [
        _pu_filter(ScopeRef.of(area))
        for permission in MAPPING_VIEWERS
        for area in areas_for(user, permission)
    ]
    return PollingUnit.objects.filter(any_of(*conditions)).select_related("ward")


class MappingUnitsView(generics.ListAPIView):
    """Polling units in your area with their mapping status."""

    serializer_class = MappingUnitSerializer
    permission_classes = [requires(*MAPPING_VIEWERS)]
    filterset_fields = {"mapping_status": ["exact"], "ward__code": ["exact"]}
    search_fields = ["code", "name", "location"]
    ordering_fields = ["code", "name"]

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return PollingUnit.objects.none()
        return _units_for(self.request.user).annotate(pin_count=Count("pins")).order_by("code")


def _unit(request, code):
    return get_object_or_404(_units_for(request.user), code=code)


class PinUnitView(APIView):
    """Pin a unit's location while standing at it (multipart: gps JSON + photo)."""

    permission_classes = [requires(*MAPPERS)]
    parser_classes = [MultiPartParser, FormParser]

    @extend_schema(
        request={"multipart/form-data": PinRequestSerializer}, responses={201: PinSerializer}
    )
    def post(self, request, code):
        serializer = PinRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        pin = mapping.pin_unit(
            actor=request.user, unit=_unit(request, code), **serializer.validated_data
        )
        return Response(PinSerializer(pin).data, status=status.HTTP_201_CREATED)


class ProblemView(APIView):
    """Report that a unit cannot be pinned (not found, moved, different name)."""

    permission_classes = [requires(*MAPPERS)]

    @extend_schema(request=ProblemSerializer, responses={201: PinSerializer})
    def post(self, request, code):
        serializer = ProblemSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        pin = mapping.report_problem(
            actor=request.user, unit=_unit(request, code), **serializer.validated_data
        )
        return Response(PinSerializer(pin).data, status=status.HTTP_201_CREATED)


class UnitPinsView(APIView):
    permission_classes = [requires(*MAPPING_VIEWERS)]

    @extend_schema(responses=PinSerializer(many=True))
    def get(self, request, code):
        pins = _unit(request, code).pins.select_related("pinned_by", "polling_unit")
        return Response(PinSerializer(pins, many=True).data)


def _pin(request, uuid):
    return get_object_or_404(
        PollingUnitPin.objects.select_related("polling_unit", "pinned_by").filter(
            polling_unit__in=_units_for(request.user)
        ),
        uuid=uuid,
    )


class AcceptPinView(APIView):
    """Resolve a conflict by accepting this pin as the unit's location."""

    permission_classes = [requires(*MAPPERS)]

    @extend_schema(request=None, responses=PinSerializer)
    def post(self, request, uuid):
        pin = mapping.accept_pin(actor=request.user, pin=_pin(request, uuid))
        return Response(PinSerializer(pin).data)


class PinPhotoView(APIView):
    permission_classes = [requires(*MAPPING_VIEWERS)]

    @extend_schema(responses={(200, "image/jpeg"): OpenApiTypes.BINARY})
    def get(self, request, uuid):
        pin = _pin(request, uuid)
        if not pin.photo:
            raise Http404
        response = FileResponse(
            pin.photo.open("rb"),
            content_type=mimetypes.guess_type(pin.photo.name)[0] or "image/jpeg",
        )
        response["Cache-Control"] = "private, no-store"
        return response


def _status_counts(queryset, group_by=None):
    counts = {f"{name}": Count("pk", filter=Q(mapping_status=name)) for name in STATUSES}
    # Located from INEC's locator and not yet checked by a coordinator (part of "pinned").
    counts["from_inec"] = Count("pk", filter=Q(mapping_status="pinned", location_source="inec"))
    if group_by is None:
        return queryset.aggregate(total=Count("pk"), **counts)
    return {
        row.pop(group_by): row
        for row in queryset.values(group_by).annotate(total=Count("pk"), **counts)
    }


def _with_percent(counts):
    total = counts.get("total") or 0
    mapped = (counts.get("pinned") or 0) + (counts.get("confirmed") or 0)
    return {
        **{key: counts.get(key) or 0 for key in ("total", *STATUSES, "from_inec")},
        "mapped": mapped,
        "mapped_percent": round(mapped * 100 / total, 1) if total else 0.0,
    }


class MappingProgressView(APIView):
    """How much of an area's polling units are mapped, with a child breakdown."""

    permission_classes = [requires(*MAPPING_VIEWERS)]

    @extend_schema(parameters=AREA_PARAMS, responses={200: OpenApiTypes.OBJECT})
    def get(self, request):
        held = next((p for p in MAPPING_VIEWERS if has_capability(request.user, p)), None)
        scope = _area(request, held or MAPPING_VIEWERS[0])
        units = PollingUnit.objects.filter(_pu_filter(scope))
        body = {"scope": scope_payload(scope), **_with_percent(_status_counts(units))}
        if scope.scope_level in CHILD_LEVEL and scope.scope_level != "ward":
            child_level = CHILD_LEVEL[scope.scope_level]
            path = PU_PATH[LEVEL_FIELD[child_level]]
            by_child = _status_counts(units, group_by=path)
            body["children"] = [
                {
                    "level": child_level,
                    "code": unit.code,
                    "name": unit.name,
                    **_with_percent(by_child.get(unit.pk, {})),
                }
                for unit in _units_within(child_level, scope).order_by("name")
            ]
        return Response(body)

"""Read-only reference geography for authenticated clients."""

from rest_framework import viewsets

from . import serializers
from .models import Lga, PollingUnit, SenatorialDistrict, State, Ward, Zone


class ZoneViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = Zone.objects.all()
    serializer_class = serializers.ZoneSerializer
    lookup_field = "code"
    pagination_class = None


class StateViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = State.objects.select_related("zone")
    serializer_class = serializers.StateSerializer
    lookup_field = "code"
    pagination_class = None
    filterset_fields = {"zone__code": ["exact"]}
    search_fields = ["name"]


class SenatorialDistrictViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = SenatorialDistrict.objects.select_related("state")
    serializer_class = serializers.SenatorialDistrictSerializer
    lookup_field = "code"
    # District codes contain slashes, e.g. SD/031/KD.
    lookup_value_regex = r"SD/\d{3}/[A-Z]+"
    pagination_class = None
    filterset_fields = {"state__code": ["exact"]}
    search_fields = ["name"]


class LgaViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = Lga.objects.select_related("state", "senatorial_district")
    serializer_class = serializers.LgaSerializer
    lookup_field = "code"
    filterset_fields = {
        "state__code": ["exact"],
        "state__zone__code": ["exact"],
        "senatorial_district__code": ["exact"],
    }
    search_fields = ["name"]


class WardViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = Ward.objects.select_related("lga")
    serializer_class = serializers.WardSerializer
    lookup_field = "code"
    filterset_fields = {"lga__code": ["exact"], "lga__state__code": ["exact"]}
    search_fields = ["name"]


class PollingUnitViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = PollingUnit.objects.select_related("ward")
    serializer_class = serializers.PollingUnitSerializer
    lookup_field = "code"
    filterset_fields = {
        "ward__code": ["exact"],
        "ward__lga__code": ["exact"],
        "ward__lga__state__code": ["exact"],
    }
    search_fields = ["name", "code"]

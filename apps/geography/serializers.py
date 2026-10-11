from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from .models import Lga, PollingUnit, SenatorialDistrict, State, Ward, Zone

# Coordinates are only offered for directions once the mapping exercise has
# placed the unit; a conflict or reported problem means they cannot be trusted.
NAVIGABLE_STATUSES = ("pinned", "confirmed")


def navigation_for(unit: PollingUnit):
    """Where to send someone travelling to [unit], or None if it is not mapped."""
    if unit.latitude is None or unit.longitude is None:
        return None
    if unit.mapping_status not in NAVIGABLE_STATUSES:
        return None
    return {
        "latitude": float(unit.latitude),
        "longitude": float(unit.longitude),
        "confirmed": unit.mapping_status == "confirmed",
        "accuracy_m": unit.location_accuracy_m,
        "source": unit.location_source,
    }


class NavigationSerializer(serializers.Serializer):
    latitude = serializers.FloatField()
    longitude = serializers.FloatField()
    confirmed = serializers.BooleanField(help_text="False until a second pin agrees.")
    accuracy_m = serializers.FloatField(allow_null=True)
    source = serializers.CharField(
        help_text="inec, coordinator, checkIn or imported: where the location came from."
    )


class ZoneSerializer(serializers.ModelSerializer):
    class Meta:
        model = Zone
        fields = ["code", "name"]


class StateSerializer(serializers.ModelSerializer):
    zone = serializers.SlugRelatedField(slug_field="code", read_only=True)
    zone_name = serializers.CharField(source="zone.name", read_only=True)

    class Meta:
        model = State
        fields = ["code", "name", "zone", "zone_name", "is_fct"]


class SenatorialDistrictSerializer(serializers.ModelSerializer):
    state = serializers.SlugRelatedField(slug_field="code", read_only=True)

    class Meta:
        model = SenatorialDistrict
        fields = ["code", "name", "state"]


class LgaSerializer(serializers.ModelSerializer):
    state = serializers.SlugRelatedField(slug_field="code", read_only=True)
    senatorial_district = serializers.SlugRelatedField(slug_field="code", read_only=True)

    class Meta:
        model = Lga
        fields = ["code", "slug", "name", "state", "senatorial_district", "inec_code"]


class WardSerializer(serializers.ModelSerializer):
    lga = serializers.SlugRelatedField(slug_field="code", read_only=True)

    class Meta:
        model = Ward
        fields = ["code", "name", "lga"]


class PollingUnitSerializer(serializers.ModelSerializer):
    ward = serializers.SlugRelatedField(slug_field="code", read_only=True)
    navigation = serializers.SerializerMethodField()

    class Meta:
        model = PollingUnit
        fields = [
            "code",
            "name",
            "location",
            "ward",
            "registered_voters",
            "latitude",
            "longitude",
            "mapping_status",
            "navigation",
        ]

    @extend_schema_field(NavigationSerializer(allow_null=True))
    def get_navigation(self, unit):
        return navigation_for(unit)

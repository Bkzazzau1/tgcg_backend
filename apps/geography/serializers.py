from rest_framework import serializers

from .models import Lga, PollingUnit, SenatorialDistrict, State, Ward, Zone


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
        fields = ["code", "slug", "name", "state", "senatorial_district"]


class WardSerializer(serializers.ModelSerializer):
    lga = serializers.SlugRelatedField(slug_field="code", read_only=True)

    class Meta:
        model = Ward
        fields = ["code", "name", "lga"]


class PollingUnitSerializer(serializers.ModelSerializer):
    ward = serializers.SlugRelatedField(slug_field="code", read_only=True)

    class Meta:
        model = PollingUnit
        fields = ["code", "name", "ward", "registered_voters", "latitude", "longitude"]

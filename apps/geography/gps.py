"""GPS fixes from the field apps and how they are judged against a polling unit."""

import math
from decimal import Decimal

from django.conf import settings
from django.db import models
from rest_framework import serializers

# Generous bounding box around Nigeria.
NIGERIA_LAT = (3.5, 14.5)
NIGERIA_LNG = (2.0, 15.0)
EARTH_RADIUS_M = 6_371_000


class LocationStatus(models.TextChoices):
    ON_SITE = "onSite", "At the polling unit"
    AWAY = "away", "Away from the polling unit"
    UNVERIFIED = "unverified", "Location could not be checked"
    SUSPECT = "suspect", "Location may be faked"
    NO_GPS = "noGps", "No GPS"


def distance_m(lat1, lng1, lat2, lng2) -> float:
    """Great-circle distance in metres (haversine)."""
    p1, p2 = math.radians(float(lat1)), math.radians(float(lat2))
    dp = p2 - p1
    dl = math.radians(float(lng2) - float(lng1))
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(a))


def assess(polling_unit, latitude, longitude, accuracy_m, is_mocked):
    """Judge a fix against a polling unit. Returns (status, distance_m or None)."""
    if latitude is None or longitude is None:
        return LocationStatus.NO_GPS, None
    if is_mocked:
        return LocationStatus.SUSPECT, None
    if polling_unit is None or polling_unit.latitude is None:
        return LocationStatus.UNVERIFIED, None
    distance = distance_m(latitude, longitude, polling_unit.latitude, polling_unit.longitude)
    if accuracy_m is None or accuracy_m > settings.GPS_MAX_ACCURACY_M:
        return LocationStatus.UNVERIFIED, round(distance)
    # Give the fix the benefit of its own accuracy radius.
    if distance - accuracy_m > settings.PU_PROXIMITY_METERS:
        return LocationStatus.AWAY, round(distance)
    return LocationStatus.ON_SITE, round(distance)


class GpsCheckFields(models.Model):
    """How a record's GPS fix was captured and judged (latitude/longitude live on the model)."""

    gps_accuracy_m = models.FloatField(null=True, blank=True)
    gps_captured_at = models.DateTimeField(null=True, blank=True)
    gps_is_mocked = models.BooleanField(default=False)
    distance_to_pu_m = models.PositiveIntegerField(null=True, blank=True)
    location_status = models.CharField(
        max_length=12, choices=LocationStatus.choices, default=LocationStatus.NO_GPS
    )

    class Meta:
        abstract = True

    def apply_fix(self, fix: dict | None, polling_unit):
        fix = fix or {}
        self.latitude = fix.get("latitude")
        self.longitude = fix.get("longitude")
        self.gps_accuracy_m = fix.get("accuracy_m")
        self.gps_captured_at = fix.get("captured_at")
        self.gps_is_mocked = bool(fix.get("is_mocked"))
        self.location_status, self.distance_to_pu_m = assess(
            polling_unit, self.latitude, self.longitude, self.gps_accuracy_m, self.gps_is_mocked
        )


class GpsFixSerializer(serializers.Serializer):
    """One reading from the phone's GPS (geolocator Position)."""

    latitude = serializers.DecimalField(max_digits=9, decimal_places=6)
    longitude = serializers.DecimalField(max_digits=9, decimal_places=6)
    accuracy_m = serializers.FloatField(min_value=0, max_value=100_000)
    captured_at = serializers.DateTimeField()
    is_mocked = serializers.BooleanField(
        default=False, help_text="Android reports a mock-location app is active."
    )

    def validate(self, attrs):
        lat, lng = attrs["latitude"], attrs["longitude"]
        if not (
            Decimal(str(NIGERIA_LAT[0])) <= lat <= Decimal(str(NIGERIA_LAT[1]))
            and Decimal(str(NIGERIA_LNG[0])) <= lng <= Decimal(str(NIGERIA_LNG[1]))
        ):
            raise serializers.ValidationError("The location is outside Nigeria.")
        return attrs


class GpsReadSerializer(serializers.Serializer):
    """GPS block shown on a record."""

    latitude = serializers.DecimalField(max_digits=9, decimal_places=6, allow_null=True)
    longitude = serializers.DecimalField(max_digits=9, decimal_places=6, allow_null=True)
    accuracy_m = serializers.FloatField(source="gps_accuracy_m", allow_null=True)
    captured_at = serializers.DateTimeField(source="gps_captured_at", allow_null=True)
    is_mocked = serializers.BooleanField(source="gps_is_mocked")
    distance_to_pu_m = serializers.IntegerField(allow_null=True)
    status = serializers.ChoiceField(source="location_status", choices=LocationStatus.choices)

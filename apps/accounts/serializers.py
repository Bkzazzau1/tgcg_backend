from rest_framework import serializers
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer

from .models import User


def scope_payload(user: User) -> dict:
    """User scope in the shape of the Flutter `GeographicScope` class."""

    def part(obj, attr):
        return getattr(obj, attr) if obj is not None else None

    return {
        "level": user.scope_level,
        "country": "Nigeria",
        "zoneId": part(user.zone, "code"),
        "zoneName": part(user.zone, "name"),
        "stateId": part(user.state, "code"),
        "stateName": part(user.state, "name"),
        "senatorialDistrictId": part(user.senatorial_district, "code"),
        "senatorialDistrictName": part(user.senatorial_district, "name"),
        "lgaId": part(user.lga, "code"),
        "lgaName": part(user.lga, "name"),
        "wardId": part(user.ward, "code"),
        "wardName": part(user.ward, "name"),
        "pollingUnitId": part(user.polling_unit, "code"),
        "pollingUnitName": part(user.polling_unit, "name"),
    }


class UserProfileSerializer(serializers.ModelSerializer):
    scope = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = ["access_id", "full_name", "phone_number", "email", "role", "scope"]
        read_only_fields = fields

    def get_scope(self, user) -> dict:
        return scope_payload(user)


class LoginSerializer(TokenObtainPairSerializer):
    """Sign in with an access ID or phone number ("username") and password."""

    username_field = "username"

    def validate(self, attrs):
        data = super().validate(attrs)
        data["user"] = UserProfileSerializer(self.user).data
        return data


class LogoutSerializer(serializers.Serializer):
    refresh = serializers.CharField()

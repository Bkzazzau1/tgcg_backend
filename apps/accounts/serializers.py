from rest_framework import serializers
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer

from apps.core.permissions import capabilities_for
from apps.geography.scope import ScopeField

from .models import User


class UserProfileSerializer(serializers.ModelSerializer):
    scope = ScopeField(read_only=True)
    capabilities = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = [
            "access_id",
            "full_name",
            "phone_number",
            "email",
            "role",
            "scope",
            "capabilities",
        ]
        read_only_fields = fields

    def get_capabilities(self, user) -> list[str]:
        return sorted(capabilities_for(user))


class LoginSerializer(TokenObtainPairSerializer):
    """Sign in with an access ID or phone number ("username") and password."""

    username_field = "username"

    def validate(self, attrs):
        data = super().validate(attrs)
        data["user"] = UserProfileSerializer(self.user).data
        return data


class LogoutSerializer(serializers.Serializer):
    refresh = serializers.CharField()

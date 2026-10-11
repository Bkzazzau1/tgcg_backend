from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer

from apps.access.engine import active_grants
from apps.access.serializers import GrantSerializer
from apps.core.permissions import capabilities_for
from apps.geography.scope import ScopeField

from .models import User


class UserProfileSerializer(serializers.ModelSerializer):
    scope = ScopeField(read_only=True)
    capabilities = serializers.SerializerMethodField()
    access = serializers.SerializerMethodField(
        help_text="Where the access comes from: each appointment, or a legacy role."
    )
    membership_number = serializers.CharField(
        source="member.membership_number",
        read_only=True,
        default=None,
        help_text="The member behind this account, if any.",
    )

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
            "access",
            "membership_number",
        ]
        read_only_fields = fields

    def get_capabilities(self, user) -> list[str]:
        return sorted(capabilities_for(user))

    @extend_schema_field(GrantSerializer(many=True))
    def get_access(self, user) -> list:
        return GrantSerializer(active_grants(user), many=True).data


class LoginSerializer(TokenObtainPairSerializer):
    """Sign in with an access ID or phone number ("username") and password."""

    username_field = "username"

    def validate(self, attrs):
        data = super().validate(attrs)
        data["user"] = UserProfileSerializer(self.user).data
        return data


class LogoutSerializer(serializers.Serializer):
    refresh = serializers.CharField()

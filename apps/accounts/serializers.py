from django.contrib.auth.models import update_last_login
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
    supervises = serializers.SerializerMethodField(
        help_text="Holds a role that other roles report to (supervision of their appointments)."
    )
    identity_verified = serializers.SerializerMethodField(
        help_text="The member behind this account has a verified identity. Membership needs "
        "none; agents, observers and appointed roles act only when it is."
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
            "supervises",
            "identity_verified",
        ]
        read_only_fields = fields

    def get_capabilities(self, user) -> list[str]:
        return sorted(capabilities_for(user))

    @extend_schema_field(GrantSerializer(many=True))
    def get_access(self, user) -> list:
        return GrantSerializer(active_grants(user), many=True).data

    def get_supervises(self, user) -> bool:
        from apps.access.services import supervises_any

        return supervises_any(user)

    def get_identity_verified(self, user) -> bool:
        from apps.access.services import identity_verified

        return identity_verified(user)


class LoginSerializer(TokenObtainPairSerializer):
    """Sign in with an access ID or phone number ("username") and password."""

    username_field = "username"

    def validate(self, attrs):
        data = super().validate(attrs)
        first = self.user.last_login is None
        update_last_login(None, self.user)
        member = getattr(self.user, "member", None)
        if first and member is not None:
            # A member's first sign-in earns credits for whoever registered them.
            from apps.wallets.services import reward_registration_quietly

            reward_registration_quietly(member, proof="signIn")
        data["user"] = UserProfileSerializer(self.user).data
        return data


class LogoutSerializer(serializers.Serializer):
    refresh = serializers.CharField()


class PasswordChangeSerializer(serializers.Serializer):
    """The current password or PIN, and the new one (a member's: 6 digits)."""

    current_password = serializers.CharField(write_only=True, trim_whitespace=False)
    new_password = serializers.CharField(write_only=True, trim_whitespace=False, max_length=128)

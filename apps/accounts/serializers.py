import logging

from django.contrib.auth.models import update_last_login
from django.db import transaction
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers
from rest_framework.exceptions import APIException, AuthenticationFailed
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer, TokenObtainSerializer
from rest_framework_simplejwt.settings import api_settings

from apps.access.engine import active_grants
from apps.access.serializers import GrantSerializer
from apps.core.permissions import capabilities_for
from apps.geography.scope import ScopeField

from .models import User

logger = logging.getLogger(__name__)


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
    must_change_pin = serializers.BooleanField(
        source="must_change_password",
        read_only=True,
        help_text="Signed in with a first password others know: nothing else works until "
        "the account chooses its own PIN at auth/password/.",
    )
    first_pin = serializers.SerializerMethodField(
        help_text='While must_change_pin: what that first password is, "phone number", '
        '"phone number with its country code" (abroad: its digits, without the +), "VIN" '
        'or "NIN" (never the password itself).'
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
            "must_change_pin",
            "first_pin",
        ]
        read_only_fields = fields

    def get_capabilities(self, user) -> list[str]:
        return sorted(capabilities_for(user))

    def get_first_pin(self, user) -> str | None:
        """As the first password was chosen, on a list or at a reset: the phone,
        else the VIN, else the NIN (apps.membership.services.first_pin_for)."""
        from apps.membership.services import first_pin_for

        if not user.must_change_password:
            return None
        return first_pin_for(user, getattr(user, "member", None))[1]

    @extend_schema_field(GrantSerializer(many=True))
    def get_access(self, user) -> list:
        return GrantSerializer(active_grants(user), many=True).data

    def get_supervises(self, user) -> bool:
        from apps.access.services import supervises_any

        return supervises_any(user)

    def get_identity_verified(self, user) -> bool:
        from apps.access.services import identity_verified

        return identity_verified(user)


class SignInUnavailable(APIException):
    status_code = 503
    default_detail = "Sign-in could not be completed. Please try again."
    default_code = "sign_in_unavailable"


class LoginSerializer(TokenObtainPairSerializer):
    """Sign in with an access ID or phone number ("username") and password."""

    username_field = "username"

    def validate(self, attrs):
        # Failed-password counters must persist outside the sign-in transaction.
        data = TokenObtainSerializer.validate(self, attrs)
        try:
            with transaction.atomic():
                self.user = User.objects.select_for_update().get(pk=self.user.pk)
                if not api_settings.USER_AUTHENTICATION_RULE(self.user):
                    raise AuthenticationFailed(
                        self.error_messages["no_active_account"], "no_active_account"
                    )
                first = self.user.last_login is None
                member = getattr(self.user, "member", None)
                if first and member is not None:
                    from apps.wallets.services import reward_registration

                    reward_registration(member, proof="signIn")
                update_last_login(None, self.user)
                refresh = self.get_token(self.user)
                data["refresh"] = str(refresh)
                data["access"] = str(refresh.access_token)
                data["user"] = UserProfileSerializer(self.user).data
        except AuthenticationFailed:
            raise
        except Exception as exc:
            logger.error("Could not finish sign-in: %s", type(exc).__name__)
            raise SignInUnavailable from exc
        return data


class LogoutSerializer(serializers.Serializer):
    refresh = serializers.CharField()


class PasswordChangeSerializer(serializers.Serializer):
    """The current password or PIN, and the new one (a member's: 6 digits)."""

    current_password = serializers.CharField(write_only=True, trim_whitespace=False)
    new_password = serializers.CharField(write_only=True, trim_whitespace=False, max_length=128)

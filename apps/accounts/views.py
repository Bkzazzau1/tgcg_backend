from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.db import transaction
from drf_spectacular.utils import extend_schema, inline_serializer
from rest_framework import generics, serializers, status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.token_blacklist.models import BlacklistedToken, OutstandingToken
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.views import TokenObtainPairView

from apps.access import audit
from apps.core.choices import Role

from .serializers import (
    LoginSerializer,
    LogoutSerializer,
    PasswordChangeSerializer,
    UserProfileSerializer,
)

SESSION_TOKENS = inline_serializer(
    "SessionTokens", {"access": serializers.CharField(), "refresh": serializers.CharField()}
)


class LoginView(TokenObtainPairView):
    serializer_class = LoginSerializer
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "login"


class LogoutView(APIView):
    """Revoke a refresh token so it cannot mint new access tokens."""

    permission_classes = [IsAuthenticated]

    @extend_schema(request=LogoutSerializer, responses={205: None})
    def post(self, request):
        serializer = LogoutSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            RefreshToken(serializer.validated_data["refresh"]).blacklist()
        except TokenError:
            return Response(
                {"detail": "Token is invalid or already revoked."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return Response(status=status.HTTP_205_RESET_CONTENT)


class MeView(generics.RetrieveAPIView):
    serializer_class = UserProfileSerializer

    def get_object(self):
        return self.request.user


def _check_new_password(user, new: str) -> None:
    """Members sign in with a 6-digit PIN; staff with a password the
    validators accept."""
    if user.role == Role.MEMBER:
        from apps.membership.registration import validate_pin

        try:
            validate_pin(new)
        except ValidationError as exc:
            raise ValidationError({"new_password": exc.message_dict["pin"]}) from exc
    else:
        try:
            validate_password(new, user)
        except ValidationError as exc:
            raise ValidationError({"new_password": exc.messages}) from exc


class PasswordChangeView(APIView):
    """Change one's own password, or a member's 6-digit PIN, while signed in.

    Members given their phone number as a first password change it here.
    Every other session ends, as their refresh tokens are revoked; this device
    gets new tokens.
    """

    permission_classes = [IsAuthenticated]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "login"

    @extend_schema(request=PasswordChangeSerializer, responses={200: SESSION_TOKENS})
    def post(self, request):
        serializer = PasswordChangeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = request.user
        current = serializer.validated_data["current_password"]
        new = serializer.validated_data["new_password"]
        if not user.check_password(current):
            raise ValidationError({"current_password": "That is not your current PIN or password."})
        if new == current:
            raise ValidationError({"new_password": "Choose one different from the current one."})
        _check_new_password(user, new)
        with transaction.atomic():
            user.set_password(new)
            user.save(update_fields=["password"])
            for token in OutstandingToken.objects.filter(user=user):
                BlacklistedToken.objects.get_or_create(token=token)
            audit.record(
                actor=user,
                action="account.passwordChanged",
                target_type="user",
                target_id=user.access_id,
                target_label=user.full_name,
            )
        refresh = RefreshToken.for_user(user)
        return Response({"access": str(refresh.access_token), "refresh": str(refresh)})

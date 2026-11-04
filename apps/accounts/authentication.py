"""Signed-in requests, refused while the account must choose its own PIN.

A member from a group's list signs in first with their VIN as the password
(4 November 2026), which whoever holds the list knows. Until they choose
their own PIN, the server answers nothing but who they are, changing the
PIN and signing out, whatever the app asks.
"""

from django.urls import reverse
from rest_framework.exceptions import PermissionDenied
from rest_framework_simplejwt.authentication import JWTAuthentication

# Named routes still open before the PIN is changed.
OPEN_FIRST = ("auth-me", "auth-password", "auth-logout")


class PinChangeRequired(PermissionDenied):
    """403 with `code` beside `detail`, so an app tells it from any refusal."""

    default_detail = "Choose your own PIN first."
    default_code = "pin_change_required"

    def __init__(self):
        super().__init__({"detail": self.default_detail, "code": self.default_code})


class FirstPinJWTAuthentication(JWTAuthentication):
    def authenticate(self, request):
        result = super().authenticate(request)
        if result is None:
            return None
        user, token = result
        if user.must_change_password and request.path not in {reverse(n) for n in OPEN_FIRST}:
            raise PinChangeRequired()
        return user, token

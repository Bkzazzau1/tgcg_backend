from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.backends import ModelBackend
from django.utils import timezone

from .phone import normalize_ng_phone


class AccessIdOrPhoneBackend(ModelBackend):
    """Authenticate with an access ID (case-insensitive) or a Nigerian phone number.

    Members sign in with phone + 6-digit PIN (stored as their password), so
    repeated failures lock the account for a while to stop PIN guessing.
    """

    def authenticate(self, request, username=None, password=None, **kwargs):
        User = get_user_model()
        if username is None:
            username = kwargs.get(User.USERNAME_FIELD)
        if not username or password is None:
            return None

        identifier = username.strip()
        phone = normalize_ng_phone(identifier)
        user = None
        if phone:
            user = User.objects.filter(phone_number=phone).first()
        if user is None:
            user = User.objects.filter(access_id=identifier.upper()).first()

        if user is None:
            # Run the hasher anyway so timing does not reveal unknown accounts.
            User().set_password(password)
            return None

        now = timezone.now()
        if user.locked_until and user.locked_until > now:
            user.check_password(password)  # constant work while locked
            return None

        if user.check_password(password) and self.user_can_authenticate(user):
            if user.failed_login_attempts or user.locked_until:
                user.failed_login_attempts = 0
                user.locked_until = None
                user.save(update_fields=["failed_login_attempts", "locked_until"])
            return user

        user.failed_login_attempts += 1
        fields = ["failed_login_attempts"]
        if user.failed_login_attempts >= settings.LOGIN_MAX_FAILED_ATTEMPTS:
            user.locked_until = now + timedelta(minutes=settings.LOGIN_LOCKOUT_MINUTES)
            user.failed_login_attempts = 0
            fields.append("locked_until")
        user.save(update_fields=fields)
        return None

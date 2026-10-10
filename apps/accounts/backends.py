from django.contrib.auth import get_user_model
from django.contrib.auth.backends import ModelBackend

from .phone import normalize_ng_phone


class AccessIdOrPhoneBackend(ModelBackend):
    """Authenticate with an access ID (case-insensitive) or a Nigerian phone number."""

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
        if user.check_password(password) and self.user_can_authenticate(user):
            return user
        return None

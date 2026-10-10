from django.contrib.auth.models import AbstractBaseUser, BaseUserManager, PermissionsMixin
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone

from apps.core.choices import Role
from apps.geography.scope import ScopedModel

from .phone import normalize_ng_phone


class UserManager(BaseUserManager):
    use_in_migrations = True

    def _create_user(self, access_id, password, **extra):
        if not access_id:
            raise ValueError("An access ID is required.")
        user = self.model(access_id=access_id.strip().upper(), **extra)
        user.set_password(password)
        user.full_clean(exclude=["password"])
        user.save(using=self._db)
        return user

    def create_user(self, access_id, password=None, **extra):
        extra.setdefault("is_staff", False)
        extra.setdefault("is_superuser", False)
        return self._create_user(access_id, password, **extra)

    def create_superuser(self, access_id, password=None, **extra):
        extra.setdefault("is_staff", True)
        extra.setdefault("is_superuser", True)
        extra.setdefault("role", Role.NATIONAL_ADMINISTRATOR)
        if not extra["is_staff"] or not extra["is_superuser"]:
            raise ValueError("Superuser must have is_staff and is_superuser set.")
        return self._create_user(access_id, password, **extra)


class User(AbstractBaseUser, PermissionsMixin, ScopedModel):
    """An operator of the TGCG platform with a role and a geographic scope.

    Signs in with an access ID or phone number, matching the Flutter login.
    """

    access_id = models.CharField(
        max_length=32,
        unique=True,
        help_text="Issued access ID, e.g. TGCG-KD-0001. Stored upper-case.",
    )
    phone_number = models.CharField(
        max_length=16,
        unique=True,
        null=True,
        blank=True,
        help_text="Nigerian mobile number, stored as +234XXXXXXXXXX.",
    )
    full_name = models.CharField(max_length=150)
    email = models.EmailField(blank=True)
    role = models.CharField(max_length=32, choices=Role.choices, default=Role.OBSERVER)

    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)
    date_joined = models.DateTimeField(default=timezone.now)

    objects = UserManager()

    USERNAME_FIELD = "access_id"
    REQUIRED_FIELDS = ["full_name"]

    class Meta:
        ordering = ["full_name"]

    def __str__(self):
        return f"{self.full_name} ({self.access_id})"

    def get_full_name(self):
        return self.full_name

    def get_short_name(self):
        return self.full_name.split(" ")[0] if self.full_name else self.access_id

    def clean(self):
        super().clean()
        self.access_id = (self.access_id or "").strip().upper()
        if self.phone_number:
            normalized = normalize_ng_phone(self.phone_number)
            if normalized is None:
                raise ValidationError({"phone_number": "Enter a valid Nigerian mobile number."})
            self.phone_number = normalized
        else:
            self.phone_number = None

        self.normalize_scope()
        self.validate_role_scope(self.role)

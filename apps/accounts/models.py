from django.contrib.auth.models import AbstractBaseUser, BaseUserManager, PermissionsMixin
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone

from apps.core.choices import GeographyLevel, Role

from .phone import normalize_ng_phone

# The least specific scope each role must be assigned to. Roles not listed
# work nationally or at any level.
ROLE_SCOPE_LEVEL = {
    Role.ZONAL_COORDINATOR: GeographyLevel.GEOPOLITICAL_ZONE,
    Role.STATE_COORDINATOR: GeographyLevel.STATE,
    Role.LGA_COORDINATOR: GeographyLevel.LGA,
    Role.WARD_COORDINATOR: GeographyLevel.WARD,
    Role.POLLING_UNIT_AGENT: GeographyLevel.POLLING_UNIT,
}

# Scope field that must be set for each level (ancestors are derived).
LEVEL_FIELD = {
    GeographyLevel.GEOPOLITICAL_ZONE: "zone",
    GeographyLevel.STATE: "state",
    GeographyLevel.SENATORIAL_DISTRICT: "senatorial_district",
    GeographyLevel.LGA: "lga",
    GeographyLevel.WARD: "ward",
    GeographyLevel.POLLING_UNIT: "polling_unit",
}


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


class User(AbstractBaseUser, PermissionsMixin):
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

    scope_level = models.CharField(
        max_length=24,
        choices=GeographyLevel.choices,
        default=GeographyLevel.COUNTRY,
    )
    zone = models.ForeignKey(
        "geography.Zone", on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    state = models.ForeignKey(
        "geography.State", on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    senatorial_district = models.ForeignKey(
        "geography.SenatorialDistrict",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
    )
    lga = models.ForeignKey(
        "geography.Lga", on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    ward = models.ForeignKey(
        "geography.Ward", on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    polling_unit = models.ForeignKey(
        "geography.PollingUnit",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
    )

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

    # --- scope -----------------------------------------------------------

    def _derive_scope_ancestors(self):
        """Fill parent units from the most specific one that is set."""
        if self.polling_unit_id:
            self.ward = self.polling_unit.ward
        if self.ward_id:
            self.lga = self.ward.lga
        if self.lga_id:
            self.senatorial_district = self.lga.senatorial_district
            self.state = self.lga.state
        if self.senatorial_district_id:
            self.state = self.senatorial_district.state
        if self.state_id:
            self.zone = self.state.zone

    def _clear_scope_below_level(self):
        order = list(LEVEL_FIELD)
        if self.scope_level == GeographyLevel.COUNTRY:
            deeper = order
        else:
            deeper = order[order.index(self.scope_level) + 1 :]
        for level in deeper:
            setattr(self, LEVEL_FIELD[level], None)

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

        if self.scope_level != GeographyLevel.COUNTRY:
            field = LEVEL_FIELD[self.scope_level]
            if getattr(self, f"{field}_id") is None:
                raise ValidationError(
                    {field: f"Required for a {self.get_scope_level_display().lower()} scope."}
                )
        self._derive_scope_ancestors()
        self._clear_scope_below_level()

        required = ROLE_SCOPE_LEVEL.get(self.role)
        if required and self.scope_level != required:
            raise ValidationError(
                {
                    "scope_level": (
                        f"A {self.get_role_display().lower()} must be scoped to a "
                        f"{GeographyLevel(required).label.lower()}."
                    )
                }
            )

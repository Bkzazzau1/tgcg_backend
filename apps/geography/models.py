"""Canonical Nigerian electoral geography.

Codes match the Flutter client's GeographyRegistry exactly (state `KD`,
district `SD/031/KD`, LGA `KD-KADUNA-NORTH`) so records sync without mapping.
"""

from django.db import models


class Zone(models.Model):
    code = models.CharField(max_length=4, unique=True)
    name = models.CharField(max_length=64, unique=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class State(models.Model):
    code = models.CharField(max_length=4, unique=True)
    name = models.CharField(max_length=64, unique=True)
    zone = models.ForeignKey(Zone, on_delete=models.PROTECT, related_name="states")
    is_fct = models.BooleanField(default=False)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class SenatorialDistrict(models.Model):
    code = models.CharField(max_length=16, unique=True)
    name = models.CharField(max_length=96)
    state = models.ForeignKey(State, on_delete=models.PROTECT, related_name="senatorial_districts")

    class Meta:
        ordering = ["code"]

    def __str__(self):
        return self.name


class Lga(models.Model):
    code = models.CharField(max_length=48, unique=True)
    slug = models.SlugField(max_length=48)
    name = models.CharField(max_length=96)
    state = models.ForeignKey(State, on_delete=models.PROTECT, related_name="lgas")
    senatorial_district = models.ForeignKey(
        SenatorialDistrict, on_delete=models.PROTECT, related_name="lgas"
    )
    inec_code = models.CharField(  # noqa: DJ001 - NULL until polling units are loaded
        max_length=8,
        unique=True,
        null=True,
        blank=True,
        help_text="INEC state-LGA code, e.g. 18-23 for Zaria.",
    )

    class Meta:
        ordering = ["state__name", "name"]
        verbose_name = "LGA"
        verbose_name_plural = "LGAs"
        constraints = [
            models.UniqueConstraint(fields=["state", "slug"], name="unique_lga_slug_per_state"),
        ]

    def __str__(self):
        return f"{self.name} ({self.state.code})"


class Ward(models.Model):
    code = models.CharField(max_length=64, unique=True, help_text="INEC ward code, e.g. 18-23-01.")
    name = models.CharField(max_length=128)
    lga = models.ForeignKey(Lga, on_delete=models.PROTECT, related_name="wards")

    class Meta:
        ordering = ["lga__name", "name"]

    def __str__(self):
        return self.name


class PollingUnit(models.Model):
    code = models.CharField(
        max_length=64, unique=True, help_text="INEC polling unit code, e.g. 18-23-01-001."
    )
    name = models.CharField(max_length=255)
    location = models.CharField(max_length=255, blank=True, help_text="INEC location note.")
    ward = models.ForeignKey(Ward, on_delete=models.PROTECT, related_name="polling_units")
    registered_voters = models.PositiveIntegerField(null=True, blank=True)
    latitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    longitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    location_source = models.CharField(
        max_length=16,
        choices=[
            ("checkIn", "Confirmed agent check-in"),
            ("coordinator", "Set by a coordinator"),
            ("imported", "Imported"),
        ],
        blank=True,
    )
    location_accuracy_m = models.FloatField(null=True, blank=True)
    mapping_status = models.CharField(
        max_length=12,
        choices=[
            ("unmapped", "Not yet mapped"),
            ("pinned", "Pinned once"),
            ("confirmed", "Confirmed by a second pin"),
            ("conflict", "Pins disagree"),
            ("problem", "Reported problem"),
        ],
        default="unmapped",
        db_index=True,
    )
    location_confirmed_by = models.ForeignKey(
        "accounts.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    location_confirmed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["code"]

    def __str__(self):
        return f"{self.code} {self.name}"

"""Members in the diaspora (3 November 2026): people living abroad who join
on their own form. They give the country and city where they live, an email
and a phone from any country, and their home in Nigeria: their polling unit
from a PVC, or without one their LGA of origin and, if they know it, their
ward. They count under that home like anyone; the Members & Exco page also
counts them by country. Members registered with Nigerian details stay as
they are.
"""

from django.core.exceptions import ValidationError
from django.db import models

from apps.core.models import TimeStampedModel

from .countries import NAMES


class DiasporaProfile(TimeStampedModel):
    """Where a member in the diaspora lives."""

    member = models.OneToOneField(
        "membership.Member", on_delete=models.CASCADE, related_name="diaspora"
    )
    country = models.CharField(max_length=2, db_index=True, help_text="ISO 3166-1 alpha-2 code.")
    city = models.CharField(max_length=100)
    address = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ["country", "city"]

    def __str__(self):
        return f"{self.member} in {self.city}, {NAMES.get(self.country, self.country)}"

    def clean(self):
        super().clean()
        self.country = (self.country or "").strip().upper()
        self.city = " ".join((self.city or "").split())
        self.address = " ".join((self.address or "").split())
        errors = {}
        if self.country not in NAMES:
            errors["country"] = "Choose the country where you live."
        if not self.city:
            errors["city"] = "Enter the city or town where you live."
        if errors:
            raise ValidationError(errors)

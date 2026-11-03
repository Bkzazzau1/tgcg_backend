"""Joining from the diaspora: what the form gives, checked before anything
is saved, and the counts by country."""

from django.core.exceptions import ValidationError
from django.db.models import Count

from apps.core.choices import GeographyLevel
from apps.geography.models import Lga, Ward

from .countries import NAMES
from .models import DiasporaProfile

# The registration form's field for each detail, so a mistake shows by it.
FIELDS = {
    "country": "residence_country",
    "city": "residence_city",
    "address": "residence_address",
}


def clean_residence(*, country="", city="", address="") -> dict:
    """Where the member lives abroad, as it will be kept."""
    profile = DiasporaProfile(country=country, city=city, address=address)
    try:
        profile.clean()
        profile.clean_fields(exclude=["member"])
    except ValidationError as error:
        raise ValidationError(
            {FIELDS[field]: messages for field, messages in error.message_dict.items()}
        ) from None
    return {"country": profile.country, "city": profile.city, "address": profile.address}


def home_place(*, lga="", ward="") -> dict:
    """The member's home without a PVC, as scope fields: their ward when they
    know it, or else their LGA of origin."""
    lga = (lga or "").strip()
    ward = (ward or "").strip()
    if not lga:
        raise ValidationError(
            {"home_lga": "Choose your state and LGA of origin, or give your PVC details."}
        )
    found = Lga.objects.filter(code=lga).first()
    if found is None:
        raise ValidationError({"home_lga": "That LGA was not found. Choose it again."})
    if not ward:
        return {"scope_level": GeographyLevel.LGA, "lga": found}
    in_lga = Ward.objects.filter(code=ward, lga=found).first()
    if in_lga is None:
        raise ValidationError({"home_ward": "That ward is not in your LGA. Choose it again."})
    return {"scope_level": GeographyLevel.WARD, "ward": in_lga}


def record(member, residence: dict) -> DiasporaProfile:
    return DiasporaProfile.objects.create(member=member, **residence)


def summary(members) -> dict:
    """How many of `members` live abroad, and how many in each country, the
    most first."""
    rows = (
        members.filter(diaspora__isnull=False)
        .values_list("diaspora__country")
        .annotate(n=Count("pk"))
        .order_by("-n", "diaspora__country")
    )
    countries = [{"country": code, "name": NAMES.get(code, code), "count": n} for code, n in rows]
    return {"total": sum(row["count"] for row in countries), "countries": countries}

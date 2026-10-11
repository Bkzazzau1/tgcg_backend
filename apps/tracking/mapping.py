"""Polling-unit mapping: coordinators pin each unit's location on site.

The first good pin sets the unit's location; a second coordinator's pin
within PIN_CONFIRM_RADIUS_M confirms it; a pin further away is a conflict
that another coordinator resolves. Locations from INEC's Polling Unit Locator,
imports and confirmed check-ins are treated like a first pin, so one
coordinator on site is enough to confirm them.
"""

from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from apps.core.permissions import Capability
from apps.geography.gps import distance_m
from apps.geography.models import PollingUnit
from apps.geography.scope import ScopeRef, scope_contains
from apps.membership.registration import validate_image

from .models import PollingUnitPin

Pin = PollingUnitPin


def require_unit_in_scope(actor, unit: PollingUnit):
    unit_scope = ScopeRef.resolve("pollingUnit", unit.code)
    if not scope_contains(actor, unit_scope, permission=Capability.CONFIRM_AGENT_LOCATION):
        raise PermissionDenied("This polling unit is outside your operational scope.")


def _lock(unit):
    return PollingUnit.objects.select_for_update().get(pk=unit.pk)


def _set_location(unit, pin, actor, status):
    unit.latitude, unit.longitude = pin.latitude, pin.longitude
    unit.location_source = "coordinator"
    unit.location_accuracy_m = pin.gps_accuracy_m
    unit.location_confirmed_by = actor
    unit.location_confirmed_at = timezone.now()
    unit.mapping_status = status
    unit.save(
        update_fields=[
            "latitude",
            "longitude",
            "location_source",
            "location_accuracy_m",
            "location_confirmed_by",
            "location_confirmed_at",
            "mapping_status",
        ]
    )


@transaction.atomic
def pin_unit(*, actor, unit, gps, photo, at_entrance, name_matches, note="") -> Pin:
    """Record a coordinator standing at the unit. Raises ValidationError with
    guidance when the pin does not meet the rules."""
    require_unit_in_scope(actor, unit)
    if not at_entrance:
        raise ValidationError(
            {"at_entrance": "Stand at the unit's main entrance or voting point before pinning."}
        )
    if not name_matches:
        raise ValidationError(
            {"name_matches": "The signboard does not match: report a problem instead of pinning."}
        )
    if not gps:
        raise ValidationError({"gps": "A GPS reading is required."})
    if gps.get("is_mocked"):
        raise ValidationError({"gps": "A fake-location app is active. Turn it off and try again."})
    accuracy = gps.get("accuracy_m")
    if accuracy is None or accuracy > settings.PIN_MAX_ACCURACY_M:
        raise ValidationError(
            {
                "gps": f"GPS accuracy is {round(accuracy or 0)} m; it must be "
                f"{settings.PIN_MAX_ACCURACY_M} m or better. Move outdoors, away from "
                "walls and roofs, and wait a few seconds."
            }
        )
    validate_image(photo, "photo", required=True)

    unit = _lock(unit)
    pin = Pin(
        polling_unit=unit,
        pinned_by=actor,
        photo=photo,
        at_entrance=True,
        name_matches=True,
        note=(note or "").strip(),
    )
    pin.apply_fix(gps, unit)

    if unit.latitude is None:
        pin.status = Pin.Status.PINNED
        pin.save()
        _set_location(unit, pin, actor, "pinned")
        return pin

    distance = distance_m(pin.latitude, pin.longitude, unit.latitude, unit.longitude)
    pin.distance_to_pu_m = round(distance)
    radius = settings.PIN_CONFIRM_RADIUS_M
    if unit.location_source == "inec":
        # INEC rounds some coordinates (location_accuracy_m holds by how much),
        # so a pin at the right spot can look further away than it is.
        radius += unit.location_accuracy_m or 0
    if distance <= radius:
        pin.status = Pin.Status.CONFIRMED
        pin.save()
        # A confirmation must come from a different person than who set it.
        if unit.location_confirmed_by_id != actor.pk and unit.mapping_status in (
            "pinned",
            "problem",
        ):
            unit.mapping_status = "confirmed"
            unit.save(update_fields=["mapping_status"])
        return pin

    pin.status = Pin.Status.CONFLICT
    pin.save()
    unit.mapping_status = "conflict"
    unit.save(update_fields=["mapping_status"])
    return pin


@transaction.atomic
def report_problem(*, actor, unit, problem, note="") -> Pin:
    require_unit_in_scope(actor, unit)
    if problem not in Pin.Problem.values:
        raise ValidationError({"problem": "Choose what is wrong with this unit."})
    note = (note or "").strip()
    if not note:
        raise ValidationError({"note": "Describe what you found, e.g. where the unit moved to."})
    unit = _lock(unit)
    pin = Pin.objects.create(
        polling_unit=unit,
        pinned_by=actor,
        status=Pin.Status.PROBLEM,
        problem=problem,
        note=note,
    )
    if unit.latitude is None:
        unit.mapping_status = "problem"
        unit.save(update_fields=["mapping_status"])
    return pin


@transaction.atomic
def accept_pin(*, actor, pin: Pin) -> Pin:
    """Resolve a conflict by accepting one pin as the unit's location."""
    pin = Pin.objects.select_for_update().select_related("polling_unit").get(pk=pin.pk)
    unit = _lock(pin.polling_unit)
    require_unit_in_scope(actor, unit)
    if pin.status != Pin.Status.CONFLICT:
        raise ValidationError("Only a conflicting pin can be accepted.")
    if pin.pinned_by_id == actor.pk:
        raise PermissionDenied("Another coordinator must resolve your own pin.")

    now = timezone.now()
    unit.pins.exclude(pk=pin.pk).filter(
        status__in=[Pin.Status.PINNED, Pin.Status.CONFIRMED, Pin.Status.CONFLICT]
    ).update(status=Pin.Status.SUPERSEDED, resolved_by=actor, resolved_at=now)
    pin.status = Pin.Status.ACCEPTED
    pin.resolved_by, pin.resolved_at = actor, now
    pin.save(update_fields=["status", "resolved_by", "resolved_at", "updated_at"])
    _set_location(unit, pin, actor, "confirmed")
    return pin

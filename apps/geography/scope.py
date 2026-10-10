"""Geographic scope shared by users, members, agents and operational records.

Every scoped row stores its level plus the full chain of units down to that
level (ancestors are derived), so "is X inside the user's scope" is a single
equality on the user's most specific unit.
"""

from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from apps.core.choices import GeographyLevel, Role

# Scope field holding the unit for each level, from broadest to narrowest.
LEVEL_FIELD = {
    GeographyLevel.GEOPOLITICAL_ZONE: "zone",
    GeographyLevel.STATE: "state",
    GeographyLevel.SENATORIAL_DISTRICT: "senatorial_district",
    GeographyLevel.LGA: "lga",
    GeographyLevel.WARD: "ward",
    GeographyLevel.POLLING_UNIT: "polling_unit",
}
SCOPE_FIELDS = tuple(LEVEL_FIELD.values())

# The scope level each operational role must be assigned at. Roles not
# listed work nationally or at any level.
ROLE_SCOPE_LEVEL = {
    Role.ZONAL_COORDINATOR: GeographyLevel.GEOPOLITICAL_ZONE,
    Role.STATE_COORDINATOR: GeographyLevel.STATE,
    Role.LGA_COORDINATOR: GeographyLevel.LGA,
    Role.WARD_COORDINATOR: GeographyLevel.WARD,
    Role.POLLING_UNIT_AGENT: GeographyLevel.POLLING_UNIT,
}

_MODEL_FOR_FIELD = {
    "zone": "geography.Zone",
    "state": "geography.State",
    "senatorial_district": "geography.SenatorialDistrict",
    "lga": "geography.Lga",
    "ward": "geography.Ward",
    "polling_unit": "geography.PollingUnit",
}


def _scope_fk(field):
    return models.ForeignKey(
        _MODEL_FOR_FIELD[field],
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
    )


class ScopedModel(models.Model):
    """Abstract base adding a validated geographic scope."""

    scope_level = models.CharField(
        max_length=24,
        choices=GeographyLevel.choices,
        default=GeographyLevel.COUNTRY,
    )
    zone = _scope_fk("zone")
    state = _scope_fk("state")
    senatorial_district = _scope_fk("senatorial_district")
    lga = _scope_fk("lga")
    ward = _scope_fk("ward")
    polling_unit = _scope_fk("polling_unit")

    class Meta:
        abstract = True

    def normalize_scope(self):
        """Require the level's unit, derive its ancestors, clear deeper units."""
        if self.scope_level != GeographyLevel.COUNTRY:
            field = LEVEL_FIELD[self.scope_level]
            if getattr(self, f"{field}_id") is None:
                label = GeographyLevel(self.scope_level).label.lower()
                raise ValidationError({field: f"Required for a {label} scope."})

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

        levels = list(LEVEL_FIELD)
        start = (
            0 if self.scope_level == GeographyLevel.COUNTRY else levels.index(self.scope_level) + 1
        )
        for level in levels[start:]:
            setattr(self, LEVEL_FIELD[level], None)

    def validate_role_scope(self, role, field="scope_level"):
        required = ROLE_SCOPE_LEVEL.get(role)
        if required and self.scope_level != required:
            raise ValidationError(
                {
                    field: (
                        f"A {Role(role).label.lower()} must be scoped to a "
                        f"{GeographyLevel(required).label.lower()}."
                    )
                }
            )

    @property
    def scope_unit_field(self) -> str | None:
        """Name of the most specific unit field for this scope (None = national)."""
        if self.scope_level == GeographyLevel.COUNTRY:
            return None
        return LEVEL_FIELD[self.scope_level]


def within_scope_q(actor: ScopedModel, prefix: str = "") -> Q:
    """Filter for rows (with scope fields under `prefix`) inside actor's scope."""
    field = actor.scope_unit_field
    if field is None:
        return Q()
    return Q(**{f"{prefix}{field}_id": getattr(actor, f"{field}_id")})


def scope_contains(actor: ScopedModel, target: ScopedModel) -> bool:
    """True when `target`'s scope lies inside `actor`'s scope."""
    field = actor.scope_unit_field
    if field is None:
        return True
    return getattr(target, f"{field}_id") == getattr(actor, f"{field}_id")


def scope_payload(obj: ScopedModel) -> dict:
    """Scope in the shape of the Flutter `GeographicScope` class."""

    def part(unit, attr):
        return getattr(unit, attr) if unit is not None else None

    return {
        "level": obj.scope_level,
        "country": "Nigeria",
        "zoneId": part(obj.zone, "code"),
        "zoneName": part(obj.zone, "name"),
        "stateId": part(obj.state, "code"),
        "stateName": part(obj.state, "name"),
        "senatorialDistrictId": part(obj.senatorial_district, "code"),
        "senatorialDistrictName": part(obj.senatorial_district, "name"),
        "lgaId": part(obj.lga, "code"),
        "lgaName": part(obj.lga, "name"),
        "wardId": part(obj.ward, "code"),
        "wardName": part(obj.ward, "name"),
        "pollingUnitId": part(obj.polling_unit, "code"),
        "pollingUnitName": part(obj.polling_unit, "name"),
    }


_PAYLOAD_KEY = {
    "zone": "zoneId",
    "state": "stateId",
    "senatorial_district": "senatorialDistrictId",
    "lga": "lgaId",
    "ward": "wardId",
    "polling_unit": "pollingUnitId",
}


_SCOPE_SCHEMA = {
    "type": "object",
    "description": (
        "Geographic scope (Flutter GeographicScope). On input send `level` and the "
        "id for that level; parent units are derived."
    ),
    "properties": {
        "level": {"type": "string", "enum": list(GeographyLevel.values)},
        "country": {"type": "string", "readOnly": True},
        **{
            key: {"type": "string", "nullable": True}
            for key in (
                "zoneId",
                "zoneName",
                "stateId",
                "stateName",
                "senatorialDistrictId",
                "senatorialDistrictName",
                "lgaId",
                "lgaName",
                "wardId",
                "wardName",
                "pollingUnitId",
                "pollingUnitName",
            )
        },
    },
    "required": ["level"],
}


@extend_schema_field(_SCOPE_SCHEMA)
class ScopeField(serializers.Field):
    """Reads/writes a scope as `{"level": ..., "lgaId": "KD-KADUNA-NORTH", ...}`.

    On input only `level` and the unit id for that level are needed; parent
    units are derived. Validated data is a dict of model attributes.
    """

    def to_representation(self, obj):
        return scope_payload(obj)

    def get_attribute(self, instance):
        return instance

    def to_internal_value(self, data):
        from django.apps import apps

        if not isinstance(data, dict):
            raise serializers.ValidationError("Expected a scope object.")
        level = data.get("level", GeographyLevel.COUNTRY)
        if level not in GeographyLevel.values:
            raise serializers.ValidationError({"level": f"Unknown level {level!r}."})

        attrs = {"scope_level": level, **{field: None for field in SCOPE_FIELDS}}
        if level == GeographyLevel.COUNTRY:
            return attrs
        field = LEVEL_FIELD[level]
        key = _PAYLOAD_KEY[field]
        code = data.get(key)
        if not code:
            raise serializers.ValidationError({key: "Required for this level."})
        model = apps.get_model(_MODEL_FOR_FIELD[field])
        try:
            attrs[field] = model.objects.get(code=code)
        except model.DoesNotExist as exc:
            raise serializers.ValidationError({key: f"Unknown code {code!r}."}) from exc
        return attrs


SCOPE_SELECT_RELATED = tuple(SCOPE_FIELDS)


class ScopeRef:
    """An in-memory scope (level + unit chain) usable wherever a ScopedModel is.

    Build with `ScopeRef.resolve(level, code)`; parent units are derived.
    """

    def __init__(self, level, **units):
        self.scope_level = level
        for field in SCOPE_FIELDS:
            setattr(self, field, units.get(field))

    def __getattr__(self, name):
        # Provide `<field>_id` like a model instance would.
        if name.endswith("_id") and name[:-3] in SCOPE_FIELDS:
            unit = getattr(self, name[:-3])
            return unit.pk if unit is not None else None
        raise AttributeError(name)

    scope_unit_field = ScopedModel.scope_unit_field

    @classmethod
    def national(cls):
        return cls(GeographyLevel.COUNTRY)

    @classmethod
    def resolve(cls, level, code=None):
        """Look up the unit for `level`/`code` and derive its parents.

        Raises ValidationError for an unknown level or code.
        """
        from django.apps import apps

        if level == GeographyLevel.COUNTRY:
            return cls.national()
        if level not in LEVEL_FIELD:
            raise ValidationError({"level": f"Unknown level {level!r}."})
        field = LEVEL_FIELD[level]
        model = apps.get_model(_MODEL_FOR_FIELD[field])
        unit = model.objects.filter(code=code).first() if code else None
        if unit is None:
            raise ValidationError({"code": f"Unknown {field.replace('_', ' ')} {code!r}."})
        units = {field: unit}
        if field == "polling_unit":
            units["ward"] = unit.ward
        if "ward" in units:
            units["lga"] = units["ward"].lga
        if "lga" in units:
            units["senatorial_district"] = units["lga"].senatorial_district
            units["state"] = units["lga"].state
        if "senatorial_district" in units:
            units["state"] = units["senatorial_district"].state
        if "state" in units:
            units["zone"] = units["state"].zone
        return cls(level, **units)

    @classmethod
    def of(cls, obj):
        """Copy the scope of any ScopedModel instance."""
        return cls(obj.scope_level, **{f: getattr(obj, f) for f in SCOPE_FIELDS})

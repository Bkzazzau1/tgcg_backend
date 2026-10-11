"""System role definitions: the former fixed roles, kept in step with the code.

They are created on migrate and refreshed whenever ROLE_CAPABILITIES changes.
Their permissions and levels are managed in code; administrators may still
edit their description and reporting line. National Administrator is the
protected role.
"""

from apps.core.choices import GeographyLevel, Role
from apps.core.permissions import ROLE_CAPABILITIES

from .models import RoleCategory, RoleDefinition, name_key

L = GeographyLevel
ALL_LEVELS = list(GeographyLevel.values)

SYSTEM_ROLES = {
    Role.NATIONAL_ADMINISTRATOR: (RoleCategory.COMMAND, [L.COUNTRY]),
    Role.NATIONAL_COLLATION_OFFICER: (RoleCategory.COMMAND, ALL_LEVELS),
    Role.SITUATION_ROOM_DIRECTOR: (RoleCategory.COMMAND, ALL_LEVELS),
    Role.READ_ONLY_EXECUTIVE: (RoleCategory.COMMAND, ALL_LEVELS),
    Role.ZONAL_COORDINATOR: (RoleCategory.FIELD, [L.GEOPOLITICAL_ZONE]),
    Role.STATE_COORDINATOR: (RoleCategory.FIELD, [L.STATE]),
    Role.LGA_COORDINATOR: (RoleCategory.FIELD, [L.LGA]),
    Role.WARD_COORDINATOR: (RoleCategory.FIELD, [L.WARD]),
    Role.POLLING_UNIT_AGENT: (RoleCategory.FIELD, [L.POLLING_UNIT]),
    Role.OBSERVER: (RoleCategory.FIELD, ALL_LEVELS),
    Role.LEGAL_OFFICER: (RoleCategory.LEGAL, ALL_LEVELS),
    Role.TECHNICAL_SUPPORT: (RoleCategory.TECHNICAL, ALL_LEVELS),
}

# Default reporting lines, set only when an administrator has not chosen one.
DEFAULT_SUPERVISOR = {
    Role.POLLING_UNIT_AGENT: Role.WARD_COORDINATOR,
    Role.WARD_COORDINATOR: Role.LGA_COORDINATOR,
    Role.LGA_COORDINATOR: Role.STATE_COORDINATOR,
    Role.STATE_COORDINATOR: Role.ZONAL_COORDINATOR,
}

DISPLAY_NAMES = {
    Role.NATIONAL_ADMINISTRATOR: "National Administrator",
    Role.NATIONAL_COLLATION_OFFICER: "National Collation Officer",
    Role.SITUATION_ROOM_DIRECTOR: "Situation Room Director",
    Role.READ_ONLY_EXECUTIVE: "Executive Viewer",
    Role.ZONAL_COORDINATOR: "Zonal Coordinator",
    Role.STATE_COORDINATOR: "State Coordinator",
    Role.LGA_COORDINATOR: "LGA Coordinator",
    Role.WARD_COORDINATOR: "Ward Coordinator",
    Role.POLLING_UNIT_AGENT: "Polling Unit Agent",
    Role.OBSERVER: "Observer",
    Role.LEGAL_OFFICER: "Legal Officer",
    Role.TECHNICAL_SUPPORT: "Technical Support",
}


def sync_system_roles() -> dict[str, RoleDefinition]:
    roles = {}
    for role, (category, levels) in SYSTEM_ROLES.items():
        protected = role == Role.NATIONAL_ADMINISTRATOR
        fields = {
            "name": DISPLAY_NAMES[role],
            "name_key": name_key(DISPLAY_NAMES[role]),
            "category": category,
            "permitted_levels": levels,
            "permissions": sorted(ROLE_CAPABILITIES[role]),
            "is_system": True,
            "is_protected": protected,
            "legacy_role": role,
        }
        definition = RoleDefinition.objects.filter(code=role.value).first()
        if definition is None:
            definition = RoleDefinition.objects.create(code=role.value, **fields)
        else:
            changed = [name for name, value in fields.items() if getattr(definition, name) != value]
            for name in changed:
                setattr(definition, name, fields[name])
            if changed:
                definition.save(update_fields=[*changed, "updated_at"])
        roles[role] = definition

    for role, supervisor in DEFAULT_SUPERVISOR.items():
        definition = roles[role]
        if definition.supervising_role_id is None:
            definition.supervising_role = roles[supervisor]
            definition.save(update_fields=["supervising_role", "updated_at"])
    return roles


def sync_after_migrate(sender, **kwargs):
    sync_system_roles()


def system_role(role: str) -> RoleDefinition:
    return RoleDefinition.objects.get(code=role)

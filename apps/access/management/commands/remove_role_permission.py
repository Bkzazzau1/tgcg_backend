"""Take one permission away from named roles, as a National Administrator
would in Role Management (audited as role.updated).

    python manage.py remove_role_permission viewSituationRoom --by NA-1 \
        --role "PU Coordinator" --role "Assistant PU Coordinator" --commit

Without --commit it only reports what would change. System roles take their
permissions from the code (ROLE_CAPABILITIES) and are left alone.
"""

from apps.access.management.role_permission_command import RolePermissionCommand


class Command(RolePermissionCommand):
    help = "Remove a permission from named roles (dry run unless --commit)."
    adding = False

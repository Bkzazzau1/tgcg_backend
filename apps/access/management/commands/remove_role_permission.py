"""Take one permission away from named roles, as a National Administrator
would in Role Management. Each change is written to the audit log as
role.updated, by the administrator named with --by.

    python manage.py remove_role_permission viewSituationRoom --by NA-1 \
        --role "PU Coordinator" --role "Assistant PU Coordinator" --commit

Without --commit it only reports what would change. System roles take their
permissions from the code (ROLE_CAPABILITIES) and are left alone.
"""

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.access import engine
from apps.access.models import RoleDefinition, name_key
from apps.access.registry import PERMISSIONS
from apps.access.services import update_role
from apps.accounts.models import User


class Command(BaseCommand):
    help = "Remove a permission from named roles (dry run unless --commit)."

    def add_arguments(self, parser):
        parser.add_argument("permission", help="Permission code, e.g. viewSituationRoom.")
        parser.add_argument(
            "--role", dest="roles", action="append", required=True, help="Role name; repeatable."
        )
        parser.add_argument(
            "--by", required=True, help="Access ID of the National Administrator making it."
        )
        parser.add_argument("--commit", action="store_true", help="Save; without it, a dry run.")

    def handle(self, *args, permission, roles, by, commit, **options):
        actor = User.objects.filter(access_id=by).first()
        if actor is None or not engine.is_national_administrator(actor):
            raise CommandError("--by must be the access ID of a National Administrator.")
        if permission not in PERMISSIONS:
            raise CommandError(f"Unknown permission {permission!r}.")

        changing = []
        for name in roles:
            role = RoleDefinition.objects.filter(name_key=name_key(name)).first()
            if role is None:
                self.stdout.write(f"{name}: no such role")
            elif role.is_system:
                self.stdout.write(f"{role.name}: a system role, set in code")
            elif permission not in role.permissions:
                self.stdout.write(f"{role.name}: does not have it")
            else:
                self.stdout.write(f"{role.name}: loses it")
                changing.append(role)
        if not commit:
            self.stdout.write("Dry run: nothing was saved. Add --commit to change them.")
            return

        try:
            with transaction.atomic():
                for role in changing:
                    update_role(
                        actor=actor,
                        role=role,
                        permissions=[code for code in role.permissions if code != permission],
                    )
        except ValidationError as exc:
            raise CommandError("; ".join(exc.messages)) from exc
        self.stdout.write(self.style.SUCCESS(f"Removed {permission} from {len(changing)} role(s)."))

"""Shared by add_role_permission and remove_role_permission: one permission
given to, or taken from, named roles as a National Administrator would in
Role Management. Each change is written to the audit log as role.updated, by
the administrator named with --by.

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


class RolePermissionCommand(BaseCommand):
    adding: bool

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
            elif (permission in role.permissions) == self.adding:
                self.stdout.write(
                    f"{role.name}: {'has it already' if self.adding else 'does not have it'}"
                )
            else:
                self.stdout.write(f"{role.name}: {'gains' if self.adding else 'loses'} it")
                changing.append(role)
        if not commit:
            self.stdout.write("Dry run: nothing was saved. Add --commit to change them.")
            return

        try:
            with transaction.atomic():
                for role in changing:
                    kept = [code for code in role.permissions if code != permission]
                    update_role(
                        actor=actor,
                        role=role,
                        permissions=sorted([*kept, permission]) if self.adding else kept,
                    )
        except ValidationError as exc:
            raise CommandError("; ".join(exc.messages)) from exc
        done = "Added" if self.adding else "Removed"
        where = "to" if self.adding else "from"
        self.stdout.write(
            self.style.SUCCESS(f"{done} {permission} {where} {len(changing)} role(s).")
        )

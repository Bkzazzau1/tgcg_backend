from django.apps import AppConfig
from django.db.models.signals import post_migrate


class AccessConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.access"
    label = "access"

    def ready(self):
        from .system_roles import sync_after_migrate

        post_migrate.connect(sync_after_migrate, sender=self)

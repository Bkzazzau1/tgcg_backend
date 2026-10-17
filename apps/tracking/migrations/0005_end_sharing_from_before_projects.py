# Location is shared only during projects (decided on 17 October 2026).
# Sharing switched on before projects existed has no project to end it, so it
# ends when this release is applied; later pings from older apps are dropped.

from django.db import migrations
from django.utils import timezone


def end_sharing(apps, schema_editor):
    DutySession = apps.get_model("tracking", "DutySession")
    now = timezone.now()
    for session in DutySession.objects.filter(ended_at__isnull=True, participation__isnull=True):
        session.ended_at = max(now, session.started_at)
        session.save(update_fields=["ended_at"])


class Migration(migrations.Migration):

    dependencies = [
        ("tracking", "0004_project_links"),
    ]

    operations = [
        migrations.RunPython(end_sharing, migrations.RunPython.noop),
    ]

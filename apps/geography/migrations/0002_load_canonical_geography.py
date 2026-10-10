# Seeds the canonical zones, states, senatorial districts and LGAs so every
# database starts with the same reference geography as the Flutter client.

from django.db import migrations


def load(apps, schema_editor):
    from apps.geography.seed import load_canonical_geography

    load_canonical_geography(apps)


class Migration(migrations.Migration):

    dependencies = [
        ("geography", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(load, migrations.RunPython.noop),
    ]

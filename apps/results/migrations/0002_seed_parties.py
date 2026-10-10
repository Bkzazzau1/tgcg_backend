# Seeds the 18 political parties registered by INEC for the 2023 general
# election. Review against INEC's current register before each election;
# parties are managed in the admin afterwards.

from django.db import migrations

PARTIES = [
    ("A", "Accord"),
    ("AA", "Action Alliance"),
    ("AAC", "African Action Congress"),
    ("ADC", "African Democratic Congress"),
    ("ADP", "Action Democratic Party"),
    ("APC", "All Progressives Congress"),
    ("APGA", "All Progressives Grand Alliance"),
    ("APM", "Allied Peoples Movement"),
    ("APP", "Action Peoples Party"),
    ("BP", "Boot Party"),
    ("LP", "Labour Party"),
    ("NNPP", "New Nigeria Peoples Party"),
    ("NRM", "National Rescue Movement"),
    ("PDP", "Peoples Democratic Party"),
    ("PRP", "Peoples Redemption Party"),
    ("SDP", "Social Democratic Party"),
    ("YPP", "Young Progressives Party"),
    ("ZLP", "Zenith Labour Party"),
]


def seed(apps, schema_editor):
    Party = apps.get_model("results", "Party")
    for acronym, name in PARTIES:
        Party.objects.update_or_create(acronym=acronym, defaults={"name": name})


class Migration(migrations.Migration):
    dependencies = [
        ("results", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(seed, migrations.RunPython.noop),
    ]

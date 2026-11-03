# Types of support group (3 November 2026). The four the owner named, in
# their order; every group registered before is a Support Group. The field
# becomes required in 0003, in its own transaction, after the groups have
# their type.

import uuid

import django.db.models.deletion
from django.db import migrations, models

KINDS = [
    ("support", "Support Groups"),
    ("women", "Women Groups"),
    ("youth", "Youth Groups"),
    ("pwd", "People With Disability (PWD) Groups"),
]


def add_kinds(apps, schema_editor):
    GroupKind = apps.get_model("support_groups", "GroupKind")
    SupportGroup = apps.get_model("support_groups", "SupportGroup")
    for position, (code, name) in enumerate(KINDS, start=1):
        GroupKind.objects.get_or_create(code=code, defaults={"name": name, "position": position})
    general = GroupKind.objects.get(code="support")
    SupportGroup.objects.filter(kind__isnull=True).update(kind=general)


class Migration(migrations.Migration):

    dependencies = [
        ('support_groups', '0001_initial'),
    ]

    operations = [
        migrations.CreateModel(
            name='GroupKind',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('uuid', models.UUIDField(default=uuid.uuid4, editable=False, unique=True)),
                ('created_at', models.DateTimeField(auto_now_add=True, db_index=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('code', models.SlugField(editable=False, max_length=40, unique=True)),
                ('name', models.CharField(max_length=80, unique=True)),
                ('position', models.PositiveSmallIntegerField(default=0, help_text='Its place in lists.')),
                ('offered', models.BooleanField(default=True, help_text='Offered when a group registers.')),
            ],
            options={
                'ordering': ['position', 'id'],
            },
        ),
        migrations.AddField(
            model_name='supportgroup',
            name='kind',
            field=models.ForeignKey(null=True, on_delete=django.db.models.deletion.PROTECT, related_name='groups', to='support_groups.groupkind'),
        ),
        migrations.RunPython(add_kinds, migrations.RunPython.noop),
    ]

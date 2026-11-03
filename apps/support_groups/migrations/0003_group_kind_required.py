# Every group has its type since 0002: from now on one is required.

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('support_groups', '0002_group_kinds'),
    ]

    operations = [
        migrations.AlterField(
            model_name='supportgroup',
            name='kind',
            field=models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='groups', to='support_groups.groupkind'),
        ),
    ]

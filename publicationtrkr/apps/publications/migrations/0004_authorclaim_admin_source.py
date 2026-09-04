"""
Add `admin` to AuthorClaim.source (issue #32).

Choices only, so this is app-level and emits no DDL -- Postgres never held the
constraint. It ships as its own migration because Django records choices in the model
state, and run_server.sh refuses to boot on `makemigrations --check` drift.

The value distinguishes a fabric_uuid an admin typed into the author edit form from one
the scorer suggested and an admin approved. See the AuthorClaim docstring for why that
distinction is worth a migration.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('publications', '0003_authorclaim'),
    ]

    operations = [
        migrations.AlterField(
            model_name='authorclaim',
            name='source',
            field=models.CharField(choices=[('machine', 'Machine'), ('self', 'Self'), ('admin', 'Admin')], default='machine', max_length=24),
        ),
    ]

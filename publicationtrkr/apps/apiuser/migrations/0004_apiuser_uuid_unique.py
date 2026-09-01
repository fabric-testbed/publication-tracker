"""
Add the unique index on ApiUser.uuid.

Split from 0003 rather than appended to it. 0003 repoints foreign keys and deletes
duplicate rows; Django declares foreign keys DEFERRABLE INITIALLY DEFERRED, so those
trigger events stay pending until 0003's transaction commits, and Postgres rejects an
ALTER TABLE issued while they are outstanding. Running the index in a second migration
gives it a transaction of its own.

This will fail loudly if 0003's dedupe missed anything, which is the behaviour we want
-- CHANGELOG 1.10.0 records production hitting `IntegrityError: could not create unique
index` on TaskTimeoutTracker duplicates.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('apiuser', '0003_apiuser_sync_fields'),
    ]

    operations = [
        migrations.AlterField(
            model_name='apiuser',
            name='uuid',
            field=models.CharField(max_length=255, unique=True),
        ),
    ]

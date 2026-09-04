"""
Add the Google Scholar and Scopus identifier columns to ApiUser (issue #32, v1.15.0).

Two nullable-in-practice CharFields and nothing else. No data step: the values come from
core-api's /core-api-metrics/people, which `sync_fabric_users` reads on its next run, so
a backfill here would only duplicate that -- and it would have to make a network call
from inside a migration, under a token the host may not have added yet.

Both columns are `blank=True` and NOT NULL, so every existing row gets `''` and reads as
"no identifier on record", which author-claim scoring treats as no signal rather than as
a negative. That is the right reading of the data: 8 of 3,311 people hold a Scholar
identifier and none holds a Scopus one.

Nothing here touches a foreign key, so unlike 0003/0004 this needs no split across two
migrations -- there are no pending trigger events for an ALTER TABLE to trip over.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('apiuser', '0004_apiuser_uuid_unique'),
    ]

    operations = [
        migrations.AddField(
            model_name='apiuser',
            name='google_scholar',
            field=models.CharField(blank=True, max_length=255),
        ),
        migrations.AddField(
            model_name='apiuser',
            name='scopus',
            field=models.CharField(blank=True, max_length=255),
        ),
    ]

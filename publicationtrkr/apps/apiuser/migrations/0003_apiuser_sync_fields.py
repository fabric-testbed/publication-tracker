"""
Prepare ApiUser to hold the whole FABRIC directory rather than only the people who
have logged in (see issue #23).

Operation order matters:

  1. AddField / AlterField for the new columns and defaults. `fabric_roles` and
     `projects` had no default, so they were required on every insert -- any bulk
     create path trips over that immediately.
  2. RunPython `backfill_has_logged_in` -- every row that exists *before* this
     migration got there via `auth_user_by_cookie` / `auth_user_by_token`, i.e. a real
     session. The column default is False (correct for rows the sync will create), so
     without this backfill the entire existing population would be mislabelled.
  3. RunPython `dedupe_apiuser_uuid` -- MUST run before the unique index is added,
     which is why that AlterField lands in 0004 rather than at the end of this file.
     Django declares foreign keys DEFERRABLE INITIALLY DEFERRED, so the UPDATE and
     DELETE below leave trigger events pending until this migration's transaction
     commits, and Postgres refuses ALTER TABLE while they are outstanding:
     `OperationalError: cannot ALTER TABLE "apiuser_apiuser" because it has pending
     trigger events`. Two migrations means two transactions, and the commit at the end
     of this one flushes them.

The dedupe is not a delete. `Publication.created_by` / `modified_by` are ForeignKeys on
the integer `id`, so deleting a duplicate would SET_NULL those references and silently
destroy publication provenance. Losers' inbound FKs are repointed at the survivor
first, discovered through the historical model's `related_objects` rather than a
hardcoded list, so a foreign key added later is still handled.

CHANGELOG 1.10.0 records production hitting `IntegrityError: could not create unique
index` on TaskTimeoutTracker duplicates, which is exactly this failure one table over.
The March `dumpdata/apiuser.json` snapshot has 11 rows and 11 distinct uuids, so this
may well be a no-op on current data -- it is written defensively because the sample is
five months old and predates any constraint that would have prevented drift.
"""

import django.contrib.postgres.fields
from django.db import migrations, models


def backfill_has_logged_in(apps, schema_editor):
    """Every pre-existing row was created by a login, except the anonymous placeholder."""
    ApiUser = apps.get_model('apiuser', 'ApiUser')
    ApiUser.objects.exclude(cilogon_id__in=('', 'None')).update(has_logged_in=True)


def dedupe_apiuser_uuid(apps, schema_editor):
    """Collapse duplicate uuids onto one survivor, repointing every inbound FK first."""
    ApiUser = apps.get_model('apiuser', 'ApiUser')

    seen = {}
    for row in ApiUser.objects.all().order_by('id'):
        seen.setdefault(row.uuid, []).append(row)

    # Every ManyToOneRel pointing at ApiUser in this historical project state.
    relations = [
        rel for rel in ApiUser._meta.related_objects
        if getattr(rel, 'field', None) is not None and rel.field.many_to_one
    ]

    for uuid_value, rows in seen.items():
        if len(rows) < 2:
            continue
        # Prefer a row that carries a live session (non-null access_expires), then the
        # most recently refreshed one, then the lowest id. Deterministic either way.
        rows.sort(key=lambda r: (
            r.access_expires is None,
            -(r.access_expires.timestamp() if r.access_expires else 0),
            r.id,
        ))
        survivor, losers = rows[0], rows[1:]
        loser_ids = [r.id for r in losers]

        for rel in relations:
            column = '{0}_id'.format(rel.field.name)
            rel.related_model.objects.filter(
                **{'{0}__in'.format(column): loser_ids}
            ).update(**{column: survivor.id})

        ApiUser.objects.filter(id__in=loser_ids).delete()


def drop_dead_author_refresh_tracker(apps, schema_editor):
    """
    Remove the orphaned `author_refresh_check` TaskTimeoutTracker row.

    ARC / AUTHOR_REFRESH_CHECK_DAYS were scaffolding for an abandoned earlier attempt at
    this same feature: the tracker was initialized on every boot and never read by any
    code path. `init_task_timeout_tracker` no longer creates it; this clears the row it
    already left behind on deployed databases.
    """
    TaskTimeoutTracker = apps.get_model('apiuser', 'TaskTimeoutTracker')
    TaskTimeoutTracker.objects.filter(name='author_refresh_check').delete()


def noop(apps, schema_editor):
    """Reverse is a no-op: merged rows and a dropped cache row cannot be un-merged."""


class Migration(migrations.Migration):

    dependencies = [
        ('apiuser', '0002_tasktimeouttracker_unique_task_timeout_tracker_name'),
    ]

    operations = [
        migrations.AddField(
            model_name='apiuser',
            name='active',
            field=models.BooleanField(default=True),
        ),
        migrations.AddField(
            model_name='apiuser',
            name='has_logged_in',
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name='apiuser',
            name='last_synced',
            field=models.DateTimeField(blank=True, default=None, null=True),
        ),
        migrations.AlterField(
            model_name='apiuser',
            name='cilogon_id',
            field=models.CharField(blank=True, max_length=255),
        ),
        migrations.AlterField(
            model_name='apiuser',
            name='fabric_roles',
            field=django.contrib.postgres.fields.ArrayField(
                base_field=models.CharField(blank=True, max_length=255), default=list
            ),
        ),
        migrations.AlterField(
            model_name='apiuser',
            name='projects',
            field=django.contrib.postgres.fields.ArrayField(
                base_field=models.CharField(blank=True, max_length=255), default=list
            ),
        ),
        migrations.RunPython(backfill_has_logged_in, noop),
        migrations.RunPython(dedupe_apiuser_uuid, noop),
        migrations.RunPython(drop_dead_author_refresh_tracker, noop),
    ]

"""
Add ApiUserProjectMembership and seed it from ApiUser.projects (issue #72, v1.22.0).

The table records every project a person has ever been observed on, so that the
directory sync can apply removals without deleting the evidence claim scoring reads.
It has to exist *before* anything syncs removals on a schedule -- see the issue.

The seed writes one row per element of every ApiUser.projects, marked `source='seed'`.
Its dates are the row's `last_synced` (the day the directory last confirmed it), or the
migration's own time for a row only a login ever wrote. Either way they are observation
dates: a seeded `first_seen` says when we first looked, not when the person joined.

Memberships already removed from ApiUser.projects -- the 156 dropped by the 2026-09-21
`--full` sync -- are not in this table after the migration. They survive only in
pre-deploy dumps, and `import_project_memberships` loads them afterwards.

Reversing drops the table, and with it every membership recorded since the deploy.
"""

from datetime import datetime, timezone

import django.db.models.deletion
from django.db import migrations, models


def seed_memberships(apps, schema_editor):
    ApiUser = apps.get_model('apiuser', 'ApiUser')
    ApiUserProjectMembership = apps.get_model('apiuser', 'ApiUserProjectMembership')
    now = datetime.now(timezone.utc)
    rows = []
    users = 0
    for api_user in ApiUser.objects.exclude(projects=[]).order_by('id').iterator():
        seen_at = api_user.last_synced or now
        # A set, and blanks dropped: ApiUser.projects is written sorted and de-duplicated
        # by split_fabric_roles, but a duplicate here would fail the unique constraint
        # and abort the deploy, which is too high a price for trusting that.
        projects = sorted({p for p in api_user.projects if p})
        if projects:
            users += 1
        for project_uuid in projects:
            rows.append(ApiUserProjectMembership(
                api_user_id=api_user.id, project_uuid=project_uuid,
                first_seen=seen_at, last_seen=seen_at, source='seed',
            ))
    ApiUserProjectMembership.objects.bulk_create(rows, batch_size=1000)
    print('apiuser.0006: seeded {0} project membership(s) for {1} user(s) from '
          'ApiUser.projects'.format(len(rows), users))


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('apiuser', '0005_apiuser_scholar_scopus'),
    ]

    operations = [
        migrations.CreateModel(
            name='ApiUserProjectMembership',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('first_seen', models.DateTimeField()),
                ('last_seen', models.DateTimeField()),
                ('project_uuid', models.CharField(db_index=True, max_length=255)),
                ('source', models.CharField(choices=[('seed', 'Seeded from existing data'), ('sync', 'Directory sync'), ('login', 'Login refresh')], max_length=16)),
                ('api_user', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='memberships', to='apiuser.apiuser')),
            ],
            options={
                'constraints': [models.UniqueConstraint(fields=('api_user', 'project_uuid'), name='unique_api_user_project_membership')],
            },
        ),
        # Reversing the CreateModel drops the table, so the seed needs no reverse of its own.
        migrations.RunPython(seed_memberships, noop),
    ]

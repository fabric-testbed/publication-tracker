"""
Add AuthorClaim, and record the claims that already exist as `self_asserted` (issue #32).

The table is the ledger behind the admin claim queue. `Author.fabric_uuid` stays the one
authoritative field -- scoring never writes it -- so this migration adds no column to
Author and changes no attribution.

The data step exists because the ledger would otherwise start out lying. Production
already holds hundreds of claimed authors, every one of them asserted by the person
themselves through the immediate self-claim path, and with no row here they would look
like authors nobody has ever claimed. The queue would then offer suggestions for people
who are already attributed, which is both noise and a way to overwrite a correct claim.
Option (a) on the issue keeps that immediate path, so the backfill records what it did.

Two properties of the data this cannot assume, and therefore checks:

  - `Author.fabric_uuid` is a CharField, not a foreign key, so there is no guarantee it
    names an ApiUser that exists. One that does not cannot become a claim -- the row
    needs an api_user FK -- so those are counted and reported rather than skipped
    silently. A non-zero count is a data-quality finding worth seeing in the deploy log,
    in the same way 0002's orphan count was.
  - The same person can be claimed on many authors, which is normal and fine; the unique
    constraint is on (author, api_user), not on api_user.

`decided_at` and `decided_by` are deliberately left NULL on backfilled rows. We do not
know when the claim was made or by whom, and inventing `now()` would put a false date in
an audit trail. The `signals` marker below is what distinguishes a backfilled row from
one written by a live self-claim.

Reverse deletes only the rows this created, identified by that marker, so a rollback
cannot take a real decision with it.
"""

from uuid import uuid4

import django.db.models.deletion
from django.db import migrations, models

# Marks a row created by this backfill rather than by a live self-claim, so the
# reverse can be exact and so the provenance is legible later.
BACKFILL_MARKER = {'backfill': 'publications.0003'}


def backfill_self_asserted_claims(apps, schema_editor):
    Author = apps.get_model('publications', 'Author')
    ApiUser = apps.get_model('apiuser', 'ApiUser')
    AuthorClaim = apps.get_model('publications', 'AuthorClaim')

    claimed = list(
        Author.objects.exclude(fabric_uuid__isnull=True).exclude(fabric_uuid='')
    )
    if not claimed:
        print('publications.0003: no claimed Author rows, nothing to backfill')
        return

    api_user_ids = dict(
        ApiUser.objects.filter(
            uuid__in={a.fabric_uuid for a in claimed}
        ).values_list('uuid', 'id')
    )

    rows, unmatched = [], []
    for author in claimed:
        api_user_id = api_user_ids.get(author.fabric_uuid)
        if api_user_id is None:
            unmatched.append(author.uuid)
            continue
        rows.append(AuthorClaim(
            author_id=author.id,
            api_user_id=api_user_id,
            score=1.0,
            signals=dict(BACKFILL_MARKER),
            source='self',
            status='self_asserted',
            uuid=str(uuid4()),
        ))

    # ignore_conflicts covers a re-run against a database where some rows already
    # exist; the unique constraint on (author, api_user) is what makes that safe.
    AuthorClaim.objects.bulk_create(rows, ignore_conflicts=True)
    print('publications.0003: recorded {0} self_asserted claim(s) from {1} claimed '
          'Author row(s)'.format(len(rows), len(claimed)))
    if unmatched:
        print('publications.0003: WARNING {0} claimed Author row(s) carry a '
              'fabric_uuid matching no ApiUser and could not be recorded: {1}'.format(
                  len(unmatched), ', '.join(unmatched[:10])))


def remove_backfilled_claims(apps, schema_editor):
    AuthorClaim = apps.get_model('publications', 'AuthorClaim')
    removed = AuthorClaim.objects.filter(
        status='self_asserted', signals=BACKFILL_MARKER
    ).delete()[0]
    print('publications.0003: removed {0} backfilled claim(s)'.format(removed))


class Migration(migrations.Migration):

    dependencies = [
        ('apiuser', '0004_apiuser_uuid_unique'),
        ('publications', '0002_delete_orphan_authors'),
    ]

    operations = [
        migrations.CreateModel(
            name='AuthorClaim',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created', models.DateTimeField(auto_now_add=True)),
                ('decided_at', models.DateTimeField(blank=True, default=None, null=True)),
                ('modified', models.DateTimeField(auto_now=True)),
                ('score', models.FloatField(default=0.0)),
                ('signals', models.JSONField(blank=True, default=dict)),
                ('source', models.CharField(choices=[('machine', 'Machine'), ('self', 'Self')], default='machine', max_length=24)),
                ('status', models.CharField(choices=[('suggested', 'Suggested'), ('approved', 'Approved'), ('rejected', 'Rejected'), ('self_asserted', 'Self-asserted')], default='suggested', max_length=24)),
                ('uuid', models.CharField(max_length=255)),
                ('api_user', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='publications_authorclaim_api_user', to='apiuser.apiuser')),
                ('author', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='claims', to='publications.author')),
                ('decided_by', models.ForeignKey(blank=True, default=None, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='publications_authorclaim_decided_by', to='apiuser.apiuser')),
            ],
            options={
                'indexes': [models.Index(fields=['status', '-score'], name='authorclaim_status_score')],
                'constraints': [models.UniqueConstraint(fields=('author', 'api_user'), name='unique_author_claim')],
            },
        ),
        migrations.RunPython(
            backfill_self_asserted_claims,
            remove_backfilled_claims,
        ),
    ]

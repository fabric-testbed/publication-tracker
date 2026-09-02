"""
Delete the Author rows that belong to no Publication (see issue #24).

Where they came from: PublicationViewSet.create() saved every Author row before
publication.save(), so a title/link unique-constraint failure returned 400 with those
rows already committed and no publication left to reference them. Production carried
19 of them as of 2026-09-01. The publication_builder refactor in this same release
wraps both writes in one transaction, so this is a one-time cleanup of what the old
path left behind, not a recurring job.

This is a tracked data migration rather than the hand-run SQL used for the 1.9.9
cleanup. That script is no longer in the repo -- deleted in 6ae0ff1, while the
CHANGELOG entry still cites it -- which is exactly the failure a migration avoids: the
cleanup travels with the code that needs it and runs at boot in every environment,
once, in order. It follows apiuser/0003's RunPython dedupe, shipped in 1.12.0.

Nothing is lost. An orphan is unreachable by definition: Publication.authors is the
only path to an Author row, and no publication exists to hold this one's uuid. All 19
on production carry a NULL fabric_uuid, so no claim is destroyed either.

Both of those are properties of the data, not constraints the database enforces --
there is no foreign key between Author.publication_uuid and Publication.uuid, and
Publication.authors is a bare text array. So this checks them and prints what it finds
rather than assuming. A surprise surfaces in `docker compose logs -f django` at the
APPLY step, and before that on the fabric-dev rehearsal against a same-day production
dump.

Reverse is a noop. The deleted rows are in the pre-deploy dump if the count ever looks
wrong, but they were unreferenced, so there is nothing to restore them into.
"""

from django.db import migrations
from django.db.models import Exists, OuterRef


def delete_orphan_authors(apps, schema_editor):
    Author = apps.get_model('publications', 'Author')
    Publication = apps.get_model('publications', 'Publication')

    # The NOT EXISTS from the deploy plan's pre-flight count, as a subquery. An
    # __in against every publication uuid would send the whole table as a literal.
    orphans = Author.objects.filter(
        ~Exists(Publication.objects.filter(uuid=OuterRef('publication_uuid')))
    )
    orphan_uuids = list(orphans.values_list('uuid', flat=True))
    if not orphan_uuids:
        print('publications.0002: no orphan Author rows found, nothing to delete')
        return

    claimed = orphans.exclude(fabric_uuid__isnull=True).exclude(fabric_uuid='').count()
    if claimed:
        print('publications.0002: WARNING {0} of {1} orphan Author row(s) carry a '
              'fabric_uuid. They are still unreachable, but this was not true of the '
              'rows surveyed on 2026-09-01.'.format(claimed, len(orphan_uuids)))

    # An orphan should appear in no publication's authors array. Nothing enforces
    # that, so a hit here means a live publication is about to lose an author it
    # still lists -- worth seeing in the deploy log.
    referenced = list(
        Publication.objects.filter(authors__overlap=orphan_uuids).values_list('uuid', flat=True)
    )
    if referenced:
        print('publications.0002: WARNING orphan Author rows are still listed by '
              '{0} publication(s): {1}'.format(len(referenced), ', '.join(referenced)))

    deleted, _ = orphans.delete()
    print('publications.0002: deleted {0} orphan Author row(s)'.format(deleted))


def noop(apps, schema_editor):
    """Reverse is a no-op: the rows were unreferenced, so there is nothing to undo."""


class Migration(migrations.Migration):

    dependencies = [
        ('publications', '0001_initial'),
    ]

    operations = [
        migrations.RunPython(delete_orphan_authors, noop),
    ]

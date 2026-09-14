"""
Give every Author row the slot it occupies in its publication's author list (see #61).

Author order is the credit order printed on the paper, and it was only ever implied by
the position of an Author's uuid inside Publication.authors. PublicationSerializer
resolved that array with `filter(uuid__in=...)` and no ORDER BY -- Author had no
Meta.ordering either -- so Postgres returned heap order, which an UPDATE reshuffles. Any
claim, rename or display_name edit could therefore reorder a publication's authors for
good. Measured against the 2026-09-14 production dump on the fabric-dev rehearsal, 146
of 178 multi-author publications were already being served out of order, one of them a
34-author paper whose first author came back last.

The schema half of this migration adds the column and the Meta ordering. This data half
fills the column in from Publication.authors, which stays authoritative for membership
and has been correct all along -- the write path has always built it in the order the
caller supplied, whether that came from parse_bibtex splitting on ' and ' or from the
web form's comma-separated field. Nothing here invents an order; it copies the one the
database already held.

Two things this checks rather than assumes, because neither is enforced by the schema --
there is no foreign key between Author.publication_uuid and Publication.uuid, and
Publication.authors is a bare text array:

  * An Author named by no publication's array. Those are orphans, the class of row
    publications/0002 deleted 19 of on 2026-09-01. There should be none left, so a
    non-zero count here is worth seeing rather than silently defaulting to 0.
  * One Author uuid named by two different publications' arrays, which would make
    "its" position ambiguous. That should be impossible -- _create_authors mints a
    fresh uuid per publication -- and the last writer would win, so it prints.

Both surface in `docker compose logs -f django` at the APPLY step, and before that on
the rehearsal against a same-day production dump.

Reverse is a noop: reversing AddField drops the column, so there is nothing to put back.
"""

from django.db import migrations, models


def backfill_author_order(apps, schema_editor):
    Author = apps.get_model('publications', 'Author')
    Publication = apps.get_model('publications', 'Publication')

    # uuid -> position, read straight off the authoritative array.
    order_by_uuid = {}
    claimed_twice = set()
    for authors in Publication.objects.values_list('authors', flat=True).iterator():
        for position, author_uuid in enumerate(authors or []):
            if author_uuid in order_by_uuid:
                claimed_twice.add(author_uuid)
            order_by_uuid[author_uuid] = position

    to_update = []
    unplaced = 0
    for author in Author.objects.all().iterator():
        position = order_by_uuid.get(author.uuid)
        if position is None:
            unplaced += 1
            continue
        if author.author_order != position:
            author.author_order = position
            to_update.append(author)

    Author.objects.bulk_update(to_update, ['author_order'], batch_size=500)
    print('publications.0005: set author_order on {0} Author row(s) from '
          '{1} publication slot(s)'.format(len(to_update), len(order_by_uuid)))

    if unplaced:
        print('publications.0005: WARNING {0} Author row(s) are named by no '
              "publication's authors array and keep author_order=0. That is the orphan "
              'class publications/0002 cleaned up; none were expected to '
              'remain.'.format(unplaced))
    if claimed_twice:
        print('publications.0005: WARNING {0} Author uuid(s) appear in more than one '
              "publication's authors array, so their slot is ambiguous and the last one "
              'read won: {1}'.format(len(claimed_twice), ', '.join(sorted(claimed_twice))))


def noop(apps, schema_editor):
    """Reverse is a no-op: reversing AddField drops the column outright."""


class Migration(migrations.Migration):

    dependencies = [
        ('publications', '0004_authorclaim_admin_source'),
    ]

    operations = [
        migrations.AlterModelOptions(
            name='author',
            options={'ordering': ('author_order', 'id')},
        ),
        migrations.AddField(
            model_name='author',
            name='author_order',
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.RunPython(backfill_author_order, noop),
    ]

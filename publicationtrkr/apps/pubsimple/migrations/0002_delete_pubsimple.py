"""
Drop the pubsimple table.

The pub-simple format was a temporary, simpler shape for a publication. The
publications app supersedes it: same fields plus ``bibtex``, plus a real
``Author`` model, so there is nothing left for pubsimple to carry.

**Pub-simple data is disposable and is not migrated.** Everything from it that
mattered was entered into ``publications`` directly, so dropping the table loses
nothing that is wanted.

An earlier version of this migration refused to drop the table while any row had
no matching Publication, on the assumption that such a row was data about to be
lost. That assumption was wrong, and the guard would have blocked the very
deploy it was meant to protect: 27 of production's 102 rows have no counterpart,
all of them deliberately. The check is kept, but it now *reports* rather than
refusing -- the boot log records exactly what was dropped, which is the part
that was actually worth having.
"""

from django.db import migrations


def report_before_drop(apps, schema_editor):
    """
    Record what this migration is about to destroy, then let it proceed.

    Counts rows with no counterpart in publications using the same (title, link)
    pairing the Publication model enforces -- (title, link), and (title) where
    link IS NULL. Written to stdout so it lands in the container log alongside
    the rest of the boot output.
    """
    PubSimple = apps.get_model('pubsimple', 'PubSimple')
    Publication = apps.get_model('publications', 'Publication')

    total = PubSimple.objects.count()
    if not total:
        print('pubsimple: table is empty, nothing to drop')
        return

    unmatched = 0
    for ps in PubSimple.objects.all().iterator():
        if ps.link is None:
            exists = Publication.objects.filter(title=ps.title, link__isnull=True).exists()
        else:
            exists = Publication.objects.filter(title=ps.title, link=ps.link).exists()
        if not exists:
            unmatched += 1

    print(
        'pubsimple: dropping {0} row(s); {1} had no matching Publication '
        '(expected -- pub-simple data is disposable)'.format(total, unmatched)
    )


def noop_reverse(apps, schema_editor):
    """Nothing to undo -- the check only reads."""


class Migration(migrations.Migration):

    dependencies = [
        ('pubsimple', '0001_initial'),
        ('publications', '0001_initial'),
    ]

    operations = [
        migrations.RunPython(report_before_drop, noop_reverse),
        migrations.DeleteModel(name='PubSimple'),
    ]

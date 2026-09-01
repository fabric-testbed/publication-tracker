"""
Drop the pubsimple table.

The pub-simple format was a temporary, simpler shape for a publication. The
publications app supersedes it: same fields plus ``bibtex``, plus a real
``Author`` model, so there is nothing left for pubsimple to carry.

Data is *not* migrated here. It is migrated by the ``import_from_pubsimple``
management command, which must be run against the previous release (1.10.0)
before this migration is deployed -- see "Upgrade notes" in CHANGELOG.md.

Rather than trust that the operator remembered, ``verify_imported`` below
refuses to drop the table while any row is still unaccounted for. A missed
import therefore stops the boot with a readable error instead of silently
destroying rows: ``run_server.sh`` runs under ``set -euo pipefail``, so a
failing migration halts the container rather than serving on a dropped table.
"""

from django.db import migrations


def verify_imported(apps, schema_editor):
    """
    Refuse to drop pubsimple while any row has no counterpart in publications.

    Matches the two unique constraints the Publication model enforces --
    (title, link), and (title) where link IS NULL -- which is the same pairing
    import_from_pubsimple uses to decide whether a row is already present.
    """
    PubSimple = apps.get_model('pubsimple', 'PubSimple')
    Publication = apps.get_model('publications', 'Publication')

    unimported = []
    for ps in PubSimple.objects.all().iterator():
        if ps.link is None:
            exists = Publication.objects.filter(title=ps.title, link__isnull=True).exists()
        else:
            exists = Publication.objects.filter(title=ps.title, link=ps.link).exists()
        if not exists:
            unimported.append(ps.title)

    if unimported:
        sample = '\n'.join('  - {0}'.format(t[:100]) for t in unimported[:10])
        more = '\n  ... and {0} more'.format(len(unimported) - 10) if len(unimported) > 10 else ''
        raise RuntimeError(
            '\n'
            'Refusing to drop pubsimple: {0} row(s) have no matching Publication.\n'
            'Dropping the table now would lose them.\n\n'
            '{1}{2}\n\n'
            'Run the import against the previous release before deploying this one:\n'
            '    python manage.py import_from_pubsimple --dry-run\n'
            '    python manage.py import_from_pubsimple\n'.format(
                len(unimported), sample, more
            )
        )


def noop_reverse(apps, schema_editor):
    """Nothing to undo -- the check only reads."""


class Migration(migrations.Migration):

    dependencies = [
        ('pubsimple', '0001_initial'),
        ('publications', '0001_initial'),
    ]

    operations = [
        migrations.RunPython(verify_imported, noop_reverse),
        migrations.DeleteModel(name='PubSimple'),
    ]

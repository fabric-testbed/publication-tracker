"""
Management command: import_project_memberships

Load membership history recovered from a pre-deploy dump (#72). The CSV comes from
`scripts/extract-memberships-from-dump.py`: columns uuid, project_uuid, observed_at.

Every row goes through record_memberships() as `source='seed'`, so replaying a dump is
harmless -- the same dump twice, or dumps in any order, converges on the same table:
`first_seen` moves only earlier, `last_seen` only later, and nothing is deleted. That is
what makes it safe to import every dump back to 2026-09-01 without working out which
ones overlap.

The line that matters in the report is **new, no longer current**: memberships this
import put into the history that ApiUser.projects no longer lists. Those are the ended
memberships the 2026-09-21 `--full` sync removed, and the evidence claim scoring would
otherwise have lost.

A uuid the directory does not hold is skipped and counted, never created: a directory
row needs a name and roles that a dump of old projects cannot supply.

Usage:
    python manage.py import_project_memberships memberships.csv --dry-run
    python manage.py import_project_memberships memberships.csv
"""

import csv
import os

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils.dateparse import parse_datetime

from publicationtrkr.apps.apiuser.models import ApiUser, ApiUserProjectMembership
from publicationtrkr.apps.apiuser.utils.memberships import record_memberships

COLUMNS = ['uuid', 'project_uuid', 'observed_at']


class Command(BaseCommand):
    help = 'Import project membership history from a CSV extracted from a pre-deploy dump (#72).'

    def add_arguments(self, parser):
        parser.add_argument('csv', help='output of scripts/extract-memberships-from-dump.py')
        parser.add_argument('--dry-run', action='store_true',
                            help='Report what would be imported, then roll it back.')

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        groups = self._read(options['csv'])

        anon_uuid = os.getenv('API_USER_ANON_UUID')
        users = {u.uuid: u for u in ApiUser.objects.only('id', 'uuid', 'projects')}
        known = set(ApiUserProjectMembership.objects.values_list('api_user_id', 'project_uuid'))

        rows = sum(len(projects) for projects in groups.values())
        unknown = set()
        matched = set()
        new = new_former = already = inserted = 0
        # One transaction for the whole file: a failure part-way leaves nothing behind,
        # and a dry run is the real upsert rolled back, so its counts are exact rather
        # than predicted.
        with transaction.atomic():
            for (uuid, observed_at), projects in sorted(groups.items()):
                api_user = users.get(uuid)
                if api_user is None or uuid == anon_uuid:
                    unknown.add(uuid)
                    continue
                matched.add(uuid)
                current = set(api_user.projects or [])
                for project_uuid in projects:
                    if (api_user.id, project_uuid) not in known:
                        known.add((api_user.id, project_uuid))
                        new += 1
                        if project_uuid not in current:
                            new_former += 1
                    else:
                        already += 1
                inserted += record_memberships(
                    api_user.id, projects, observed_at, ApiUserProjectMembership.SEED)
            if inserted != new:
                # The two counts come from different places -- the upsert's own RETURNING
                # and the pre-read above -- so a disagreement means something else wrote
                # the table mid-run. Not fatal to the data, but the report would be wrong.
                raise CommandError('inserted {0} row(s) but expected {1}; rolled back -- '
                                   'is a sync running? Retry.'.format(inserted, new))
            if dry_run:
                transaction.set_rollback(True)

        self.stdout.write(self.style.WARNING('DRY RUN - rolled back.') if dry_run else 'APPLIED')
        self.stdout.write('Rows read              : {0}'.format(rows))
        self.stdout.write('Users matched          : {0}'.format(len(matched)))
        self.stdout.write('Unknown uuids skipped  : {0}'.format(len(unknown)))
        self.stdout.write('New memberships        : {0}{1}'.format(
            new, ' (would be written)' if dry_run else ''))
        self.stdout.write('  of which not current : {0}'.format(new_former))
        self.stdout.write('Already in history     : {0}'.format(already))

    def _read(self, path):
        """{(uuid, observed_at): [project_uuid, ...]}, validated before anything is written."""
        groups = {}
        try:
            with open(path, newline='', encoding='utf-8') as handle:
                reader = csv.reader(handle)
                header = next(reader, None)
                if header != COLUMNS:
                    raise CommandError('expected header {0}, got {1}'.format(COLUMNS, header))
                for number, row in enumerate(reader, start=2):
                    if len(row) != 3 or not all(row):
                        raise CommandError('line {0}: expected 3 non-empty fields'.format(number))
                    uuid, project_uuid, observed = row
                    observed_at = parse_datetime(observed)
                    if observed_at is None or observed_at.tzinfo is None:
                        raise CommandError('line {0}: observed_at {1!r} is not a timestamp '
                                           'with an offset'.format(number, observed))
                    groups.setdefault((uuid, observed_at), []).append(project_uuid)
        except OSError as exc:
            raise CommandError('cannot read {0}: {1}'.format(path, exc))
        return groups

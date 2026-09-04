"""
Management command: sync_fabric_users

Populates ApiUser from the whole FABRIC population rather than only the people who have
happened to log in (issue #23).

`api_users` was a cache filled lazily on first authenticated request, which made it
useless as a directory: the author-claim flow could only offer people who had already
visited, and admins could not see the FABRIC population at all. The source here is
`GET /journey-tracker/people` on core-api, a peer-service ingest endpoint that already
existed -- roughly 3,300 people in production, returned unpaginated.

Since v1.15.0 a second, independent pass reads `GET /core-api-metrics/people` for the
Google Scholar and Scopus identifiers author-claim scoring uses as a weak prior (#32).
It runs here, in the nightly sync, rather than at scoring time on purpose: scoring stays
a pure local computation over rows it has already loaded, with no network call inside an
O(authors x users) loop. One request per sync, never one per person.

What it deliberately does NOT do
--------------------------------
* It never deletes. `Publication.created_by` / `modified_by` are `SET_NULL`, so removing
  an ApiUser silently destroys publication provenance. Deactivated people are marked
  `active=False` and kept.
* It never writes `cilogon_id`, `access_expires`, `access_type` or `has_logged_in` on a
  row that already exists. Those belong to the login path. /journey-tracker/people does
  not return cilogon_id -- our login join key -- and that resolves itself: a synced row
  leaves it blank, and the first time that person authenticates, `auth_user_by_cookie` /
  `auth_user_by_token` look the row up by `uuid`, find it, and fill cilogon_id in.

Usage:
    python manage.py sync_fabric_users               # incremental: watermark -> now
    python manage.py sync_fabric_users --dry-run     # preview only, no writes
    python manage.py sync_fabric_users --full        # whole population, backwards
    python manage.py sync_fabric_users --since 2026-01-01
    python manage.py sync_fabric_users --if-due      # no-op unless the cadence elapsed
"""

import os
from datetime import datetime, timedelta, timezone

from django.core.management.base import BaseCommand, CommandError
from django.utils.dateparse import parse_datetime

from publicationtrkr.apps.apiuser.models import ApiUser, TaskTimeoutTracker
from publicationtrkr.apps.apiuser.utils.locks import SYNC_ADVISORY_LOCK_KEY, advisory_lock
from publicationtrkr.utils.core_api import (
    JOURNEY_TRACKER_MAX_WINDOW_DAYS,
    get_core_api_metrics_people,
    get_journey_tracker_people,
)
from publicationtrkr.utils.fabric_auth import split_fabric_roles

# Floor for --full. FABRIC's earliest `updated` timestamp is in April 2023; this leaves
# generous margin. Windows before the first record come back empty and cost one cheap
# request each, so the floor is a deliberate over-estimate rather than a tuned value.
FULL_SYNC_EARLIEST = datetime(2020, 1, 1, tzinfo=timezone.utc)

# Rewind the watermark by this much when saving it. A person whose record is updated
# while the sync is mid-run would otherwise fall in the gap between the window we read
# and the watermark we store. Re-reading an hour of records is free -- every write is an
# idempotent upsert keyed on uuid.
WATERMARK_OVERLAP = timedelta(hours=1)

# The advisory lock and its key moved to apiuser/utils/locks.py, which records why each
# key exists. score_author_claims (#32) needs the identical guard, and a copied locking
# primitive is one that drifts apart silently.

# Fields the sync owns. Everything else on ApiUser belongs to the login path.
SYNCED_FIELDS = ('active', 'affiliation', 'email', 'fabric_roles', 'name', 'projects')

# Fields the Scholar/Scopus pass owns (#32). Deliberately disjoint from SYNCED_FIELDS:
# they come from a different endpoint, under a different token, in a pass with a different
# shape, and one failing must not leave the other half-written.
METRICS_FIELDS = ('google_scholar', 'scopus')


def iter_windows(start, end, newest_first: bool = False):
    """
    Split [start, end) into consecutive chunks no wider than the endpoint's limit.

    /journey-tracker/people rejects a window wider than 90 days, so a backfill has to be
    walked rather than asked for in one call.
    """
    windows = []
    cursor = start
    while cursor < end:
        nxt = min(cursor + timedelta(days=JOURNEY_TRACKER_MAX_WINDOW_DAYS), end)
        windows.append((cursor, nxt))
        cursor = nxt
    if newest_first:
        windows.reverse()
    return windows


class Command(BaseCommand):
    help = 'Sync FABRIC users from core-api /journey-tracker/people into ApiUser.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Preview what would be written without touching the database.',
        )
        parser.add_argument(
            '--full',
            action='store_true',
            help='Backfill the whole population, ignoring the watermark.',
        )
        parser.add_argument(
            '--since',
            metavar='ISO',
            help='Sync records updated since this timestamp (e.g. 2026-01-01), '
                 'ignoring the watermark. UTC is assumed when no offset is given.',
        )
        parser.add_argument(
            '--if-due',
            action='store_true',
            help='Exit without syncing unless USR_TIMEOUT_IN_SECONDS has elapsed since '
                 'the last run. Lets the sidecar fire more often than the cadence.',
        )

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        full = options['full']
        since = options['since']
        if_due = options['if_due']

        if full and since:
            raise CommandError('--full and --since are mutually exclusive.')

        token = os.getenv('FABRIC_CORE_API_TOKEN')
        if not token:
            raise CommandError('FABRIC_CORE_API_TOKEN is not set (see env.template).')

        tracker = TaskTimeoutTracker.objects.filter(name=os.getenv('USR_NAME')).first()
        if tracker is None:
            self.stdout.write(self.style.WARNING(
                'No "{0}" TaskTimeoutTracker row -- run init_task_timeout_tracker. '
                'Proceeding without a watermark.'.format(os.getenv('USR_NAME'))
            ))

        if if_due and tracker is not None and not tracker.timed_out():
            self.stdout.write(
                'Not due: last run {0}, cadence {1}s. Nothing to do.'.format(
                    tracker.last_updated, tracker.timeout_in_seconds
                )
            )
            return

        now = datetime.now(timezone.utc)
        start, mode = self._resolve_start(full=full, since=since, tracker=tracker, now=now)
        windows = iter_windows(start, now, newest_first=(mode == 'full'))

        if dry_run:
            self.stdout.write(self.style.WARNING('DRY RUN - no changes will be written.\n'))
        self.stdout.write(
            'Mode         : {0}\n'
            'Window       : {1:%Y-%m-%d %H:%M} -> {2:%Y-%m-%d %H:%M} UTC '
            '({3} request(s))\n'.format(mode, start, now, len(windows))
        )

        with advisory_lock(SYNC_ADVISORY_LOCK_KEY) as acquired:
            if not acquired:
                self.stdout.write(self.style.WARNING(
                    'Another sync_fabric_users run holds the lock. Exiting without '
                    'syncing -- the watermark is left untouched for that run to advance.'
                ))
                return
            self._sync(windows=windows, token=token, dry_run=dry_run,
                       tracker=tracker, now=now)

    def _resolve_start(self, full, since, tracker, now):
        """Return (start, mode) -- where the walk begins and why."""
        if full:
            return FULL_SYNC_EARLIEST, 'full'
        if since:
            parsed = parse_datetime(since) if 'T' in since or ':' in since else None
            if parsed is None:
                try:
                    parsed = datetime.strptime(since, '%Y-%m-%d')
                except ValueError:
                    raise CommandError(
                        '--since "{0}" is not a recognisable date or timestamp.'.format(since)
                    )
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            if parsed >= now:
                raise CommandError('--since must be in the past.')
            return parsed, 'since'
        watermark = self._read_watermark(tracker)
        if watermark is None:
            self.stdout.write(self.style.WARNING(
                'No watermark recorded yet - falling back to a full backfill.'
            ))
            return FULL_SYNC_EARLIEST, 'full'
        return watermark, 'incremental'

    def _read_watermark(self, tracker):
        if tracker is None or not tracker.value:
            return None
        parsed = parse_datetime(tracker.value)
        if parsed is None:
            self.stdout.write(self.style.WARNING(
                'Watermark "{0}" is not parseable - ignoring it.'.format(tracker.value)
            ))
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)

    def _sync(self, windows, token, dry_run, tracker, now):
        anon_uuid = os.getenv('API_USER_ANON_UUID')
        # One read of the table up front, rather than a lookup per person. At ~3,300
        # rows this is a few MB and turns 2N queries into N.
        existing = {u.uuid: u for u in ApiUser.objects.all()}

        created = updated = unchanged = skipped = errors = 0
        missing_affiliation = 0
        seen = set()

        for window_start, window_end in windows:
            label = '{0:%Y-%m-%d} .. {1:%Y-%m-%d}'.format(window_start, window_end)
            try:
                people = get_journey_tracker_people(
                    start_date=window_start, end_date=window_end, token=token
                )
            except Exception as exc:
                # A window we could not read must abort the run. Continuing would let
                # the watermark advance past people we never saw, and they would stay
                # missing until someone noticed and ran --full.
                raise CommandError(
                    'Window {0} failed: {1}. Watermark not advanced.'.format(label, exc)
                )
            self.stdout.write('  {0}  {1:5d} record(s)'.format(label, len(people)))

            for person in people:
                try:
                    fabric_uuid = person.get('fabric_uuid')
                    if not fabric_uuid or fabric_uuid == anon_uuid:
                        skipped += 1
                        continue
                    if fabric_uuid in seen:
                        continue
                    seen.add(fabric_uuid)

                    if person.get('affiliation') is None:
                        missing_affiliation += 1

                    projects, fabric_roles = split_fabric_roles(person.get('fabric_roles'))
                    # affiliation / email / name are CharField(blank=True) and NOT NULL,
                    # so every null from the endpoint has to be coerced to ''.
                    values = {
                        'active': bool(person.get('active', True)),
                        'affiliation': person.get('affiliation') or '',
                        'email': person.get('email_address') or '',
                        'fabric_roles': fabric_roles,
                        'name': person.get('name') or '',
                        'projects': projects,
                    }

                    api_user = existing.get(fabric_uuid)
                    if api_user is None:
                        if not dry_run:
                            api_user = ApiUser(
                                uuid=fabric_uuid,
                                # Blank until this person's first login fills it in.
                                cilogon_id='',
                                access_expires=None,
                                has_logged_in=False,
                                last_synced=now,
                                **values,
                            )
                            api_user.save()
                            existing[fabric_uuid] = api_user
                        # Counted after the write, so a row that failed to save is
                        # reported once as an error rather than as both.
                        created += 1
                        continue

                    changed = [
                        field for field in SYNCED_FIELDS
                        if getattr(api_user, field) != values[field]
                    ]
                    if not dry_run:
                        for field in changed:
                            setattr(api_user, field, values[field])
                        api_user.last_synced = now
                        api_user.save(update_fields=changed + ['last_synced'])
                    if changed:
                        updated += 1
                    else:
                        unchanged += 1

                except Exception as exc:
                    self.stdout.write(self.style.ERROR(
                        '  ERROR  {0}  -  {1}'.format(person.get('fabric_uuid'), exc)
                    ))
                    errors += 1

        # After the window loop, so people this run created are in `existing` and get
        # their identifiers on the same pass that created them; before the watermark,
        # so the whole report is one block.
        metrics = self._sync_metrics(existing=existing, dry_run=dry_run)

        watermark_note = 'not advanced (dry run)'
        if not dry_run and tracker is not None:
            tracker.value = (now - WATERMARK_OVERLAP).isoformat()
            tracker.last_updated = now
            tracker.save(update_fields=['value', 'last_updated'])
            watermark_note = tracker.value
        elif not dry_run:
            watermark_note = 'not advanced (no tracker row)'

        self.stdout.write('\n--- Summary ---')
        self.stdout.write('People seen  : {0}'.format(len(seen)))
        if dry_run:
            self.stdout.write(self.style.WARNING('Would create : {0}'.format(created)))
            self.stdout.write(self.style.WARNING('Would update : {0}'.format(updated)))
        else:
            self.stdout.write(self.style.SUCCESS('Created      : {0}'.format(created)))
            self.stdout.write(self.style.SUCCESS('Updated      : {0}'.format(updated)))
        self.stdout.write('Unchanged    : {0}'.format(unchanged))
        self.stdout.write('Skipped      : {0}'.format(skipped))
        if missing_affiliation:
            # core-api resolved the whole 19% backlog in v1.11.5 (fabric-core-api-dev
            # #224), so a non-zero count here means the regression came back.
            self.stdout.write(self.style.WARNING(
                'No affiliation: {0}  (coerced to "")'.format(missing_affiliation)
            ))
        if errors:
            self.stdout.write(self.style.ERROR('Errors       : {0}'.format(errors)))
        else:
            self.stdout.write('Errors       : {0}'.format(errors))
        if metrics is None:
            self.stdout.write('Scholar/Scopus: pass did not run')
        else:
            self.stdout.write(
                'Scholar/Scopus: {0} of {1} person record(s) carry an identifier; '
                '{2} row(s) {3}, {4} uuid(s) not in the directory'.format(
                    metrics['with_ids'], metrics['seen'], metrics['changed'],
                    'would change' if dry_run else 'changed', metrics['unknown'],
                )
            )
        self.stdout.write('Watermark    : {0}'.format(watermark_note))

    def _sync_metrics(self, existing, dry_run):
        """
        Refresh google_scholar / scopus from /core-api-metrics/people (#32).

        Its own pass rather than a step inside the per-window loop, because the two
        endpoints have different shapes: /journey-tracker/people is windowed and
        incremental, /core-api-metrics/people is one unwindowed snapshot of the whole
        population. Folding it into the loop would refresh identifiers only for people
        whose journey-tracker record happened to change in that window, and would repeat
        the same full-population request once per window.

        Returns a counts dict, or None when the pass did not run.

        Deliberately does **not** stamp `last_synced`. That field means "the
        journey-tracker sync touched this row", and this endpoint returns everybody every
        time -- stamping it would set it to now for all 3,300 people on every run and
        flatten the only staleness signal the directory has.
        """
        token = os.getenv('FABRIC_CORE_API_SERVICES_TOKEN')
        if not token:
            # Warn and skip, never raise. This is a second, disjoint credential: the
            # services token cannot read /journey-tracker/people and the readonly token
            # cannot read this endpoint. Copying the CommandError guard above would turn a
            # working nightly directory sync into a hard failure on any host whose .env
            # lagged the restart -- the same shape as the USR_* triple that bit the 1.12.0
            # deploy. The identifier columns simply keep the values they already hold.
            self.stdout.write(self.style.WARNING(
                'FABRIC_CORE_API_SERVICES_TOKEN is not set -- skipping the Scholar/Scopus '
                'pass and leaving those columns untouched. The directory sync above is '
                'unaffected. See env.template.'
            ))
            return None

        try:
            people = get_core_api_metrics_people(token=token)
        except Exception as exc:
            # Reported, not raised, and specifically *not* a CommandError. Unlike a
            # journey-tracker window -- where a silent gap hides people until someone runs
            # --full -- nothing is lost by not refreshing a weak prior tonight: the columns
            # keep yesterday's values and tomorrow's run picks them up. The fetcher raises
            # rather than returning [] so that "we could not ask" is distinguishable from
            # "nobody has an identifier" right here, which is what makes this branch
            # possible at all.
            self.stdout.write(self.style.ERROR(
                'Scholar/Scopus pass failed: {0}. Identifiers left unchanged.'.format(exc)
            ))
            return None

        changed_rows = unknown = with_ids = 0
        seen = set()
        for person in people:
            uuid = person.get('uuid')
            if not uuid or uuid in seen:
                continue
            seen.add(uuid)
            values = {
                # Both columns are CharField(blank=True) and NOT NULL, and the endpoint
                # returns null far more often than not, so every value is coerced to ''.
                'google_scholar': person.get('google_scholar') or '',
                'scopus': person.get('scopus') or '',
            }
            if values['google_scholar'] or values['scopus']:
                with_ids += 1

            api_user = existing.get(uuid)
            if api_user is None:
                # Known to core-api but not in our directory. Not created here: this
                # endpoint carries no name, email, affiliation or roles, so the row would
                # be a uuid with two identifiers on it and nothing to name-match against.
                # /journey-tracker/people is the only thing that creates ApiUser rows.
                unknown += 1
                continue

            changed = [f for f in METRICS_FIELDS if getattr(api_user, f) != values[f]]
            if not changed:
                continue
            changed_rows += 1
            if not dry_run:
                for field in changed:
                    setattr(api_user, field, values[field])
                # update_fields is exactly the changed identifier columns -- no
                # last_synced, and nothing the journey-tracker pass owns.
                api_user.save(update_fields=changed)

        return {
            'seen': len(seen),
            'with_ids': with_ids,
            'changed': changed_rows,
            'unknown': unknown,
        }

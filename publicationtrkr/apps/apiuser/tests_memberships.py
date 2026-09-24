"""
Tests for project membership history (#72): the upsert and the 0006 seed.

What is worth pinning is what the table must *never* do -- lose a row, move `last_seen`
backwards, or forget the earliest `first_seen` -- because each of those silently erases
the scoring evidence the table exists to keep, and nothing downstream would notice.
"""

import importlib.util
import os
import tempfile
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from importlib import import_module
from io import StringIO
from pathlib import Path
from unittest import mock

from django.apps import apps as global_apps
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from publicationtrkr.apps.apiuser.models import ApiUser, ApiUserProjectMembership
# The module, not the class: a TestCase imported by name is collected here too, and
# every sync test would run twice.
from publicationtrkr.apps.apiuser import tests as sync_tests
from publicationtrkr.apps.apiuser.utils.memberships import record_memberships
from publicationtrkr.utils.fabric_auth import save_refreshed_user

seed_memberships = import_module(
    'publicationtrkr.apps.apiuser.migrations.0006_project_membership'
).seed_memberships

PROJECT_A = 'f0e4a6c1-1111-4a2b-9c3d-000000000001'
PROJECT_B = 'f0e4a6c1-2222-4a2b-9c3d-000000000002'
PROJECT_C = 'f0e4a6c1-3333-4a2b-9c3d-000000000003'

DAY_1 = datetime(2026, 9, 1, 3, 0, tzinfo=timezone.utc)
DAY_2 = DAY_1 + timedelta(days=1)
DAY_3 = DAY_1 + timedelta(days=2)


class RecordMembershipsTests(TestCase):

    def setUp(self):
        self.user = ApiUser.objects.create(uuid='user-1', name='Jane Smith')

    def rows(self):
        return {
            m.project_uuid: m for m in
            ApiUserProjectMembership.objects.filter(api_user=self.user)
        }

    def test_first_observation_inserts_one_row_per_project(self):
        new = record_memberships(self.user.id, [PROJECT_A, PROJECT_B], DAY_1, 'sync')
        self.assertEqual(new, 2)
        rows = self.rows()
        self.assertEqual(set(rows), {PROJECT_A, PROJECT_B})
        for row in rows.values():
            self.assertEqual((row.first_seen, row.last_seen, row.source), (DAY_1, DAY_1, 'sync'))

    def test_leaving_a_project_keeps_its_row_and_its_last_seen(self):
        # Leaving and a project expiring look identical from here: the project is simply
        # no longer in the list. Either way the row is the evidence and must survive.
        record_memberships(self.user.id, [PROJECT_A, PROJECT_B], DAY_1, 'sync')
        new = record_memberships(self.user.id, [PROJECT_A], DAY_2, 'sync')
        self.assertEqual(new, 0)
        rows = self.rows()
        self.assertEqual(set(rows), {PROJECT_A, PROJECT_B})
        self.assertEqual(rows[PROJECT_A].last_seen, DAY_2)
        self.assertEqual(rows[PROJECT_B].last_seen, DAY_1)

    def test_an_empty_list_deletes_nothing(self):
        # Someone whose last project expired arrives with projects=[]. That must read as
        # "nothing to refresh", never as "clear their history".
        record_memberships(self.user.id, [PROJECT_A], DAY_1, 'sync')
        self.assertEqual(record_memberships(self.user.id, [], DAY_2, 'sync'), 0)
        self.assertEqual(record_memberships(self.user.id, None, DAY_2, 'sync'), 0)
        self.assertEqual(self.rows()[PROJECT_A].last_seen, DAY_1)

    def test_rejoining_keeps_the_original_first_seen(self):
        record_memberships(self.user.id, [PROJECT_A], DAY_1, 'sync')
        record_memberships(self.user.id, [], DAY_2, 'sync')
        record_memberships(self.user.id, [PROJECT_A], DAY_3, 'login')
        row = self.rows()[PROJECT_A]
        self.assertEqual((row.first_seen, row.last_seen, row.source), (DAY_1, DAY_3, 'sync'))

    def test_an_older_observation_never_moves_last_seen_backwards(self):
        # The dump import replays snapshots older than what the sync already wrote.
        record_memberships(self.user.id, [PROJECT_A], DAY_3, 'sync')
        record_memberships(self.user.id, [PROJECT_A], DAY_1, 'seed')
        row = self.rows()[PROJECT_A]
        self.assertEqual(row.last_seen, DAY_3)
        # ...but it does move first_seen earlier, and `source` follows the date it now
        # describes: that first_seen came from a seed, so it is an observation date only.
        self.assertEqual((row.first_seen, row.source), (DAY_1, 'seed'))

    def test_a_later_observation_does_not_relabel_the_source(self):
        record_memberships(self.user.id, [PROJECT_A], DAY_1, 'seed')
        record_memberships(self.user.id, [PROJECT_A], DAY_2, 'sync')
        row = self.rows()[PROJECT_A]
        self.assertEqual((row.first_seen, row.last_seen, row.source), (DAY_1, DAY_2, 'seed'))

    def test_replaying_the_same_observation_is_idempotent(self):
        record_memberships(self.user.id, [PROJECT_A, PROJECT_B], DAY_1, 'seed')
        before = list(ApiUserProjectMembership.objects.values().order_by('id'))
        self.assertEqual(record_memberships(self.user.id, [PROJECT_A, PROJECT_B], DAY_1, 'seed'), 0)
        self.assertEqual(list(ApiUserProjectMembership.objects.values().order_by('id')), before)

    def test_duplicates_and_blanks_are_dropped(self):
        # Postgres rejects an ON CONFLICT statement that touches the same row twice.
        new = record_memberships(self.user.id, [PROJECT_A, PROJECT_A, '', PROJECT_B], DAY_1, 'sync')
        self.assertEqual(new, 2)
        self.assertEqual(set(self.rows()), {PROJECT_A, PROJECT_B})

    def test_memberships_are_per_user(self):
        other = ApiUser.objects.create(uuid='user-2', name='John Doe')
        record_memberships(self.user.id, [PROJECT_A], DAY_1, 'sync')
        self.assertEqual(record_memberships(other.id, [PROJECT_A], DAY_2, 'sync'), 1)
        self.assertEqual(self.rows()[PROJECT_A].last_seen, DAY_1)

    def test_an_unknown_source_is_refused(self):
        with self.assertRaises(ValueError):
            record_memberships(self.user.id, [PROJECT_A], DAY_1, 'guess')
        self.assertFalse(ApiUserProjectMembership.objects.exists())

    def test_history_does_not_change_current_membership(self):
        # Authorization reads ApiUser.projects and nothing else. A former membership
        # must never read as a current one.
        self.user.projects = [PROJECT_A]
        self.user.save()
        record_memberships(self.user.id, [PROJECT_A, PROJECT_B], DAY_1, 'sync')
        self.user.refresh_from_db()
        self.assertEqual(self.user.projects, [PROJECT_A])
        self.assertTrue(self.user.is_project_member(PROJECT_A))
        self.assertFalse(self.user.is_project_member(PROJECT_B))


class SeedMembershipsMigrationTests(TestCase):
    """Calls the 0006 RunPython directly, as tests_migrations.py does for publications."""

    def run_seed(self):
        with mock.patch('builtins.print') as printed:
            seed_memberships(global_apps, None)
        return '\n'.join(str(call.args[0]) for call in printed.call_args_list)

    def test_one_row_per_current_project_dated_by_last_synced(self):
        synced = ApiUser.objects.create(
            uuid='user-1', name='Jane Smith', projects=[PROJECT_A, PROJECT_B], last_synced=DAY_2)
        output = self.run_seed()
        rows = ApiUserProjectMembership.objects.filter(api_user=synced)
        self.assertEqual({r.project_uuid for r in rows}, {PROJECT_A, PROJECT_B})
        for row in rows:
            self.assertEqual((row.first_seen, row.last_seen, row.source), (DAY_2, DAY_2, 'seed'))
        self.assertIn('seeded 2 project membership(s) for 1 user(s)', output)

    def test_a_login_only_row_is_dated_now(self):
        ApiUser.objects.create(uuid='user-1', name='Jane Smith', projects=[PROJECT_C])
        before = datetime.now(timezone.utc)
        self.run_seed()
        row = ApiUserProjectMembership.objects.get()
        self.assertGreaterEqual(row.first_seen, before)
        self.assertEqual(row.first_seen, row.last_seen)

    def test_users_without_projects_and_bad_elements_are_skipped(self):
        ApiUser.objects.create(uuid='anon', name='')
        ApiUser.objects.create(
            uuid='user-1', name='Jane Smith', projects=[PROJECT_A, PROJECT_A, ''], last_synced=DAY_1)
        output = self.run_seed()
        self.assertEqual(ApiUserProjectMembership.objects.count(), 1)
        self.assertIn('seeded 1 project membership(s) for 1 user(s)', output)


class SyncRecordsMembershipsTests(TestCase):
    """
    sync_fabric_users writing history. Borrows the harness from tests.py rather than
    subclassing it, which would run every sync test a second time.
    """

    setUp = sync_tests.SyncFabricUsersTests.setUp
    run_sync = sync_tests.SyncFabricUsersTests.run_sync

    def sync(self, people, *args):
        out = StringIO()
        with redirect_stdout(out):
            self.run_sync(people, '--since', '2026-08-01', *args)
        return out.getvalue()

    def roles(self, *projects):
        return ['Jupyterhub'] + [p + '-pm' for p in projects]

    def existing(self, projects, history=None):
        user = ApiUser.objects.create(
            uuid='u-1', name='Some One', affiliation='Example University',
            email='someone@example.edu', fabric_roles=['Jupyterhub'], projects=projects)
        for project_uuid in (projects if history is None else history):
            record_memberships(user.id, [project_uuid], DAY_1, 'seed')
        return user

    def memberships(self):
        return {m.project_uuid: m for m in ApiUserProjectMembership.objects.all()}

    def test_a_created_user_gets_history_on_the_same_pass(self):
        output = self.sync([sync_tests.person('u-1', fabric_roles=self.roles(PROJECT_A, PROJECT_B))])
        rows = self.memberships()
        self.assertEqual(set(rows), {PROJECT_A, PROJECT_B})
        self.assertEqual({r.source for r in rows.values()}, {'sync'})
        self.assertIn('Memberships  : 2 new recorded; 0 no longer current', output)

    def test_leaving_a_project_updates_current_membership_and_keeps_history(self):
        user = self.existing([PROJECT_A, PROJECT_B])
        output = self.sync([sync_tests.person('u-1', fabric_roles=self.roles(PROJECT_A))])
        user.refresh_from_db()
        # Current membership follows Core API -- that is what authorization reads...
        self.assertEqual(user.projects, [PROJECT_A])
        self.assertFalse(user.is_project_member(PROJECT_B))
        # ...and the history keeps the membership that ended, frozen at its last sighting.
        rows = self.memberships()
        self.assertEqual(set(rows), {PROJECT_A, PROJECT_B})
        self.assertGreater(rows[PROJECT_A].last_seen, DAY_1)
        self.assertEqual(rows[PROJECT_B].last_seen, DAY_1)
        self.assertIn('Memberships  : 0 new recorded; 1 no longer current', output)

    def test_an_expired_last_project_leaves_history_intact(self):
        self.existing([PROJECT_A])
        self.sync([sync_tests.person('u-1', fabric_roles=['Jupyterhub'])])
        self.assertEqual(self.memberships()[PROJECT_A].last_seen, DAY_1)

    def test_an_unchanged_user_still_refreshes_last_seen(self):
        # No field changed, so the ApiUser row only gets its last_synced stamp -- but the
        # memberships were observed tonight all the same.
        self.existing([PROJECT_A])
        output = self.sync([sync_tests.person('u-1', fabric_roles=self.roles(PROJECT_A))])
        self.assertIn('Unchanged    : 1', output)
        self.assertGreater(self.memberships()[PROJECT_A].last_seen, DAY_1)

    def test_a_dry_run_counts_against_history_and_writes_nothing(self):
        # Stored row says A and B; history knows only A; Core API now says A and C.
        # C is new to history, B has ended. A is neither.
        self.existing([PROJECT_A, PROJECT_B], history=[PROJECT_A])
        before = list(ApiUserProjectMembership.objects.values().order_by('id'))
        output = self.sync([
            sync_tests.person('u-1', fabric_roles=self.roles(PROJECT_A, PROJECT_C)),
            sync_tests.person('u-2', fabric_roles=self.roles(PROJECT_B)),
        ], '--dry-run')
        self.assertEqual(list(ApiUserProjectMembership.objects.values().order_by('id')), before)
        self.assertIn('Memberships  : 2 new would be recorded; 1 no longer current', output)

    def test_the_row_and_its_history_commit_together(self):
        # A history write that fails must not leave the ApiUser row updated without it:
        # the membership that ended would then be gone from `projects` with no record.
        user = self.existing([PROJECT_A, PROJECT_B])
        with mock.patch(
            'publicationtrkr.apps.apiuser.management.commands.sync_fabric_users'
            '.record_memberships', side_effect=RuntimeError('boom'),
        ), self.assertRaises(CommandError):
            self.sync([sync_tests.person('u-1', fabric_roles=self.roles(PROJECT_A))])
        user.refresh_from_db()
        self.assertEqual(user.projects, [PROJECT_A, PROJECT_B])


class LoginRecordsMembershipsTests(TestCase):

    def test_a_login_refresh_records_current_projects(self):
        user = ApiUser.objects.create(uuid='u-1', name='Some One', projects=[PROJECT_A])
        user.projects = [PROJECT_A, PROJECT_B]
        save_refreshed_user(user)
        rows = {m.project_uuid: m for m in ApiUserProjectMembership.objects.filter(api_user=user)}
        self.assertEqual(set(rows), {PROJECT_A, PROJECT_B})
        self.assertEqual({r.source for r in rows.values()}, {'login'})

    def test_a_brand_new_login_row_gets_history(self):
        # auth_user_by_* builds an unsaved ApiUser for someone the directory has never
        # seen; the id the history needs only exists after the save.
        user = ApiUser(uuid='u-new', name='New Person', projects=[PROJECT_C])
        save_refreshed_user(user)
        self.assertEqual(
            list(ApiUserProjectMembership.objects.values_list('api_user_id', 'project_uuid')),
            [(user.id, PROJECT_C)])

    def test_a_login_with_no_projects_deletes_nothing(self):
        user = ApiUser.objects.create(uuid='u-1', name='Some One', projects=[PROJECT_A])
        record_memberships(user.id, [PROJECT_A], DAY_1, 'sync')
        user.projects = []
        save_refreshed_user(user)
        self.assertEqual(ApiUserProjectMembership.objects.get().last_seen, DAY_1)


def load_extract_script():
    """scripts/ is not a package and the file name has hyphens, so load it by path."""
    path = Path(__file__).resolve().parents[3] / 'scripts' / 'extract-memberships-from-dump.py'
    spec = importlib.util.spec_from_file_location('extract_memberships', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


extract_script = load_extract_script()

DUMPED_AT = datetime(2026, 9, 21, 18, 53, 39, tzinfo=timezone.utc)
COPY_HEADER = ('COPY public.apiuser_apiuser (id, access_expires, uuid, projects, last_synced, '
               'affiliation) FROM stdin;\n')


class ExtractMembershipsScriptTests(TestCase):
    """The dump parser. Checked by hand against a real pg_dump as well; these pin the edges."""

    def extract(self, *rows, terminated=True):
        lines = ['SET client_encoding = \'UTF8\';\n', COPY_HEADER]
        lines += ['\t'.join(row) + '\n' for row in rows]
        if terminated:
            lines.append('\\.\n')
        return list(extract_script.extract(lines, DUMPED_AT))

    def test_one_row_per_project_dated_by_the_last_write(self):
        rows = self.extract(
            ('1', '\\N', 'u-1', '{%s,%s}' % (PROJECT_B, PROJECT_A), '2026-09-01 03:00:00+00', 'U'),
            ('2', '2026-09-10 12:05:00+00', 'u-2', '{%s}' % PROJECT_C,
             '2026-09-01 03:00:00+00', 'U'),
        )
        self.assertEqual(rows, [
            ('u-1', PROJECT_A, DAY_1), ('u-1', PROJECT_B, DAY_1),
            # The later of last_synced and access_expires: this person's login refresh
            # wrote projects after the directory sync did.
            ('u-2', PROJECT_C, datetime(2026, 9, 10, 12, 5, tzinfo=timezone.utc)),
        ])

    def test_the_dump_time_caps_and_backstops_observed_at(self):
        rows = self.extract(
            ('1', '2026-09-21 18:58:00+00', 'u-1', '{%s}' % PROJECT_A, '\\N', 'U'),
            ('2', '\\N', 'u-2', '{%s}' % PROJECT_B, '\\N', 'U'),
        )
        self.assertEqual([r[2] for r in rows], [DUMPED_AT, DUMPED_AT])

    def test_empty_null_and_quoted_arrays(self):
        rows = self.extract(
            ('1', '\\N', 'u-1', '{}', '\\N', 'U'),
            ('2', '\\N', 'u-2', '\\N', '\\N', 'U'),
            ('3', '\\N', 'u-3', '{"odd, project",%s,%s}' % (PROJECT_A, PROJECT_A), '\\N', 'U'),
        )
        self.assertEqual([r[:2] for r in rows], [('u-3', PROJECT_A), ('u-3', 'odd, project')])

    def test_escaped_fields_do_not_shift_columns(self):
        # A tab inside a value is written as the two characters \t, so splitting the line
        # on real tabs stays aligned.
        rows = self.extract(('1', '\\N', 'u-1', '{%s}' % PROJECT_A, '\\N', 'Tab\\tAff'))
        self.assertEqual(rows[0][:2], ('u-1', PROJECT_A))

    def test_a_dump_without_the_table_or_with_a_truncated_block_fails(self):
        with self.assertRaisesRegex(extract_script.DumpError, 'no COPY block'):
            list(extract_script.extract(['SET x = 1;\n'], DUMPED_AT))
        with self.assertRaisesRegex(extract_script.DumpError, 'not terminated'):
            self.extract(('1', '\\N', 'u-1', '{}', '\\N', 'U'), terminated=False)

    def test_timestamps_parse_without_fromisoformat(self):
        # The prod host's python3 is 3.6, which has no datetime.fromisoformat.
        parse = extract_script.parse_timestamp
        self.assertEqual(parse('2026-09-01 03:00:00+00'), DAY_1)
        self.assertEqual(parse('2026-09-01 03:00:00.5+00'), DAY_1.replace(microsecond=500000))
        self.assertEqual(parse('2026-08-31 23:00:00-04'), DAY_1)
        self.assertEqual(parse('2026-09-01 08:30:00+05:30'), DAY_1)
        self.assertEqual(parse('2026-09-21T18:53:39+00:00'), DUMPED_AT)
        self.assertIsNone(parse('2026-09-01 03:00:00').tzinfo)
        with self.assertRaises(extract_script.DumpError):
            parse('yesterday')

    def test_the_dump_time_is_read_from_the_backup_name(self):
        self.assertEqual(
            extract_script.dump_time_from_name('backups/pre-full-sync-v1.20.0-20260921-185339.sql.gz'),
            DUMPED_AT)
        # The v1.15.0-era backups use the compact ISO form.
        self.assertEqual(
            extract_script.dump_time_from_name('pubtrkr-db-pre-v1.15.0-20260904T220002Z.sql'),
            datetime(2026, 9, 4, 22, 0, 2, tzinfo=timezone.utc))
        self.assertIsNone(extract_script.dump_time_from_name('dump.sql.gz'))

    def test_a_dump_from_before_last_synced_existed_is_still_read(self):
        lines = [
            'COPY public.apiuser_apiuser (id, access_expires, uuid, projects) FROM stdin;\n',
            '1\t2026-08-30 10:00:00+00\tu-1\t{%s}\n' % PROJECT_A,
            '\\.\n',
        ]
        self.assertEqual(list(extract_script.extract(lines, DUMPED_AT)), [
            ('u-1', PROJECT_A, datetime(2026, 8, 30, 10, 0, tzinfo=timezone.utc))])


class ImportProjectMembershipsTests(TestCase):

    def setUp(self):
        self.user = ApiUser.objects.create(uuid='u-1', name='Some One', projects=[PROJECT_A])

    def write_csv(self, *rows, header='uuid,project_uuid,observed_at'):
        handle = tempfile.NamedTemporaryFile('w', suffix='.csv', delete=False)
        self.addCleanup(os.unlink, handle.name)
        handle.write('\n'.join([header] + [','.join(r) for r in rows]) + '\n')
        handle.close()
        return handle.name

    def run_import(self, path, *args):
        out = StringIO()
        call_command('import_project_memberships', path, *args, stdout=out)
        return out.getvalue()

    def test_imports_ended_memberships_as_seed_and_reports_them(self):
        path = self.write_csv(('u-1', PROJECT_A, DAY_1.isoformat()),
                              ('u-1', PROJECT_B, DAY_1.isoformat()))
        output = self.run_import(path)
        rows = {m.project_uuid: m for m in ApiUserProjectMembership.objects.all()}
        self.assertEqual(set(rows), {PROJECT_A, PROJECT_B})
        self.assertEqual({r.source for r in rows.values()}, {'seed'})
        self.assertIn('New memberships        : 2', output)
        # B is the ended membership -- the evidence this import exists to recover.
        self.assertIn('of which not current : 1', output)
        # History, not current membership: authorization is untouched.
        self.user.refresh_from_db()
        self.assertEqual(self.user.projects, [PROJECT_A])

    def test_a_dry_run_reports_exactly_and_writes_nothing(self):
        path = self.write_csv(('u-1', PROJECT_B, DAY_1.isoformat()))
        output = self.run_import(path, '--dry-run')
        self.assertIn('DRY RUN', output)
        self.assertIn('New memberships        : 1 (would be written)', output)
        self.assertFalse(ApiUserProjectMembership.objects.exists())

    def test_replaying_and_older_dumps_never_undo_newer_history(self):
        record_memberships(self.user.id, [PROJECT_A], DAY_3, 'sync')
        path = self.write_csv(('u-1', PROJECT_A, DAY_1.isoformat()))
        self.run_import(path)
        output = self.run_import(path)
        row = ApiUserProjectMembership.objects.get()
        self.assertEqual((row.first_seen, row.last_seen, row.source), (DAY_1, DAY_3, 'seed'))
        self.assertIn('Already in history     : 1', output)

    def test_unknown_uuids_are_skipped_not_created(self):
        path = self.write_csv(('u-gone', PROJECT_A, DAY_1.isoformat()))
        output = self.run_import(path)
        self.assertIn('Unknown uuids skipped  : 1', output)
        self.assertFalse(ApiUser.objects.filter(uuid='u-gone').exists())
        self.assertFalse(ApiUserProjectMembership.objects.exists())

    def test_a_bad_file_is_refused_before_anything_is_written(self):
        for rows, header, message in (
            ((), 'uuid,project', 'expected header'),
            ((('u-1', PROJECT_A, DAY_1.isoformat()), ('u-1', PROJECT_B, '2026-09-01')),
             'uuid,project_uuid,observed_at', 'line 3'),
            ((('u-1', '', DAY_1.isoformat()),), 'uuid,project_uuid,observed_at', 'line 2'),
        ):
            with self.subTest(message=message), self.assertRaisesRegex(CommandError, message):
                self.run_import(self.write_csv(*rows, header=header))
        self.assertFalse(ApiUserProjectMembership.objects.exists())

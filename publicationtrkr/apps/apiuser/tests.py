"""
Tests for the FABRIC user sync (issue #23, and its Scholar/Scopus pass from #32).

Covers the two things most likely to break silently: the role parser, which decides
what counts as project membership for every user in the system, and the sync's field
ownership rule, which is the difference between a resync refreshing a directory entry
and a resync logging someone out.

The v1.15.0 Scholar/Scopus pass reads a *second* core-api endpoint under a *second*
token, so it gets its own stub target -- and `run_sync` stubs it unconditionally, or
every test in this file would make a live request the moment a services token appears
in the environment.
"""

import os
from datetime import datetime, timedelta, timezone
from io import StringIO
from unittest import mock

from django.core.management import call_command
from django.test import SimpleTestCase, TestCase

from publicationtrkr.apps.apiuser.models import ApiUser, TaskTimeoutTracker
from publicationtrkr.utils.fabric_auth import split_fabric_roles

PROJECT_A = 'f0e4a6c1-1111-4a2b-9c3d-000000000001'
PROJECT_B = 'f0e4a6c1-2222-4a2b-9c3d-000000000002'


class SplitFabricRolesTests(SimpleTestCase):
    """No database: split_fabric_roles is a pure function over role names."""

    def test_all_three_project_suffixes_are_recognised(self):
        # -tk is rare (30 people across the whole population) but real, so it has to be
        # in the set. -pc was removed in core-api v1.10.0 and must not be.
        projects, fabric_roles = split_fabric_roles([
            PROJECT_A + '-pm', PROJECT_A + '-po', PROJECT_B + '-tk',
        ])
        self.assertEqual(projects, sorted([PROJECT_A, PROJECT_B]))
        self.assertEqual(fabric_roles, [])

    def test_global_roles_fall_through(self):
        # The global set is open-ended on purpose: project-leads and
        # artifact-manager-admins were not in the catalogued env-var list but appear in
        # the real population, and a role we have never seen must still land somewhere.
        globals_seen = [
            'Jupyterhub', 'fabric-active-users', 'publication-tracker-admins',
            'project-leads', 'facility-operators', 'some-future-role',
        ]
        projects, fabric_roles = split_fabric_roles(globals_seen)
        self.assertEqual(projects, [])
        self.assertEqual(fabric_roles, sorted(globals_seen))

    def test_a_removed_suffix_is_not_a_project(self):
        # '-pc' leaves a valid UUID behind, so the old "strip any three characters"
        # idiom would have classified it as project membership. The explicit suffix set
        # is what stops that.
        projects, fabric_roles = split_fabric_roles([PROJECT_A + '-pc'])
        self.assertEqual(projects, [])
        self.assertEqual(fabric_roles, [PROJECT_A + '-pc'])

    def test_output_is_deduplicated_and_sorted(self):
        # A stable value means a resync does not record a spurious change.
        projects, fabric_roles = split_fabric_roles([
            PROJECT_B + '-pm', PROJECT_A + '-pm', PROJECT_A + '-po',
            'Jupyterhub', 'Jupyterhub',
        ])
        self.assertEqual(projects, sorted([PROJECT_A, PROJECT_B]))
        self.assertEqual(fabric_roles, ['Jupyterhub'])

    def test_empty_and_missing_input(self):
        self.assertEqual(split_fabric_roles(None), ([], []))
        self.assertEqual(split_fabric_roles([]), ([], []))
        self.assertEqual(split_fabric_roles(['', None]), ([], []))


def person(uuid, **overrides):
    """One /journey-tracker/people row."""
    row = {
        'active': True,
        'affiliation': 'Example University',
        'deactivated_date': None,
        'email_address': 'someone@example.edu',
        'fabric_last_seen': '2026-08-31 23:58:12+00:00',
        'fabric_registered_on': '2026-08-31 20:54:14+00:00',
        'fabric_roles': ['Jupyterhub', 'fabric-active-users', PROJECT_A + '-pm'],
        'fabric_uuid': uuid,
        'name': 'Some One',
    }
    row.update(overrides)
    return row


def metrics_person(uuid, **overrides):
    """One /core-api-metrics/people row. Note `uuid`, not `fabric_uuid`."""
    row = {
        'active': True,
        'bastion_login': 'someone_0000000000',
        'google_scholar': None,
        'last_updated': '2026-09-04 12:00:00+00:00',
        'scopus': None,
        'uuid': uuid,
    }
    row.update(overrides)
    return row


class SyncFabricUsersTests(TestCase):

    def setUp(self):
        TaskTimeoutTracker.objects.create(
            description='User Sync Check', last_updated=datetime.now(timezone.utc),
            name='user_sync_check', timeout_in_seconds=86400,
            uuid='11111111-1111-1111-1111-111111111111', value=None,
        )

    def run_sync(self, people, *args, metrics=None, metrics_error=None):
        """
        Run the command with both core-api calls stubbed.

        The Scholar/Scopus stub is not optional. It is a separate endpoint under a
        separate token, and leaving it unstubbed would make every test in this file issue
        a live request as soon as FABRIC_CORE_API_SERVICES_TOKEN is present in the
        environment -- which it is, in any checkout configured to run the sync.
        """
        metrics_kwargs = (
            {'side_effect': metrics_error} if metrics_error is not None
            else {'return_value': metrics or []}
        )
        with mock.patch(
            'publicationtrkr.apps.apiuser.management.commands.sync_fabric_users'
            '.get_journey_tracker_people',
            side_effect=lambda start_date, end_date, token: people,
        ), mock.patch(
            'publicationtrkr.apps.apiuser.management.commands.sync_fabric_users'
            '.get_core_api_metrics_people',
            **metrics_kwargs,
        ), mock.patch.dict(
            os.environ, {'FABRIC_CORE_API_SERVICES_TOKEN': 'test-services-token'}
        ):
            call_command('sync_fabric_users', *args)

    def test_creates_a_synced_row_without_login_fields(self):
        self.run_sync([person('u-1')], '--since', '2026-08-01')
        api_user = ApiUser.objects.get(uuid='u-1')
        self.assertEqual(api_user.name, 'Some One')
        self.assertEqual(api_user.projects, [PROJECT_A])
        self.assertEqual(api_user.fabric_roles, ['Jupyterhub', 'fabric-active-users'])
        # cilogon_id is our login join key and the endpoint does not return one. It
        # stays blank until this person's first login fills it in.
        self.assertEqual(api_user.cilogon_id, '')
        self.assertIsNone(api_user.access_expires)
        self.assertFalse(api_user.has_logged_in)
        self.assertIsNotNone(api_user.last_synced)

    def test_null_affiliation_is_coerced_to_empty_string(self):
        # affiliation is CharField(blank=True) and NOT NULL. core-api resolved its 19%
        # null backlog in v1.11.5, but the sync must not depend on that holding.
        self.run_sync([person('u-2', affiliation=None, email_address=None, name=None)],
                      '--since', '2026-08-01')
        api_user = ApiUser.objects.get(uuid='u-2')
        self.assertEqual(api_user.affiliation, '')
        self.assertEqual(api_user.email, '')
        self.assertEqual(api_user.name, '')

    def test_resync_never_clobbers_the_login_path_fields(self):
        expires = datetime.now(timezone.utc) + timedelta(days=1)
        ApiUser.objects.create(
            uuid='u-3', name='Stale Name', affiliation='Stale Affiliation', email='',
            cilogon_id='http://cilogon.org/serverA/users/999', access_expires=expires,
            access_type=ApiUser.TOKEN, has_logged_in=True,
        )
        self.run_sync([person('u-3')], '--since', '2026-08-01')
        api_user = ApiUser.objects.get(uuid='u-3')
        # Directory fields refresh...
        self.assertEqual(api_user.name, 'Some One')
        self.assertEqual(api_user.affiliation, 'Example University')
        # ...session fields do not. Overwriting these would sign the user out and
        # orphan the cilogon_id lookup in utils/fabric_auth.py.
        self.assertEqual(api_user.cilogon_id, 'http://cilogon.org/serverA/users/999')
        self.assertEqual(api_user.access_expires, expires)
        self.assertEqual(api_user.access_type, ApiUser.TOKEN)
        self.assertTrue(api_user.has_logged_in)

    def test_deactivated_people_are_marked_not_deleted(self):
        # created_by / modified_by are SET_NULL, so deleting an ApiUser silently
        # destroys publication provenance. Deactivation is a flag, never a delete.
        ApiUser.objects.create(uuid='u-4', name='Departed', has_logged_in=True)
        self.run_sync([person('u-4', active=False)], '--since', '2026-08-01')
        api_user = ApiUser.objects.get(uuid='u-4')
        self.assertFalse(api_user.active)
        self.assertTrue(api_user.has_logged_in)

    def test_dry_run_writes_nothing_and_leaves_the_watermark(self):
        self.run_sync([person('u-5')], '--since', '2026-08-01', '--dry-run')
        self.assertFalse(ApiUser.objects.filter(uuid='u-5').exists())
        self.assertIsNone(TaskTimeoutTracker.objects.get(name='user_sync_check').value)

    def test_a_failed_window_does_not_advance_the_watermark(self):
        # "no data" and "no answer" must not look alike: a watermark advanced past a
        # window we never read hides those people until the next full backfill.
        from django.core.management.base import CommandError
        with mock.patch(
            'publicationtrkr.apps.apiuser.management.commands.sync_fabric_users'
            '.get_journey_tracker_people',
            side_effect=RuntimeError('502 Bad Gateway'),
        ):
            with self.assertRaises(CommandError):
                call_command('sync_fabric_users', '--since', '2026-08-01')
        self.assertIsNone(TaskTimeoutTracker.objects.get(name='user_sync_check').value)

    def test_successful_run_advances_the_watermark(self):
        self.run_sync([person('u-6')], '--since', '2026-08-01')
        self.assertIsNotNone(TaskTimeoutTracker.objects.get(name='user_sync_check').value)

    def test_the_anonymous_user_is_never_touched(self):
        anon_uuid = '00000000-0000-0000-0000-000000000000'
        ApiUser.objects.create(uuid=anon_uuid, name='Anonymous API User')
        self.run_sync([person(anon_uuid, name='Should Not Apply')],
                      '--since', '2026-08-01')
        self.assertEqual(ApiUser.objects.get(uuid=anon_uuid).name, 'Anonymous API User')


# Scholar/Scopus identifier pass (issue #32, v1.15.0)
#
# A second pass over a second endpoint under a second token. What matters most here is
# not that it works but how it *fails*: it must never take the nightly directory sync
# down with it, because the sync is critical and the identifiers are a weak prior.


class SyncFabricUsersMetricsPassTests(SyncFabricUsersTests):
    """Inherits setUp and run_sync from the sync tests above."""

    def test_identifiers_are_written_onto_the_directory_row(self):
        self.run_sync(
            [person('u-10')], '--since', '2026-08-01',
            metrics=[metrics_person('u-10', google_scholar='u3J2tc0AAAAJ')],
        )
        api_user = ApiUser.objects.get(uuid='u-10')
        self.assertEqual(api_user.google_scholar, 'u3J2tc0AAAAJ')
        self.assertEqual(api_user.scopus, '')

    def test_nulls_are_coerced_to_empty_strings(self):
        # Both columns are CharField(blank=True) and NOT NULL, and the endpoint returns
        # null far more often than not -- 3,303 of 3,311 people have neither identifier.
        self.run_sync([person('u-11')], '--since', '2026-08-01',
                      metrics=[metrics_person('u-11')])
        api_user = ApiUser.objects.get(uuid='u-11')
        self.assertEqual(api_user.google_scholar, '')
        self.assertEqual(api_user.scopus, '')

    def test_a_row_created_by_this_same_run_gets_its_identifiers(self):
        # The pass runs after the window loop precisely so this holds: a person seen for
        # the first time tonight should not have to wait for tomorrow's run.
        self.run_sync(
            [person('u-12')], '--since', '2026-08-01',
            metrics=[metrics_person('u-12', scopus='7005432109')],
        )
        self.assertEqual(ApiUser.objects.get(uuid='u-12').scopus, '7005432109')

    def test_the_pass_does_not_stamp_last_synced(self):
        # last_synced means "the journey-tracker sync touched this row". This endpoint
        # returns everybody every time, so stamping it here would set it to now for all
        # 3,300 rows on every run and flatten the only staleness signal there is.
        existing_stamp = datetime(2026, 1, 1, tzinfo=timezone.utc)
        ApiUser.objects.create(uuid='u-13', name='Someone', last_synced=existing_stamp)
        self.run_sync(
            [],  # no journey-tracker window touches this row
            '--since', '2026-08-01',
            metrics=[metrics_person('u-13', google_scholar='abc123')],
        )
        api_user = ApiUser.objects.get(uuid='u-13')
        self.assertEqual(api_user.google_scholar, 'abc123')
        self.assertEqual(api_user.last_synced, existing_stamp)

    def test_a_uuid_core_api_knows_but_the_directory_does_not_is_not_created(self):
        # This endpoint carries no name, email, affiliation or roles, so the row would be
        # a uuid with two identifiers on it and nothing to name-match against.
        # /journey-tracker/people is the only thing that creates ApiUser rows.
        self.run_sync([], '--since', '2026-08-01',
                      metrics=[metrics_person('never-seen', google_scholar='x')])
        self.assertFalse(ApiUser.objects.filter(uuid='never-seen').exists())

    def test_a_missing_services_token_warns_and_skips_rather_than_failing(self):
        # THE important one. Copying the readonly token's CommandError guard would turn a
        # working nightly directory sync into a hard failure on any host whose .env lagged
        # the restart -- the shape of the USR_* triple that bit the 1.12.0 deploy.
        with mock.patch(
            'publicationtrkr.apps.apiuser.management.commands.sync_fabric_users'
            '.get_journey_tracker_people',
            side_effect=lambda start_date, end_date, token: [person('u-14')],
        ), mock.patch(
            'publicationtrkr.apps.apiuser.management.commands.sync_fabric_users'
            '.get_core_api_metrics_people',
        ) as metrics_call, mock.patch.dict(
            os.environ, {'FABRIC_CORE_API_SERVICES_TOKEN': ''}
        ):
            out = StringIO()
            call_command('sync_fabric_users', '--since', '2026-08-01', stdout=out)

        metrics_call.assert_not_called()
        self.assertIn('FABRIC_CORE_API_SERVICES_TOKEN is not set', out.getvalue())
        # The directory sync completed regardless -- that is the whole point.
        self.assertTrue(ApiUser.objects.filter(uuid='u-14').exists())
        self.assertIsNotNone(TaskTimeoutTracker.objects.get(name='user_sync_check').value)

    def test_a_failed_metrics_fetch_does_not_fail_the_directory_sync(self):
        # Unlike a journey-tracker window -- where a silent gap hides people until someone
        # runs --full -- nothing is lost by not refreshing a weak prior tonight. The
        # columns keep yesterday's values and tomorrow's run picks them up.
        ApiUser.objects.create(uuid='u-15', name='Someone', google_scholar='kept')
        self.run_sync([person('u-15')], '--since', '2026-08-01',
                      metrics_error=RuntimeError('502 Bad Gateway'))
        self.assertEqual(ApiUser.objects.get(uuid='u-15').google_scholar, 'kept')
        self.assertIsNotNone(TaskTimeoutTracker.objects.get(name='user_sync_check').value)

    def test_a_dry_run_writes_no_identifiers(self):
        ApiUser.objects.create(uuid='u-16', name='Someone')
        self.run_sync([], '--since', '2026-08-01', '--dry-run',
                      metrics=[metrics_person('u-16', google_scholar='x')])
        self.assertEqual(ApiUser.objects.get(uuid='u-16').google_scholar, '')

    def test_identifiers_are_absent_from_as_dict(self):
        # as_dict() is injected into every rendered template context, so it is not the
        # place for a field nothing renders.
        keys = ApiUser(uuid='u-17', name='Someone').as_dict().keys()
        self.assertNotIn('google_scholar', keys)
        self.assertNotIn('scopus', keys)

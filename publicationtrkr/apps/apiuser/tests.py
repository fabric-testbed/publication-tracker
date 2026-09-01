"""
Tests for the FABRIC user sync (issue #23).

Covers the two things most likely to break silently: the role parser, which decides
what counts as project membership for every user in the system, and the sync's field
ownership rule, which is the difference between a resync refreshing a directory entry
and a resync logging someone out.
"""

from datetime import datetime, timedelta, timezone
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


class SyncFabricUsersTests(TestCase):

    def setUp(self):
        TaskTimeoutTracker.objects.create(
            description='User Sync Check', last_updated=datetime.now(timezone.utc),
            name='user_sync_check', timeout_in_seconds=86400,
            uuid='11111111-1111-1111-1111-111111111111', value=None,
        )

    def run_sync(self, people, *args):
        """Run the command with the core-api call stubbed to one window of `people`."""
        with mock.patch(
            'publicationtrkr.apps.apiuser.management.commands.sync_fabric_users'
            '.get_journey_tracker_people',
            side_effect=lambda start_date, end_date, token: people,
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

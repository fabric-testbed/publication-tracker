"""
Tests for scoring against membership history (#72).

The regression this exists for: a person who has left a project -- or whose project has
expired -- stops being evidence for the papers they wrote on it the moment the directory
sync applies the removal. With history, they stay evidence, labelled as a former member.

And the thing it must not do: change a single standing suggestion for a current member.
The signals dict is compared on every nightly run, so any drift in a current member's
detail string would rewrite the whole queue.
"""

from datetime import datetime, timezone
from io import StringIO

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, TestCase

from publicationtrkr.apps.apiuser.models import ApiUser
from publicationtrkr.apps.apiuser.utils.memberships import load_last_seen, record_memberships
from publicationtrkr.apps.publications.models import Author, AuthorClaim, Publication
from publicationtrkr.apps.publications.utils.claim_scoring import (
    FORMER_MEMBER_VALUE, MIN_SCORE, WEIGHT_NAME, WEIGHT_PROJECT, MembershipHistory,
    project_signal, score_pair,
)

LAST_SEEN = datetime(2026, 9, 1, 3, 0, tzinfo=timezone.utc)


class _User:
    def __init__(self, name, projects, uuid='u-1'):
        self.name = name
        self.projects = projects
        self.uuid = uuid
        self.google_scholar = ''
        self.scopus = ''


class _Author:
    def __init__(self, author_name):
        self.author_name = author_name


class _Publication:
    def __init__(self, project_uuid, uuid='pub-1'):
        self.project_uuid = project_uuid
        self.uuid = uuid


class ProjectSignalHistoryTests(SimpleTestCase):

    def history(self, **kwargs):
        return MembershipHistory({'u-1': {'p-1': LAST_SEEN}}, **kwargs)

    def test_a_current_member_scores_exactly_as_before(self):
        # Byte-identical to the v1.21.1 signal, history or not: no standing suggestion
        # for a current member may compare unequal on the first run after deploy.
        user = _User('Jane Smith', ['p-1'])
        self.assertEqual(project_signal(_Publication('p-1'), user, self.history()),
                         (1.0, 'member of the publication project p-1'))
        self.assertEqual(project_signal(_Publication('p-1'), user, self.history()),
                         project_signal(_Publication('p-1'), user))

    def test_a_former_member_keeps_the_signal_labelled_as_former(self):
        value, detail = project_signal(_Publication('p-1'), _User('Jane Smith', []), self.history())
        self.assertEqual(value, FORMER_MEMBER_VALUE)
        self.assertEqual(detail, 'former member of the publication project p-1 (last seen 2026-09-01)')

    def test_without_history_a_former_member_is_no_signal(self):
        self.assertEqual(project_signal(_Publication('p-1'), _User('Jane Smith', [])),
                         (0.0, 'not a member of the publication project'))

    def test_history_on_another_project_or_person_is_no_signal(self):
        history = self.history()
        self.assertEqual(project_signal(_Publication('p-2'), _User('Jane Smith', []), history)[0], 0.0)
        self.assertEqual(
            project_signal(_Publication('p-1'), _User('Jane Smith', [], uuid='u-2'), history)[0], 0.0)

    def test_the_former_value_is_a_fraction_of_the_project_weight(self):
        result = score_pair(_Author('Smith, Jane'), _User('Jane Smith', []), _Publication('p-1'),
                            history=self.history(former_value=0.5))
        self.assertEqual(result['signals']['project']['value'], 0.5)
        self.assertAlmostEqual(result['score'], 0.5 * WEIGHT_PROJECT + WEIGHT_NAME)

    def test_a_publication_with_no_project_ignores_history(self):
        self.assertEqual(project_signal(None, _User('Jane Smith', []), self.history()),
                         (0.0, 'publication has no project'))


class ScoreAuthorClaimsHistoryTests(TestCase):
    """
    The acceptance regression from #72, end to end: `Smith` alone is too weak a name to
    clear MIN_SCORE, and the project was carrying it. The person has since left.
    """

    def setUp(self):
        Publication.objects.create(authors=['Smith'], project_uuid='p-1', title='A paper',
                                   uuid='pub-1', year='2026')
        self.author = Author.objects.create(author_name='Smith', display_name='Smith',
                                            publication_uuid='pub-1', uuid='auth-1')
        self.user = ApiUser.objects.create(uuid='user-1', name='Jane Smith', projects=[],
                                           active=True)

    def run_scoring(self, *args):
        out = StringIO()
        call_command('score_author_claims', *args, stdout=out)
        return out.getvalue()

    def test_the_name_alone_does_not_qualify(self):
        # The premise of the test below, checked rather than assumed.
        self.assertLess(score_pair(self.author, self.user, None)['score'], MIN_SCORE)
        self.run_scoring()
        self.assertFalse(AuthorClaim.objects.exists())

    def test_a_member_who_left_still_produces_the_suggestion(self):
        record_memberships(self.user.id, ['p-1'], LAST_SEEN, 'seed')
        output = self.run_scoring()
        claim = AuthorClaim.objects.get(author=self.author, api_user=self.user)
        self.assertEqual(claim.status, AuthorClaim.SUGGESTED)
        self.assertTrue(claim.signals['project']['detail'].startswith('former member'))
        self.assertIn('Memberships    : 1 former membership(s) among candidates', output)
        self.assertIn("--- Former-member suggestions (1) ---", output)
        self.assertIn("'Smith' -> Jane Smith (user-1)", output)

    def test_a_trial_weight_is_measured_on_a_dry_run_only(self):
        record_memberships(self.user.id, ['p-1'], LAST_SEEN, 'seed')
        output = self.run_scoring('--dry-run', '--former-member-value', '0')
        self.assertIn('Suggestions created: 0', output)
        self.assertIn('scored at 0.0 of the project weight', output)
        with self.assertRaisesRegex(CommandError, 'needs --dry-run'):
            self.run_scoring('--former-member-value', '0.5')
        with self.assertRaisesRegex(CommandError, 'between 0 and 1'):
            self.run_scoring('--dry-run', '--former-member-value', '2')
        self.assertFalse(AuthorClaim.objects.exists())

    def test_history_is_loaded_by_uuid(self):
        record_memberships(self.user.id, ['p-1', 'p-2'], LAST_SEEN, 'sync')
        self.assertEqual(load_last_seen(), {'user-1': {'p-1': LAST_SEEN, 'p-2': LAST_SEEN}})

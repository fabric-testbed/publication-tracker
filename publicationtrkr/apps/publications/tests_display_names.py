"""Credited authors' display names follow their FABRIC account names (#73)."""

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from io import StringIO
from threading import Event, get_ident
from unittest import mock
from uuid import uuid4

from django.core.management import call_command
from django.db import close_old_connections, connection
from django.db.migrations.executor import MigrationExecutor
from django.db.models.signals import post_save
from django.test import RequestFactory, SimpleTestCase, TestCase, TransactionTestCase, skipUnlessDBFeature
from rest_framework.test import APIClient

from publicationtrkr.apps.apiuser.models import ApiUser, TaskTimeoutTracker
from publicationtrkr.apps.publications.models import Author, AuthorClaim
from publicationtrkr.apps.publications.utils.author_mutations import mutate_author
from publicationtrkr.apps.publications.utils.claim_ledger import approve_suggestion
from publicationtrkr.apps.publications.utils.display_names import follow_account_name_change
from publicationtrkr.apps.publications.utils.publication_builder import create_publication, update_publication
from publicationtrkr.utils.fabric_auth import get_api_user, save_refreshed_user
from publicationtrkr.utils.names import check_account_name, usable_account_name

AUTHOR_RESPONSE_FIELDS = {
    'author_name', 'author_order', 'display_name', 'fabric_uuid', 'publication_uuid', 'uuid',
}
API_AUTH = 'publicationtrkr.apps.publications.api.viewsets.get_api_user'
VIEW_AUTH = 'publicationtrkr.apps.publications.views.get_api_user'
SYNC = 'publicationtrkr.apps.apiuser.management.commands.sync_fabric_users.'


class UsableAccountNameTests(SimpleTestCase):
    """Every shape measured across the 3,346 production account names on 2026-09-21."""

    def test_inverted_names_are_uninverted(self):
        for raw, expected in (
            ('Mahmud, Imtiaz', 'Imtiaz Mahmud'),
            ('Jamil, Md Hasibul', 'Md Hasibul Jamil'),
            ('Patel, Ankita G.', 'Ankita G. Patel'),
            ('Santana Carmona, Lesther A', 'Lesther A Santana Carmona'),
            ('van der Berg, Piet', 'Piet van der Berg'),
        ):
            with self.subTest(raw=raw):
                self.assertEqual(usable_account_name(raw), expected)

    def test_a_generational_suffix_is_not_an_inversion(self):
        # splitname would read "John Smith, Jr." as surname "John Smith", given "Jr.".
        for raw in ('John Smith, Jr.', 'John Q. Smith, Sr', 'John Smith, III'):
            with self.subTest(raw=raw):
                self.assertEqual(usable_account_name(raw), raw)

    def test_names_without_a_comma_are_used_exactly_as_written(self):
        # Never title-cased, never "repaired": particles and Mc/O' stay as the person typed.
        for raw in ('Cees de Laat', "Mary O'Neil", 'Ian McDonald', 'Md Hasibul Jamil',
                    'Maria dos Santos', "Jean d'Alembert", 'JQ Smith', 'John Smith III',
                    'Ana Esquivel-Morel'):
            with self.subTest(raw=raw):
                self.assertEqual(usable_account_name(raw), raw)

    def test_non_ascii_names_are_kept_exactly(self):
        for raw in ('José García-López', 'Łukasz Żółć',
                    'Nguyễn Văn An'):
            with self.subTest(raw=raw):
                self.assertEqual(usable_account_name(raw), raw)

    def test_whitespace_is_collapsed(self):
        self.assertEqual(usable_account_name('  Jane   Doe\n'), 'Jane Doe')

    def test_unusable_names_are_skipped_with_a_reason(self):
        for raw, reason in (
            ('Rick', 'single name'),
            ('Mahmud,', 'single name'),
            ('yulong xiao', 'all lowercase'),
            ('YULONG XIAO', 'all capitals'),
            ('', 'empty'),
            (None, 'empty'),
            ('Doe Roe, Jane, Ph.D.', 'several commas'),
            ('Doe, Jane (Example-Student)', 'parenthetical note'),
            ('Jane DOE', 'a word in capitals'),
            ('Jane SMITH-JONES', 'a word in capitals'),
            ('jane Doe', 'a word in lowercase'),
            ('Jane marie Doe', 'a word in lowercase'),
        ):
            with self.subTest(raw=raw):
                self.assertEqual(check_account_name(raw), (None, reason))
                self.assertIsNone(usable_account_name(raw))


class DisplayNameSourceMigrationTests(TransactionTestCase):
    def test_existing_rows_become_byline_and_keep_their_names(self):
        executor = MigrationExecutor(connection)
        before = [('publications', '0007_author_uuid_unique')]
        executor.migrate(before)
        OldAuthor = executor.loader.project_state(before).apps.get_model('publications', 'Author')
        OldAuthor.objects.create(author_name='Smith, J.', display_name='Smith, J.',
                                 publication_uuid='p', uuid='a-1', fabric_uuid='someone')
        OldAuthor.objects.create(author_name='Jane Roe', display_name='J. Roe',
                                 publication_uuid='p', uuid='a-2')
        executor = MigrationExecutor(connection)
        executor.migrate([('publications', '0008_author_display_name_source')])
        self.assertEqual(
            list(Author.objects.order_by('uuid').values_list('uuid', 'display_name', 'display_name_source')),
            [('a-1', 'Smith, J.', Author.BYLINE), ('a-2', 'J. Roe', Author.BYLINE)],
        )


class Fixtures:
    def make_user(self, name, *, admin=False):
        return ApiUser.objects.create(
            uuid=str(uuid4()), name=name,
            fabric_roles=['publication-tracker-admins'] if admin else ['Jupyterhub'],
        )

    def make_author(self, byline='J. Smith', *others):
        publication = create_publication(
            api_user=self.admin, data={'authors': [byline, *others], 'title': str(uuid4()), 'year': '2026'})
        return Author.objects.get(uuid=publication.authors[0])

    def credit(self, author, person):
        return mutate_author(actor=self.admin, author=author, data={'fabric_uuid': person.uuid})

    def assert_shows(self, author, display_name, source, *, byline=None):
        author.refresh_from_db()
        self.assertEqual((author.display_name, author.display_name_source), (display_name, source))
        if byline is not None:
            self.assertEqual(author.author_name, byline)


class CreditingTests(Fixtures, TestCase):
    def setUp(self):
        self.admin = self.make_user('Admin Person', admin=True)
        self.person = self.make_user('Jane Q. Smith')

    def test_a_self_claim_shows_the_account_name_and_keeps_the_byline(self):
        author = self.make_author()
        mutate_author(actor=self.person, author=author, data={}, self_claim=True)
        self.assert_shows(author, 'Jane Q. Smith', Author.ACCOUNT, byline='J. Smith')
        self.assertEqual(author.fabric_uuid, self.person.uuid)

    def test_admin_attribution_follows_the_account_name(self):
        author = self.make_author()
        self.credit(author, self.person)
        self.assert_shows(author, 'Jane Q. Smith', Author.ACCOUNT, byline='J. Smith')

    def test_approving_a_suggestion_follows_the_account_name(self):
        author = self.make_author()
        claim = AuthorClaim.objects.create(author=author, api_user=self.person, score=0.5, uuid=str(uuid4()))
        approve_suggestion(claim, decided_by=self.admin)
        self.assert_shows(author, 'Jane Q. Smith', Author.ACCOUNT, byline='J. Smith')

    def test_an_inverted_account_name_is_shown_uninverted(self):
        person = self.make_user('Mahmud, Imtiaz')
        author = self.make_author('I. Mahmud')
        self.credit(author, person)
        self.assert_shows(author, 'Imtiaz Mahmud', Author.ACCOUNT, byline='I. Mahmud')

    def test_an_unusable_account_name_leaves_the_byline(self):
        for name in ('yulong xiao', 'YULONG XIAO', 'Rick', 'Doe, Jane (Example-Student)'):
            with self.subTest(name=name):
                author = self.make_author('Yulong Xiao')
                self.credit(author, self.make_user(name))
                self.assert_shows(author, 'Yulong Xiao', Author.BYLINE)

    def test_a_custom_name_survives_being_credited(self):
        author = self.make_author()
        mutate_author(actor=self.admin, author=author, data={'display_name': 'Dr. J. Smith'})
        self.assert_shows(author, 'Dr. J. Smith', Author.CUSTOM)
        mutate_author(actor=self.person, author=author, data={}, self_claim=True)
        self.assert_shows(author, 'Dr. J. Smith', Author.CUSTOM)

    def test_moving_the_credit_never_keeps_the_previous_persons_name(self):
        author = self.make_author()
        self.credit(author, self.person)
        reassign = {'correction_reason': 'Wrong person'}
        mutate_author(actor=self.admin, author=author,
                      data={**reassign, 'fabric_uuid': self.make_user('rick').uuid})
        self.assert_shows(author, 'J. Smith', Author.BYLINE)
        mutate_author(actor=self.admin, author=author,
                      data={**reassign, 'fabric_uuid': self.make_user('John Smith').uuid})
        self.assert_shows(author, 'John Smith', Author.ACCOUNT)

    def test_removing_the_credit_restores_the_byline_even_over_a_custom_name(self):
        author = self.make_author()
        mutate_author(actor=self.person, author=author, data={'display_name': 'Jane Smith, PhD'}, self_claim=True)
        self.assert_shows(author, 'Jane Smith, PhD', Author.CUSTOM)
        mutate_author(actor=self.admin, author=author,
                      data={'fabric_uuid': None, 'correction_reason': 'Not this person'})
        self.assert_shows(author, 'J. Smith', Author.BYLINE)
        self.assertIsNone(author.fabric_uuid)

    def test_a_byline_rename_carries_only_a_byline_copy_along(self):
        uncredited = self.make_author('Jane Smyth')
        mutate_author(actor=self.admin, author=uncredited,
                      data={'author_name': 'Jane Smith', 'fabric_uuid': None})
        self.assert_shows(uncredited, 'Jane Smith', Author.BYLINE)

        person = self.make_user('Jane Smith')
        author = self.make_author('Jane Smith', 'Bob Two')
        self.credit(author, person)
        self.assert_shows(author, 'Jane Smith', Author.ACCOUNT)
        publication_uuid = author.publication_uuid
        from publicationtrkr.apps.publications.models import Publication
        publication = Publication.objects.get(uuid=publication_uuid)
        slots = list(publication.authors)
        update_publication(publication=publication, data={'authors': ['J. Smith', 'Bob Two']},
                           api_user=self.admin, author_slots=slots,
                           author_correction_reason='Byline as printed')
        self.assert_shows(author, 'Jane Smith', Author.ACCOUNT, byline='J. Smith')


class ExplicitEditApiTests(Fixtures, TestCase):
    def setUp(self):
        self.admin = self.make_user('Admin Person', admin=True)
        self.person = self.make_user('Jane Q. Smith')
        self.author = self.make_author()
        self.credit(self.author, self.person)
        self.url = '/api/authors/' + self.author.uuid
        self.client = APIClient()

    def send(self, method, data):
        with mock.patch(API_AUTH, return_value=self.admin):
            return getattr(self.client, method)(self.url, data=data, format='json')

    def test_a_different_name_is_custom_and_the_account_name_is_account(self):
        response = self.send('patch', {'display_name': 'Jane Smith'})
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(set(response.data), AUTHOR_RESPONSE_FIELDS)
        self.assert_shows(self.author, 'Jane Smith', Author.CUSTOM)
        self.send('patch', {'display_name': 'Jane Q. Smith'})
        self.assert_shows(self.author, 'Jane Q. Smith', Author.ACCOUNT)

    def test_echoing_a_read_back_pins_nothing(self):
        with mock.patch(API_AUTH, return_value=self.admin):
            current = self.client.get(self.url).data
        response = self.send('put', dict(current))
        self.assertEqual(response.status_code, 200, response.data)
        self.assert_shows(self.author, 'Jane Q. Smith', Author.ACCOUNT)

    def test_use_account_name_switches_between_custom_and_account(self):
        self.send('patch', {'display_name': 'Jane Smith'})
        response = self.send('patch', {'use_account_name': True})
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(set(response.data), AUTHOR_RESPONSE_FIELDS)
        self.assert_shows(self.author, 'Jane Q. Smith', Author.ACCOUNT)
        self.send('patch', {'use_account_name': False, 'display_name': 'Jane Q. Smith'})
        self.assert_shows(self.author, 'Jane Q. Smith', Author.CUSTOM)

    def test_a_new_name_and_use_account_name_together_are_refused(self):
        response = self.send('patch', {'display_name': 'Jane Smith', 'use_account_name': True})
        self.assertEqual(response.status_code, 400)
        self.assertIn('use_account_name', response.data)
        self.assert_shows(self.author, 'Jane Q. Smith', Author.ACCOUNT)

    def test_public_reads_do_not_expose_the_source(self):
        for url in (self.url, '/api/authors', '/api/publications/' + self.author.publication_uuid):
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200)
                if 'results' in response.data:
                    rows = response.data['results']
                elif 'authors' in response.data:
                    rows = response.data['authors']
                else:
                    rows = [response.data]
                for row in rows:
                    self.assertEqual(set(row), AUTHOR_RESPONSE_FIELDS)


class AuthorFormTests(Fixtures, TestCase):
    def setUp(self):
        self.admin = self.make_user('Admin Person', admin=True)
        self.person = self.make_user('Jane Q. Smith')
        self.author = self.make_author()
        self.url = '/publications/authors/{0}/update'.format(self.author.uuid)

    def get(self, user):
        with mock.patch(VIEW_AUTH, return_value=user):
            return self.client.get(self.url)

    def post(self, user, data):
        with mock.patch(VIEW_AUTH, return_value=user):
            return self.client.post(self.url, {'save': 'save', **data})

    def test_the_claim_form_offers_the_account_name_checked(self):
        form = self.get(self.person).context['form']
        self.assertTrue(form.initial['use_account_name'])
        self.assertIn('Jane Q. Smith', form.fields['use_account_name'].label)

    def test_claiming_with_the_box_checked_shows_the_account_name(self):
        self.post(self.person, {'display_name': 'J. Smith', 'use_account_name': 'on'})
        self.assert_shows(self.author, 'Jane Q. Smith', Author.ACCOUNT)

    def test_unchecking_the_box_keeps_the_name_shown(self):
        self.post(self.person, {'display_name': 'J. Smith'})
        self.assert_shows(self.author, 'J. Smith', Author.CUSTOM)
        self.assertEqual(self.author.fabric_uuid, self.person.uuid)

    def test_a_typed_name_wins_over_the_box(self):
        self.post(self.person, {'display_name': 'Jane Smith', 'use_account_name': 'on'})
        self.assert_shows(self.author, 'Jane Smith', Author.CUSTOM)

    def test_checking_the_box_on_a_custom_name_follows_the_account_again(self):
        self.post(self.person, {'display_name': 'Jane Smith'})
        self.assertFalse(self.get(self.person).context['form'].initial['use_account_name'])
        self.post(self.person, {'display_name': 'Jane Smith', 'use_account_name': 'on'})
        self.assert_shows(self.author, 'Jane Q. Smith', Author.ACCOUNT)

    def test_an_unusable_account_name_is_explained_not_offered(self):
        person = self.make_user('yulong xiao')
        response = self.get(person)
        self.assertNotIn('use_account_name', response.context['form'].fields)
        self.assertContains(response, 'all lowercase')
        self.post(person, {'display_name': 'J. Smith'})
        self.assert_shows(self.author, 'J. Smith', Author.BYLINE)
        self.assertEqual(self.author.fabric_uuid, person.uuid)

    def test_admin_clearing_the_credit_restores_the_byline(self):
        self.credit(self.author, self.person)
        form = self.get(self.admin).context['form']
        self.assertTrue(form.initial['use_account_name'])
        self.post(self.admin, {
            'display_name': 'Jane Q. Smith', 'use_account_name': 'on', 'author_name': 'J. Smith',
            'publication_uuid': self.author.publication_uuid, 'fabric_uuid': '',
            'correction_reason': 'Not this person',
        })
        self.assert_shows(self.author, 'J. Smith', Author.BYLINE)
        self.assertIsNone(self.author.fabric_uuid)


class AccountNameChangeTests(Fixtures, TestCase):
    def setUp(self):
        self.admin = self.make_user('Admin Person', admin=True)
        self.person = self.make_user('Jane Q. Smith')
        self.following = self.make_author('J. Smith')
        self.credit(self.following, self.person)
        self.custom = self.make_author('Jane Smith')
        self.credit(self.custom, self.person)
        mutate_author(actor=self.admin, author=self.custom, data={'display_name': 'Dr. Jane Smith'})
        self.byline = self.make_author('J. Q. Smith')
        mutate_author(actor=self.admin, author=self.byline,
                      data={'fabric_uuid': self.person.uuid, 'use_account_name': False, 'display_name': 'J. Q. Smith'})
        self.assert_shows(self.byline, 'J. Q. Smith', Author.CUSTOM)
        Author.objects.filter(pk=self.byline.pk).update(display_name_source=Author.BYLINE)
        self.names_before = self.author_names()

    def author_names(self):
        return list(Author.objects.order_by('pk').values_list('uuid', 'author_name', 'fabric_uuid'))

    def assert_only_following_row_changed(self, name):
        self.assert_shows(self.following, name, Author.ACCOUNT)
        self.assert_shows(self.custom, 'Dr. Jane Smith', Author.CUSTOM)
        self.assert_shows(self.byline, 'J. Q. Smith', Author.BYLINE)
        self.assertEqual(self.author_names(), self.names_before)

    def run_sync(self, name, *args):
        TaskTimeoutTracker.objects.get_or_create(
            name='user_sync_check', defaults={
                'description': 'User Sync Check', 'last_updated': datetime.now(timezone.utc),
                'timeout_in_seconds': 86400, 'uuid': str(uuid4()),
            })
        person = {
            'active': True, 'affiliation': '', 'email_address': '', 'fabric_uuid': self.person.uuid,
            'fabric_roles': ['Jupyterhub'], 'name': name,
        }
        output = StringIO()
        with mock.patch(SYNC + 'get_journey_tracker_people', return_value=[person]), \
                mock.patch(SYNC + 'get_core_api_metrics_people', return_value=[]), \
                mock.patch.dict(os.environ, {'FABRIC_CORE_API_SERVICES_TOKEN': 'test-services-token'}):
            call_command('sync_fabric_users', '--since', '2026-01-01', *args, stdout=output)
        return output.getvalue()

    def test_the_directory_sync_carries_a_new_name_to_following_rows_only(self):
        output = self.run_sync('Jane Quinn Smith')
        self.assert_only_following_row_changed('Jane Quinn Smith')
        self.assertIn('Author names : 1 credited row(s) followed a changed account name; 0 kept', output)

    def test_a_dry_run_sync_reports_without_writing(self):
        output = self.run_sync('Jane Quinn Smith', '--dry-run')
        self.assert_only_following_row_changed('Jane Q. Smith')
        self.assertIn('1 credited row(s) would follow', output)
        self.assertEqual(ApiUser.objects.get(pk=self.person.pk).name, 'Jane Q. Smith')

    def test_an_unusable_new_name_leaves_rows_as_they_are(self):
        output = self.run_sync('jane smith')
        self.assert_only_following_row_changed('Jane Q. Smith')
        self.assertIn('0 credited row(s) followed a changed account name; 1 kept', output)
        self.assertEqual(ApiUser.objects.get(pk=self.person.pk).name, 'jane smith')

    def test_an_inverted_new_name_arrives_uninverted(self):
        self.run_sync('Smith, Jane Quinn')
        self.assert_only_following_row_changed('Jane Quinn Smith')

    def test_a_login_refresh_carries_a_changed_name(self):
        self.person.name = 'Jane Quinn Smith'
        save_refreshed_user(self.person)
        self.assert_only_following_row_changed('Jane Quinn Smith')

    def test_a_login_refresh_with_the_same_name_touches_no_author(self):
        Author.objects.filter(pk=self.following.pk).update(display_name='Stale Name')
        save_refreshed_user(ApiUser.objects.get(pk=self.person.pk))
        self.assert_shows(self.following, 'Stale Name', Author.ACCOUNT)

    def test_get_api_user_refresh_path_carries_a_changed_name(self):
        env = {'API_USER_ANON_UUID': 'anonymous', 'VOUCH_COOKIE_NAME': 'vouch',
               'API_USER_REFRESH_CHECK_MINUTES': '5'}
        ApiUser.objects.filter(pk=self.person.pk).update(cilogon_id='subject')
        refreshed = ApiUser.objects.get(pk=self.person.pk)
        refreshed.name = 'Jane Quinn Smith'
        request = RequestFactory().get('/')
        request.COOKIES['vouch'] = 'cookie'
        with mock.patch.dict(os.environ, env), \
                mock.patch('publicationtrkr.utils.fabric_auth.get_oidc_sub_from_cookie', return_value='subject'), \
                mock.patch('publicationtrkr.utils.fabric_auth.auth_user_by_cookie', return_value=refreshed):
            self.assertEqual(get_api_user(request).uuid, self.person.uuid)
        self.assert_only_following_row_changed('Jane Quinn Smith')

    def test_follow_account_name_change_counts_without_applying(self):
        self.assertEqual(follow_account_name_change(self.person.uuid, 'Jane Quinn Smith', apply=False), (1, 0))
        self.assertEqual(follow_account_name_change(self.person.uuid, 'rick', apply=False), (0, 1))
        self.assert_only_following_row_changed('Jane Q. Smith')


class SyncAuthorDisplayNamesCommandTests(Fixtures, TestCase):
    def setUp(self):
        self.admin = self.make_user('Admin Person', admin=True)
        rows = {}
        for key, byline, account in (
            ('identical', 'Paul Ruth', 'Paul Ruth'),
            ('drops', 'Mert Can Cevik', 'Mert Cevik'),
            ('adds', 'Jane Smith', 'Jane Q. Smith'),
            ('initial', 'K. Thareja', 'Komal Thareja'),
            ('inverted', 'Imtiaz Mahmud', 'Mahmud, Imtiaz'),
            ('lowercase', 'Yulong Xiao', 'yulong xiao'),
            ('custom', 'Ann Lee', 'Ann B. Lee'),
            ('edited', 'Bo Chen', 'Bo X. Chen'),
        ):
            rows[key] = self.make_author(byline)
            Author.objects.filter(pk=rows[key].pk).update(fabric_uuid=self.make_user(account).uuid)
        Author.objects.filter(pk=rows['custom'].pk).update(display_name='Dr. Ann Lee', display_name_source=Author.CUSTOM)
        Author.objects.filter(pk=rows['edited'].pk).update(display_name='B. Chen')
        rows['unknown'] = self.make_author('No Account')
        Author.objects.filter(pk=rows['unknown'].pk).update(fabric_uuid='not-in-the-directory')
        rows['uncredited'] = self.make_author('Some One')
        self.rows = rows

    def snapshot(self):
        return list(Author.objects.order_by('pk').values())

    def run_command(self, *args):
        output = StringIO()
        call_command('sync_author_display_names', *args, stdout=output)
        return output.getvalue()

    def test_the_default_is_a_categorized_preview_that_writes_nothing(self):
        before = self.snapshot()
        output = self.run_command()
        self.assertEqual(self.snapshot(), before)
        self.assertIn('DRY RUN', output)
        for line in (
            "'Mert Can Cevik' -> 'Mert Cevik'", "'Jane Smith' -> 'Jane Q. Smith'",
            "'K. Thareja' -> 'Komal Thareja'",
            'Credited rows : 9 (9 people)',
            'Identical     : 2 (0 already following; 2 to be marked as following)',
            'Would change  : 3',
            '  drops a name part         : 1', '  adds a name part          : 1',
            '  initial -> full name      : 1',
            'Skipped       : 4',
            '  account name unusable: all lowercase: 1', '  custom name               : 1',
            '  display_name edited before provenance was recorded: 1',
            '  no FABRIC account with this uuid: 1',
            'Un-inverted   : 1 row(s) from "Surname, Given" (1 identical once un-inverted, 0 changing)',
        ):
            with self.subTest(line=line):
                self.assertIn(line, output)

    def test_apply_updates_only_credited_rows_with_usable_names(self):
        before = {row['uuid']: row for row in self.snapshot()}
        self.run_command('--apply')
        expected = {
            'identical': ('Paul Ruth', Author.ACCOUNT), 'drops': ('Mert Cevik', Author.ACCOUNT),
            'adds': ('Jane Q. Smith', Author.ACCOUNT), 'initial': ('Komal Thareja', Author.ACCOUNT),
            'inverted': ('Imtiaz Mahmud', Author.ACCOUNT), 'lowercase': ('Yulong Xiao', Author.BYLINE),
            'custom': ('Dr. Ann Lee', Author.CUSTOM), 'edited': ('B. Chen', Author.BYLINE),
            'unknown': ('No Account', Author.BYLINE), 'uncredited': ('Some One', Author.BYLINE),
        }
        for key, (display_name, source) in expected.items():
            with self.subTest(row=key):
                self.assert_shows(self.rows[key], display_name, source)
        for row in self.snapshot():
            for field in ('author_name', 'fabric_uuid', 'publication_uuid', 'author_order', 'uuid'):
                self.assertEqual(row[field], before[row['uuid']][field])
        output = self.run_command()
        self.assertIn('Would change  : 0', output)
        self.assertIn('Identical     : 5 (5 already following; 0 to be marked as following)', output)

    def test_rows_marked_identical_follow_a_later_account_name_change(self):
        self.run_command('--apply')
        person = ApiUser.objects.get(uuid=Author.objects.get(pk=self.rows['identical'].pk).fabric_uuid)
        person.name = 'Paul A. Ruth'
        save_refreshed_user(person)
        self.assert_shows(self.rows['identical'], 'Paul A. Ruth', Author.ACCOUNT, byline='Paul Ruth')


@skipUnlessDBFeature('has_select_for_update')
class CreditAndNameChangeRaceTests(Fixtures, TransactionTestCase):
    def test_a_name_change_committing_mid_credit_still_reaches_the_new_credit(self):
        """
        The credit reads the old name; the name change commits before the credit does, and
        cannot see the row as credited yet. The post-commit recheck is what repairs it.
        """
        self.admin = self.make_user('Admin Person', admin=True)
        person = self.make_user('Jane Q. Smith')
        author_pk = self.make_author('J. Smith').pk
        credit_paused, name_changed = Event(), Event()
        credit_thread = []

        def pause_after_credit_write(sender, instance, update_fields=None, **kwargs):
            if get_ident() in credit_thread and update_fields and 'fabric_uuid' in update_fields:
                credit_paused.set()
                name_changed.wait(timeout=10)

        def credit():
            credit_thread.append(get_ident())
            close_old_connections()
            try:
                mutate_author(actor=ApiUser.objects.get(pk=person.pk),
                              author=Author.objects.get(pk=author_pk), data={}, self_claim=True)
            finally:
                close_old_connections()

        def change_name():
            close_old_connections()
            try:
                credit_paused.wait(timeout=10)
                changed = ApiUser.objects.get(pk=person.pk)
                changed.name = 'Jane Quinn Smith'
                save_refreshed_user(changed)
            finally:
                name_changed.set()
                close_old_connections()

        post_save.connect(pause_after_credit_write, sender=Author)
        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(credit), pool.submit(change_name)]
                for future in futures:
                    future.result(timeout=20)
        finally:
            post_save.disconnect(pause_after_credit_write, sender=Author)
        author = Author.objects.get(pk=author_pk)
        self.assertEqual(author.fabric_uuid, person.uuid)
        self.assertEqual((author.display_name, author.display_name_source), ('Jane Quinn Smith', Author.ACCOUNT))


class PreexistingCreditTests(Fixtures, TestCase):
    """Rows credited before #73 change only through a credit or the reviewed command."""

    def setUp(self):
        self.admin = self.make_user('Admin Person', admin=True)
        self.person = self.make_user('Jane Q. Smith')
        self.author = self.make_author('J. Smith', 'Bob Two')
        Author.objects.filter(pk=self.author.pk).update(fabric_uuid=self.person.uuid)

    def test_an_admin_edit_of_another_field_leaves_the_name_alone(self):
        url = '/publications/authors/{0}/update'.format(self.author.uuid)
        with mock.patch(VIEW_AUTH, return_value=self.admin):
            form = self.client.get(url).context['form']
            self.assertFalse(form.initial['use_account_name'])
            with self.captureOnCommitCallbacks(execute=True):
                self.client.post(url, {
                    'save': 'save', 'display_name': 'J. Smith', 'author_name': 'J. Smith',
                    'publication_uuid': self.author.publication_uuid, 'fabric_uuid': self.person.uuid,
                })
        self.assert_shows(self.author, 'J. Smith', Author.BYLINE)
        mutate_author(actor=self.admin, author=self.author, data={'author_order': 1})
        self.assert_shows(self.author, 'J. Smith', Author.BYLINE)

    def test_the_owner_reclaiming_it_is_a_credit_and_follows(self):
        mutate_author(actor=self.person, author=self.author, data={}, self_claim=True)
        self.assert_shows(self.author, 'Jane Q. Smith', Author.ACCOUNT)

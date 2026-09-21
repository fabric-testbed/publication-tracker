"""Database regressions for attribution-preserving publication and author writes."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest import mock
from uuid import uuid4

from django.db import IntegrityError, close_old_connections
from django.db.models.signals import pre_save
from django.test import TestCase, TransactionTestCase, skipUnlessDBFeature
from rest_framework.exceptions import ValidationError
from rest_framework.test import APIClient

from publicationtrkr.apps.apiuser.models import ApiUser
from publicationtrkr.apps.publications.models import (
    Author, AuthorClaim, AuthorCorrection, Publication,
)
from publicationtrkr.apps.publications.utils.author_mutations import mutate_author
from publicationtrkr.apps.publications.utils.claim_ledger import (
    ClaimDecisionError, approve_suggestion, reject_suggestion,
)
from publicationtrkr.apps.publications.utils.publication_builder import (
    create_publication, update_publication,
)


AUTHOR_RESPONSE_FIELDS = {
    'author_name', 'author_order', 'display_name', 'fabric_uuid',
    'publication_uuid', 'uuid',
}
AUTH_LOOKUP = 'publicationtrkr.apps.publications.api.viewsets.get_api_user'


class IntegrityFixtures:
    def make_user(self, *, admin=False):
        return ApiUser.objects.create(
            uuid=str(uuid4()), name='Test Person',
            fabric_roles=['publication-tracker-admins'] if admin else ['Jupyterhub'],
        )

    def make_publication(self, names=('Alice One', 'Bob Two')):
        return create_publication(
            api_user=self.admin,
            data={'authors': list(names), 'title': str(uuid4()), 'year': '2026'},
        )

    def claim(self, author, user=None):
        return mutate_author(
            actor=self.admin, author=author,
            data={'fabric_uuid': (user or self.person).uuid},
        )

    def assert_membership(self, publication, authors):
        publication.refresh_from_db()
        self.assertEqual(publication.authors, [author.uuid for author in authors])
        for position, author in enumerate(authors):
            author.refresh_from_db()
            self.assertEqual(author.publication_uuid, publication.uuid)
            self.assertEqual(author.author_order, position)


class AuthorApiIntegrityTests(IntegrityFixtures, TestCase):
    def setUp(self):
        self.admin = self.make_user(admin=True)
        self.person = self.make_user()
        self.publication = self.make_publication()
        self.author = Author.objects.get(uuid=self.publication.authors[0])
        self.other = Author.objects.get(uuid=self.publication.authors[1])
        self.client = APIClient()
        self.url = '/api/authors/' + self.author.uuid

    def as_admin(self, method, url, data=None):
        with mock.patch(AUTH_LOOKUP, return_value=self.admin):
            return getattr(self.client, method)(url, data=data, format='json')

    def test_list_search_and_uuid_reads_are_public_with_existing_fields(self):
        with mock.patch(AUTH_LOOKUP, side_effect=AssertionError('public read resolved identity')):
            for url in ('/api/authors', '/api/authors?search=Alice', self.url):
                with self.subTest(url=url):
                    response = self.client.get(url)
                    self.assertEqual(response.status_code, 200)
                    if 'results' in response.data:
                        self.assertEqual(set(response.data), {'count', 'next', 'previous', 'results'})
                        rows = response.data['results']
                    else:
                        rows = [response.data]
                    self.assertTrue(rows)
                    for row in rows:
                        self.assertEqual(set(row), AUTHOR_RESPONSE_FIELDS)
            self.assertEqual(self.client.get('/api/authors?search=Alice').data['count'], 1)

    def test_non_admin_cannot_post_put_patch_or_delete(self):
        with mock.patch(AUTH_LOOKUP, return_value=self.person):
            for method, url in (
                ('post', '/api/authors'), ('put', self.url),
                ('patch', self.url), ('delete', self.url),
            ):
                with self.subTest(method=method):
                    response = getattr(self.client, method)(url, data={}, format='json')
                    self.assertEqual(response.status_code, 403)
        self.assert_membership(self.publication, [self.author, self.other])

    def test_malformed_delete_body_returns_400_without_writes(self):
        for url in (self.url, '/api/publications/' + self.publication.uuid):
            for data in (['invalid'], 'invalid'):
                with self.subTest(url=url, data=data):
                    self.assertEqual(self.as_admin('delete', url, data).status_code, 400)
        self.assert_membership(self.publication, [self.author, self.other])

    def test_admin_crud_preserves_status_shapes_and_publication_membership(self):
        data = {
            'uuid': str(uuid4()), 'author_name': 'Carol Three',
            'display_name': 'Carol Three', 'publication_uuid': self.publication.uuid,
        }
        response = self.as_admin('post', '/api/authors', data)
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(set(response.data), AUTHOR_RESPONSE_FIELDS)
        added = Author.objects.get(uuid=data['uuid'])
        self.assert_membership(self.publication, [self.author, self.other, added])
        url = '/api/authors/' + added.uuid
        response = self.as_admin('put', url, {**data, 'display_name': 'C. Three'})
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(set(response.data), AUTHOR_RESPONSE_FIELDS)
        self.assertEqual(response.data['display_name'], 'C. Three')
        response = self.as_admin('patch', url, {'author_order': 0})
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(set(response.data), AUTHOR_RESPONSE_FIELDS)
        self.assert_membership(self.publication, [added, self.author, self.other])
        response = self.as_admin('delete', url)
        self.assertEqual(response.status_code, 204)
        self.assertFalse(response.content)
        self.assertFalse(Author.objects.filter(uuid=added.uuid).exists())
        self.assert_membership(self.publication, [self.author, self.other])

    def test_admin_attribution_records_decision_and_requires_reason_to_replace(self):
        response = self.as_admin('patch', self.url, {'fabric_uuid': self.person.uuid})
        self.assertEqual(response.status_code, 200, response.data)
        claim = AuthorClaim.objects.get(author=self.author, api_user=self.person)
        self.assertEqual((claim.status, claim.source), (AuthorClaim.APPROVED, AuthorClaim.ADMIN))
        for payload in ({'fabric_uuid': ''}, {'author_name': 'Someone Else'}):
            with self.subTest(payload=payload):
                response = self.as_admin('patch', self.url, payload)
                self.assertEqual(response.status_code, 400, response.data)
                self.assertIn('correction_reason', response.data)
        self.author.refresh_from_db()
        self.assertEqual(self.author.fabric_uuid, self.person.uuid)
        response = self.as_admin('patch', self.url, {
            'fabric_uuid': '', 'correction_reason': 'Attribution was entered on the wrong author.',
        })
        self.assertEqual(response.status_code, 200, response.data)
        self.assertNotIn('correction_reason', response.data)
        correction = AuthorCorrection.objects.get(author_uuid=self.author.uuid)
        self.assertEqual(correction.actor_uuid, self.admin.uuid)
        self.assertEqual(correction.before['author']['fabric_uuid'], self.person.uuid)
        self.assertEqual(correction.after['author']['fabric_uuid'], '')
        claim.refresh_from_db()
        self.assertEqual(claim.status, AuthorClaim.APPROVED)

    def test_deleting_claimed_author_needs_reason_and_retains_snapshot(self):
        self.claim(self.author)
        claim = AuthorClaim.objects.get(author=self.author)
        response = self.as_admin('delete', self.url)
        self.assertEqual(response.status_code, 400)
        self.assertTrue(AuthorClaim.objects.filter(pk=claim.pk).exists())
        response = self.as_admin('delete', self.url, {'correction_reason': 'Duplicate author entry.'})
        self.assertEqual(response.status_code, 204)
        self.assert_membership(self.publication, [self.other])
        self.assertFalse(Author.objects.filter(pk=self.author.pk).exists())
        correction = AuthorCorrection.objects.get(author_uuid=self.author.uuid)
        self.assertIsNone(correction.after)
        self.assertEqual(correction.before['author']['fabric_uuid'], self.person.uuid)
        self.assertEqual(correction.before['claims'][0]['uuid'], claim.uuid)
        self.assertEqual(correction.before['claims'][0]['status'], AuthorClaim.APPROVED)

    def test_rejected_history_also_requires_explicit_delete_correction(self):
        claim = AuthorClaim.objects.create(
            author=self.author, api_user=self.person, uuid=str(uuid4()),
            status=AuthorClaim.REJECTED, source=AuthorClaim.MACHINE,
        )
        self.assertEqual(self.as_admin('delete', self.url).status_code, 400)
        response = self.as_admin('delete', self.url, {'correction_reason': 'Remove duplicate slot.'})
        self.assertEqual(response.status_code, 204)
        correction = AuthorCorrection.objects.get(author_uuid=self.author.uuid)
        self.assertEqual(correction.before['claims'][0]['uuid'], claim.uuid)
        self.assertEqual(correction.before['claims'][0]['status'], AuthorClaim.REJECTED)

    def test_move_keeps_both_publication_lists_consistent(self):
        destination = self.make_publication(('Carol Three',))
        first = Author.objects.get(uuid=destination.authors[0])
        response = self.as_admin('patch', self.url, {'publication_uuid': destination.uuid})
        self.assertEqual(response.status_code, 200, response.data)
        self.assert_membership(self.publication, [self.other])
        self.assert_membership(destination, [first, self.author])

    def test_move_withdraws_old_publication_suggestions_but_keeps_decisions(self):
        destination = self.make_publication(('Carol Three',))
        rejected = AuthorClaim.objects.create(
            author=self.author, api_user=self.person, uuid=str(uuid4()),
            status=AuthorClaim.REJECTED, source=AuthorClaim.MACHINE,
        )
        suggested = AuthorClaim.objects.create(
            author=self.author, api_user=self.admin, uuid=str(uuid4()),
            status=AuthorClaim.SUGGESTED, source=AuthorClaim.MACHINE,
            signals={'project': {'detail': 'Evidence from the original publication'}},
        )
        response = self.as_admin('patch', self.url, {
            'publication_uuid': destination.uuid,
            'correction_reason': 'The author entry belongs to the other publication.',
        })
        self.assertEqual(response.status_code, 200, response.data)
        self.assertFalse(AuthorClaim.objects.filter(pk=suggested.pk).exists())
        rejected.refresh_from_db()
        self.assertEqual(rejected.status, AuthorClaim.REJECTED)
        self.assertEqual(rejected.author_id, self.author.pk)

    def test_invalid_destination_and_unknown_identity_write_nothing(self):
        for payload in ({'publication_uuid': str(uuid4())}, {'fabric_uuid': str(uuid4())}):
            with self.subTest(payload=payload):
                response = self.as_admin('patch', self.url, payload)
                self.assertEqual(response.status_code, 400, response.data)
                self.assert_membership(self.publication, [self.author, self.other])
                self.assertFalse(self.author.fabric_uuid)

    def test_duplicate_author_uuid_returns_400_without_changing_either_publication(self):
        destination = self.make_publication(('Carol Three',))
        existing_members = list(destination.authors)
        response = self.as_admin('post', '/api/authors', {
            'uuid': self.author.uuid, 'author_name': 'Another Person',
            'display_name': 'Another Person', 'publication_uuid': destination.uuid,
        })
        self.assertEqual(response.status_code, 400, response.data)
        self.assertEqual(Author.objects.filter(uuid=self.author.uuid).count(), 1)
        self.assert_membership(self.publication, [self.author, self.other])
        destination.refresh_from_db()
        self.assertEqual(destination.authors, existing_members)

    def test_failure_recording_claim_rolls_back_author_and_membership(self):
        with mock.patch(
            'publicationtrkr.apps.publications.utils.author_mutations.record_admin_claim',
            side_effect=RuntimeError('ledger unavailable'),
        ):
            with self.assertRaisesRegex(RuntimeError, 'ledger unavailable'):
                self.as_admin('patch', self.url, {'fabric_uuid': self.person.uuid, 'author_order': 1})
        self.assert_membership(self.publication, [self.author, self.other])
        self.assertFalse(self.author.fabric_uuid)
        self.assertFalse(AuthorClaim.objects.exists())

    def test_failure_recording_correction_rolls_back_deletion(self):
        self.claim(self.author)
        with mock.patch(
            'publicationtrkr.apps.publications.utils.author_mutations.record_correction',
            side_effect=RuntimeError('audit unavailable'),
        ):
            with self.assertRaisesRegex(RuntimeError, 'audit unavailable'):
                self.as_admin('delete', self.url, {'correction_reason': 'Duplicate.'})
        self.assert_membership(self.publication, [self.author, self.other])
        self.assertEqual(self.author.fabric_uuid, self.person.uuid)
        self.assertTrue(AuthorClaim.objects.filter(author=self.author).exists())


class PublicationAuthorIdentityTests(IntegrityFixtures, TestCase):
    def setUp(self):
        self.admin = self.make_user(admin=True)
        self.person = self.make_user()
        self.publication = self.make_publication()
        self.alice = Author.objects.get(uuid=self.publication.authors[0])
        self.bob = Author.objects.get(uuid=self.publication.authors[1])

    def update_names(self, names):
        return update_publication(
            publication=self.publication, api_user=self.admin, data={'authors': names},
        )

    def test_reorder_keeps_attribution_attached_to_same_author(self):
        self.claim(self.alice)
        claim = AuthorClaim.objects.get(author=self.alice)
        self.update_names(['Bob Two', 'Alice One'])
        self.assert_membership(self.publication, [self.bob, self.alice])
        self.assertEqual(self.alice.fabric_uuid, self.person.uuid)
        self.assertEqual(self.alice.author_name, 'Alice One')
        self.bob.refresh_from_db()
        self.assertFalse(self.bob.fabric_uuid)
        claim.refresh_from_db()
        self.assertEqual(claim.author_id, self.alice.pk)

    def test_publication_reads_preserve_consumer_shape_and_author_order(self):
        self.claim(self.alice)
        self.update_names(['Bob Two', 'Alice One'])
        with mock.patch(AUTH_LOOKUP, side_effect=AssertionError('public read resolved identity')):
            response = APIClient().get('/api/publications/' + self.publication.uuid)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(set(response.data), {
            'authors', 'bibtex', 'created', 'created_by', 'link', 'modified',
            'modified_by', 'project_name', 'project_uuid', 'title', 'uuid', 'venue', 'year',
        })
        self.assertEqual([a['uuid'] for a in response.data['authors']], [self.bob.uuid, self.alice.uuid])
        for author in response.data['authors']:
            self.assertEqual(set(author), AUTHOR_RESPONSE_FIELDS)
        self.assertEqual(response.data['authors'][1]['fabric_uuid'], self.person.uuid)

    def test_insertion_and_unclaimed_removal_preserve_remaining_uuid(self):
        self.claim(self.alice)
        self.update_names(['Carol Three', 'Alice One', 'Bob Two'])
        self.publication.refresh_from_db()
        carol = Author.objects.get(uuid=self.publication.authors[0])
        self.assertNotIn(carol.uuid, [self.alice.uuid, self.bob.uuid])
        self.assert_membership(self.publication, [carol, self.alice, self.bob])
        self.update_names(['Carol Three', 'Alice One'])
        self.assert_membership(self.publication, [carol, self.alice])
        self.assertFalse(Author.objects.filter(pk=self.bob.pk).exists())
        self.assertEqual(self.alice.fabric_uuid, self.person.uuid)

    def test_ordinary_list_edits_cannot_remove_or_replace_claimed_author(self):
        self.claim(self.alice)
        for names in (['Bob Two'], ['Someone Else', 'Bob Two']):
            with self.subTest(names=names):
                with self.assertRaises(ValidationError):
                    self.update_names(names)
                self.assert_membership(self.publication, [self.alice, self.bob])
                self.assertEqual(self.alice.author_name, 'Alice One')
                self.assertEqual(self.alice.fabric_uuid, self.person.uuid)
        self.assertFalse(AuthorCorrection.objects.exists())

    def test_publication_delete_preserves_claim_evidence_with_explicit_correction(self):
        self.claim(self.alice)
        client = APIClient()
        url = '/api/publications/' + self.publication.uuid
        with mock.patch(AUTH_LOOKUP, return_value=self.admin):
            response = client.delete(url, format='json')
            self.assertEqual(response.status_code, 400)
            response = client.delete(url, data={'correction_reason': 'Duplicate paper.'}, format='json')
        self.assertEqual(response.status_code, 204)
        self.assertFalse(Publication.objects.filter(pk=self.publication.pk).exists())
        self.assertFalse(Author.objects.filter(publication_uuid=self.publication.uuid).exists())
        correction = AuthorCorrection.objects.get(author_uuid=self.alice.uuid)
        self.assertEqual(correction.before['claims'][0]['api_user__uuid'], self.person.uuid)
        self.assertIsNone(correction.after)


class ClaimCurrentStateTests(IntegrityFixtures, TestCase):
    def setUp(self):
        self.admin = self.make_user(admin=True)
        self.person = self.make_user()
        publication = self.make_publication(('Alice One',))
        self.author = Author.objects.get(uuid=publication.authors[0])
        self.claim_row = AuthorClaim.objects.create(
            author=self.author, api_user=self.person, uuid=str(uuid4()),
            status=AuthorClaim.SUGGESTED, source=AuthorClaim.MACHINE,
        )

    def test_stale_approval_cannot_reverse_rejection(self):
        stale = AuthorClaim.objects.get(pk=self.claim_row.pk)
        reject_suggestion(self.claim_row, decided_by=self.admin)
        with self.assertRaises(ClaimDecisionError):
            approve_suggestion(stale, decided_by=self.admin)
        self.claim_row.refresh_from_db()
        self.author.refresh_from_db()
        self.assertEqual(self.claim_row.status, AuthorClaim.REJECTED)
        self.assertFalse(self.author.fabric_uuid)

    def test_relocated_claim_cannot_be_decided_using_stale_author_identity(self):
        destination = Author.objects.get(uuid=self.make_publication(('Bob Two',)).authors[0])
        AuthorClaim.objects.filter(pk=self.claim_row.pk).update(author=destination)
        for decide in (approve_suggestion, reject_suggestion):
            with self.subTest(decision=decide.__name__):
                with self.assertRaisesMessage(ClaimDecisionError, 'moved to another author'):
                    decide(self.claim_row, decided_by=self.admin)
        self.claim_row.refresh_from_db()
        self.author.refresh_from_db()
        destination.refresh_from_db()
        self.assertEqual(self.claim_row.status, AuthorClaim.SUGGESTED)
        self.assertEqual(self.claim_row.author_id, destination.pk)
        self.assertFalse(self.author.fabric_uuid)
        self.assertFalse(destination.fabric_uuid)

    def test_deleted_author_queue_action_reports_stale_state(self):
        self.author.delete()
        for decide in (approve_suggestion, reject_suggestion):
            with self.subTest(decision=decide.__name__):
                with self.assertRaisesMessage(ClaimDecisionError, 'author was removed'):
                    decide(self.claim_row, decided_by=self.admin)

    def test_stale_rejection_cannot_reverse_approval(self):
        stale = AuthorClaim.objects.get(pk=self.claim_row.pk)
        approve_suggestion(self.claim_row, decided_by=self.admin)
        with self.assertRaises(ClaimDecisionError):
            reject_suggestion(stale, decided_by=self.admin)
        self.claim_row.refresh_from_db()
        self.author.refresh_from_db()
        self.assertEqual(self.claim_row.status, AuthorClaim.APPROVED)
        self.assertEqual(self.author.fabric_uuid, self.person.uuid)

    def test_withdrawn_suggestion_cannot_be_restored_by_stale_approval(self):
        stale = AuthorClaim.objects.select_related('author', 'api_user').get(pk=self.claim_row.pk)
        winner = self.make_user()
        mutate_author(actor=winner, author=self.author, data={}, self_claim=True)
        with self.assertRaises(ClaimDecisionError):
            approve_suggestion(stale, decided_by=self.admin)
        self.author.refresh_from_db()
        self.assertEqual(self.author.fabric_uuid, winner.uuid)
        self.assertFalse(AuthorClaim.objects.filter(pk=stale.pk).exists())


@skipUnlessDBFeature('has_select_for_update')
class ConcurrentSelfClaimTests(IntegrityFixtures, TransactionTestCase):
    def test_same_uuid_cannot_be_created_concurrently_on_different_publications(self):
        self.admin = self.make_user(admin=True)
        publications = [self.make_publication(('Alice One',)), self.make_publication(('Bob Two',))]
        collision_uuid = str(uuid4())
        barrier = Barrier(2)

        def synchronize_insert(sender, instance, **kwargs):
            if instance.uuid == collision_uuid and instance._state.adding:
                # Both transactions have passed the existence check and hold different
                # publication locks. Only database uniqueness can serialize these inserts.
                barrier.wait(timeout=10)

        def attempt(publication_uuid):
            close_old_connections()
            try:
                actor = ApiUser.objects.get(pk=self.admin.pk)
                try:
                    mutate_author(actor=actor, data={
                        'uuid': collision_uuid, 'publication_uuid': publication_uuid,
                        'author_name': 'Shared UUID', 'display_name': 'Shared UUID',
                    })
                except IntegrityError:
                    return 'duplicate', publication_uuid
                return 'created', publication_uuid
            finally:
                close_old_connections()

        pre_save.connect(synchronize_insert, sender=Author)
        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(attempt, publication.uuid) for publication in publications]
                outcomes = [future.result(timeout=15) for future in futures]
        finally:
            pre_save.disconnect(synchronize_insert, sender=Author)
        self.assertCountEqual([state for state, _ in outcomes], ['created', 'duplicate'])
        self.assertEqual(Author.objects.filter(uuid=collision_uuid).count(), 1)
        winner = next(uuid for state, uuid in outcomes if state == 'created')
        for publication in publications:
            publication.refresh_from_db()
            self.assertEqual(collision_uuid in publication.authors, publication.uuid == winner)

    def test_two_users_cannot_both_claim_the_same_author(self):
        self.admin = self.make_user(admin=True)
        people = [self.make_user(), self.make_user()]
        publication = self.make_publication(('Alice One',))
        author_pk = Author.objects.get(uuid=publication.authors[0]).pk
        barrier = Barrier(2)

        def attempt(user_pk):
            close_old_connections()
            try:
                actor = ApiUser.objects.get(pk=user_pk)
                stale_author = Author.objects.get(pk=author_pk)
                barrier.wait(timeout=10)
                try:
                    mutate_author(actor=actor, author=stale_author, data={}, self_claim=True)
                except ValidationError:
                    return 'refused', actor.uuid
                return 'claimed', actor.uuid
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(attempt, person.pk) for person in people]
            outcomes = [future.result(timeout=15) for future in futures]
        self.assertCountEqual([status for status, _ in outcomes], ['claimed', 'refused'])
        winner = next(uuid for status, uuid in outcomes if status == 'claimed')
        author = Author.objects.get(pk=author_pk)
        self.assertEqual(author.fabric_uuid, winner)
        claim = AuthorClaim.objects.get(author=author)
        self.assertEqual(claim.api_user.uuid, winner)
        self.assertEqual(claim.status, AuthorClaim.SELF_ASSERTED)

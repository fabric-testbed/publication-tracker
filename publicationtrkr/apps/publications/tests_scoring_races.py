"""Interleave human decisions after scoring loads its initial database snapshot."""

from datetime import datetime, timezone
from io import StringIO
from unittest import mock
from uuid import uuid4

from django.test import TestCase

from publicationtrkr.apps.apiuser.models import ApiUser
from publicationtrkr.apps.publications.management.commands.score_author_claims import Command
from publicationtrkr.apps.publications.models import Author, AuthorClaim
from publicationtrkr.apps.publications.utils.author_mutations import mutate_author
from publicationtrkr.apps.publications.utils.claim_ledger import approve_suggestion, reject_suggestion
from publicationtrkr.apps.publications.utils.publication_builder import create_publication


class ScorerDecisionInterleavingTests(TestCase):
    def setUp(self):
        self.admin = ApiUser.objects.create(
            uuid=str(uuid4()), name='Admin', fabric_roles=['publication-tracker-admins'])
        self.person = ApiUser.objects.create(uuid=str(uuid4()), name='Alice One')
        publication = create_publication(
            api_user=self.admin,
            data={'title': 'Concurrency evidence', 'year': '2026', 'authors': ['Alice One']},
        )
        self.author = Author.objects.get(uuid=publication.authors[0])
        self.suggestion = AuthorClaim.objects.create(
            author=self.author, api_user=self.person, uuid=str(uuid4()), score=0.5,
            signals={'original': True}, status=AuthorClaim.SUGGESTED, source=AuthorClaim.MACHINE,
        )

    def score_with_interleaving(self, callback):
        # Command._score materializes authors, decided pairs and suggestions before
        # asking for candidates. A decision in this callback reproduces the vulnerable
        # interval without relying on thread scheduling or sleeps.
        with mock.patch(
            'publicationtrkr.apps.publications.management.commands.score_author_claims.candidates_for_author',
            side_effect=callback,
        ):
            Command(stdout=StringIO())._score(
                full=False, dry_run=False, tracker=None, now=datetime.now(timezone.utc))

    def test_cleanup_keeps_rejection_made_after_initial_snapshot(self):
        def decide(*args):
            reject_suggestion(self.suggestion, decided_by=self.admin)
            return []

        self.score_with_interleaving(decide)
        self.suggestion.refresh_from_db()
        self.assertEqual(self.suggestion.status, AuthorClaim.REJECTED)
        self.assertEqual(self.suggestion.decided_by_id, self.admin.pk)
        self.assertEqual(self.suggestion.signals, {'original': True})

    def test_cleanup_keeps_approval_made_after_initial_snapshot(self):
        def decide(*args):
            approve_suggestion(self.suggestion, decided_by=self.admin)
            return []

        self.score_with_interleaving(decide)
        self.suggestion.refresh_from_db()
        self.author.refresh_from_db()
        self.assertEqual(self.suggestion.status, AuthorClaim.APPROVED)
        self.assertEqual(self.author.fabric_uuid, self.person.uuid)

    def test_rescoring_keeps_rejected_status_and_original_evidence(self):
        def decide(*args):
            reject_suggestion(self.suggestion, decided_by=self.admin)
            return [(0.9, self.person, {'rescored': True})]

        self.score_with_interleaving(decide)
        self.suggestion.refresh_from_db()
        self.assertEqual(self.suggestion.status, AuthorClaim.REJECTED)
        self.assertEqual(self.suggestion.score, 0.5)
        self.assertEqual(self.suggestion.signals, {'original': True})

    def test_self_claim_prevents_stale_candidate_from_reappearing(self):
        winner = ApiUser.objects.create(uuid=str(uuid4()), name='Alice Different')

        def decide(*args):
            mutate_author(actor=winner, author=self.author, data={}, self_claim=True)
            return [(0.9, self.person, {'rescored': True})]

        self.score_with_interleaving(decide)
        self.author.refresh_from_db()
        self.assertEqual(self.author.fabric_uuid, winner.uuid)
        claims = list(self.author.claims.all())
        self.assertEqual(len(claims), 1)
        self.assertEqual(claims[0].api_user_id, winner.pk)
        self.assertEqual(claims[0].status, AuthorClaim.SELF_ASSERTED)

    def test_rename_prevents_old_name_suggestion_from_reappearing(self):
        def rename(*args):
            mutate_author(actor=self.admin, author=self.author, data={'author_name': 'Bob Two'})
            return [(0.9, self.person, {'rescored': True})]

        self.score_with_interleaving(rename)
        self.author.refresh_from_db()
        self.assertEqual(self.author.author_name, 'Bob Two')
        self.assertFalse(self.author.claims.exists())

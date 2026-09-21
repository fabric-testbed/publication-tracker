"""Regressions found while reviewing the attribution repair and ledger changes."""

from io import StringIO
from unittest import mock
from uuid import uuid4

from django.core.management import call_command
from django.test import TestCase

from publicationtrkr.apps.apiuser.models import ApiUser
from publicationtrkr.apps.publications.management.commands.repair_author_records import Command
from publicationtrkr.apps.publications.models import Author, AuthorClaim, AuthorCorrection
from publicationtrkr.apps.publications.utils.author_mutations import mutate_author
from publicationtrkr.apps.publications.utils.publication_builder import create_publication


class ReviewedLedgerIntegrityTests(TestCase):
    def setUp(self):
        self.admin = ApiUser.objects.create(
            uuid=str(uuid4()), name='Admin', fabric_roles=['publication-tracker-admins'])
        self.person = ApiUser.objects.create(
            uuid=str(uuid4()), name='Alice One', fabric_roles=['Jupyterhub'])
        self.publication = create_publication(
            api_user=self.admin, data={
                'title': str(uuid4()), 'year': '2026', 'authors': ['Alice One', 'Alice One'],
            })
        self.first, self.second = [Author.objects.get(uuid=u) for u in self.publication.authors]

    def run_repair(self, plan):
        with mock.patch.object(Command, '_load_plan', return_value=plan):
            call_command('repair_author_records', apply=True, as_user=self.admin.uuid, stdout=StringIO())

    def test_owner_display_edit_preserves_original_admin_decision(self):
        mutate_author(actor=self.admin, author=self.first, data={'fabric_uuid': self.person.uuid})
        claim = AuthorClaim.objects.get(author=self.first, api_user=self.person)
        original = (claim.uuid, claim.status, claim.source, claim.decided_by_id, claim.decided_at)
        mutate_author(
            actor=self.person, author=self.first, data={'display_name': 'A. One'}, self_claim=True,
        )
        claim.refresh_from_db()
        self.first.refresh_from_db()
        self.assertEqual(self.first.display_name, 'A. One')
        self.assertEqual(
            (claim.uuid, claim.status, claim.source, claim.decided_by_id, claim.decided_at), original,
        )

    def test_self_claim_of_unattributed_author_keeps_prior_rejection_evidence(self):
        claim = AuthorClaim.objects.create(
            author=self.first, api_user=self.person, uuid=str(uuid4()),
            status=AuthorClaim.REJECTED, source=AuthorClaim.MACHINE, decided_by=self.admin,
            signals={'name': 'reviewed candidate'},
        )
        mutate_author(actor=self.person, author=self.first, data={}, self_claim=True)
        self.first.refresh_from_db()
        claim.refresh_from_db()
        self.assertEqual(self.first.fabric_uuid, self.person.uuid)
        self.assertEqual(claim.status, AuthorClaim.SELF_ASSERTED)
        corrections = list(AuthorCorrection.objects.filter(author_uuid=self.first.uuid))
        self.assertTrue(corrections, 'A replaced rejection must remain available as evidence.')
        previous = [row for correction in corrections for row in correction.before['claims']]
        self.assertTrue(any(row['uuid'] == claim.uuid and row['status'] == AuthorClaim.REJECTED
                            for row in previous))

    def test_repair_snapshots_original_claim_before_relocation_and_rename(self):
        mutate_author(actor=self.admin, author=self.first, data={'fabric_uuid': self.person.uuid})
        claim = AuthorClaim.objects.get(author=self.first)
        self.run_repair({'publications': [{
            'publication_uuid': self.publication.uuid,
            'expect_authors': ['Alice One', 'Alice One'],
            'authors': [{'name': 'Bob Two'}, {'name': 'Alice One', 'fabric_uuid': self.person.uuid}],
            'note': 'The first duplicate name represents Bob; Alice is the second author.',
        }]})
        self.first.refresh_from_db()
        self.second.refresh_from_db()
        claim.refresh_from_db()
        self.assertEqual(claim.author_id, self.second.pk)
        self.assertEqual(self.first.author_name, 'Bob Two')
        self.assertFalse(self.first.fabric_uuid)
        self.assertEqual(self.second.fabric_uuid, self.person.uuid)
        corrections = list(AuthorCorrection.objects.filter(author_uuid=self.first.uuid))
        self.assertTrue(corrections, 'The repair must retain source attribution before detaching it.')
        original = [correction.before for correction in corrections
                    if correction.before['author']['fabric_uuid'] == self.person.uuid]
        self.assertTrue(original)
        self.assertTrue(any(row['uuid'] == claim.uuid for state in original for row in state['claims']))

    def test_orphan_repair_retains_the_deleted_claim_not_just_its_twin(self):
        mutate_author(actor=self.admin, author=self.first, data={'fabric_uuid': self.person.uuid})
        orphan = Author.objects.create(
            uuid=str(uuid4()), publication_uuid=self.publication.uuid,
            author_name=self.first.author_name, display_name=self.first.display_name,
            fabric_uuid=self.person.uuid,
        )
        lost_claim = AuthorClaim.objects.create(
            author=orphan, api_user=self.person, uuid=str(uuid4()),
            status=AuthorClaim.SELF_ASSERTED, source=AuthorClaim.SELF, decided_by=self.person,
        )
        self.run_repair({'orphan_authors': [{
            'author_uuid': orphan.uuid, 'publication_uuid': self.publication.uuid,
        }]})
        self.assertFalse(Author.objects.filter(pk=orphan.pk).exists())
        corrections = list(AuthorCorrection.objects.filter(author_uuid=orphan.uuid))
        self.assertTrue(corrections, 'Deleting a duplicate must preserve its distinct decision evidence.')
        previous = [row for correction in corrections for row in correction.before['claims']]
        self.assertTrue(any(row['uuid'] == lost_claim.uuid and row['status'] == AuthorClaim.SELF_ASSERTED
                            for row in previous))
        self.assertTrue(all(correction.after is None for correction in corrections))

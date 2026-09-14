"""
Tests for the publications data migrations (issues #24 and #61).

Kept out of tests.py because a migration is tested differently from application code:
these call the RunPython function directly against the real model registry rather than
replaying the migration graph, which is enough to pin the query that decides what gets
deleted.

The 0002 cleanup runs once, unattended, at boot on production. The thing worth pinning
is not that it deletes orphans -- it is that it deletes nothing else.

The 0005 backfill runs the same way, and what is worth pinning there is that it reads
the order out of Publication.authors rather than inventing one, and that it says so
out loud when the data cannot supply an answer.
"""

from importlib import import_module
from unittest import mock

from django.apps import apps as global_apps
from django.test import TestCase

from publicationtrkr.apps.publications.models import Author, Publication

delete_orphan_authors = import_module(
    'publicationtrkr.apps.publications.migrations.0002_delete_orphan_authors'
).delete_orphan_authors

backfill_author_order = import_module(
    'publicationtrkr.apps.publications.migrations.0005_author_order'
).backfill_author_order


class DeleteOrphanAuthorsTests(TestCase):

    def make_author(self, uuid, publication_uuid, **overrides):
        fields = {
            'author_name': 'Some One',
            'display_name': 'Some One',
            'publication_uuid': publication_uuid,
            'uuid': uuid,
        }
        fields.update(overrides)
        return Author.objects.create(**fields)

    def make_publication(self, uuid, author_uuids):
        return Publication.objects.create(
            authors=author_uuids, title='Paper {0}'.format(uuid), uuid=uuid, year='2024')

    def run_migration(self):
        """Returns everything the migration printed, which is the deploy-time record."""
        with mock.patch('builtins.print') as printed:
            delete_orphan_authors(global_apps, None)
        return '\n'.join(str(call.args[0]) for call in printed.call_args_list)

    def test_rows_belonging_to_a_publication_are_kept(self):
        self.make_publication('pub-1', ['author-1'])
        self.make_author('author-1', 'pub-1')
        self.make_author('author-2', 'pub-gone')
        self.make_author('author-3', 'pub-gone')

        output = self.run_migration()

        self.assertEqual([a.uuid for a in Author.objects.all()], ['author-1'])
        self.assertIn('deleted 2 orphan Author row(s)', output)

    def test_an_empty_table_is_not_an_error(self):
        # Every environment past the first runs this against nothing to do.
        output = self.run_migration()
        self.assertIn('nothing to delete', output)
        self.assertEqual(Author.objects.count(), 0)

    def test_a_claimed_orphan_is_deleted_but_reported(self):
        # All 19 production rows carried a NULL fabric_uuid on 2026-09-01. If that
        # stops being true the row is still unreachable, but it should not go quietly.
        self.make_author('author-1', 'pub-gone', fabric_uuid='user-1')

        output = self.run_migration()

        self.assertEqual(Author.objects.count(), 0)
        self.assertIn('WARNING', output)
        self.assertIn('carry a fabric_uuid', output)

    def test_an_orphan_a_publication_still_lists_is_reported(self):
        # Nothing enforces that an orphan is absent from every authors array. If one
        # is present, a live publication loses an author name and the log says so.
        self.make_publication('pub-1', ['author-1'])
        self.make_author('author-1', 'pub-gone')

        output = self.run_migration()

        self.assertEqual(Author.objects.count(), 0)
        self.assertIn('still listed by 1 publication(s): pub-1', output)


class BackfillAuthorOrderTests(TestCase):
    """
    0005 copies each Author's slot out of Publication.authors, which has always been in
    the right order. These pin that it copies rather than guesses.
    """

    def make_author(self, uuid, publication_uuid, author_order=0, **overrides):
        fields = {
            'author_name': 'Some One',
            'author_order': author_order,
            'display_name': 'Some One',
            'publication_uuid': publication_uuid,
            'uuid': uuid,
        }
        fields.update(overrides)
        return Author.objects.create(**fields)

    def make_publication(self, uuid, author_uuids):
        return Publication.objects.create(
            authors=author_uuids, title='Paper {0}'.format(uuid), uuid=uuid, year='2024')

    def run_migration(self):
        """Returns everything the migration printed, which is the deploy-time record."""
        with mock.patch('builtins.print') as printed:
            backfill_author_order(global_apps, None)
        return '\n'.join(str(call.args[0]) for call in printed.call_args_list)

    def test_slots_come_from_the_array_not_from_insertion_order(self):
        # The rows are created in an order that disagrees with the array on purpose.
        self.make_publication('pub-1', ['author-c', 'author-a', 'author-b'])
        self.make_author('author-a', 'pub-1')
        self.make_author('author-b', 'pub-1')
        self.make_author('author-c', 'pub-1')

        output = self.run_migration()

        self.assertEqual(
            {a.uuid: a.author_order for a in Author.objects.all()},
            {'author-c': 0, 'author-a': 1, 'author-b': 2})
        # Two, not three: author-c belongs in slot 0 and was already there, and the
        # backfill only writes the rows whose value actually changes.
        self.assertIn('set author_order on 2 Author row(s)', output)

    def test_each_publication_is_numbered_from_zero(self):
        self.make_publication('pub-1', ['author-1', 'author-2'])
        self.make_publication('pub-2', ['author-3', 'author-4'])
        for uuid, pub in (('author-1', 'pub-1'), ('author-2', 'pub-1'),
                          ('author-3', 'pub-2'), ('author-4', 'pub-2')):
            self.make_author(uuid, pub)

        self.run_migration()

        self.assertEqual(
            {a.uuid: a.author_order for a in Author.objects.all()},
            {'author-1': 0, 'author-2': 1, 'author-3': 0, 'author-4': 1})

    def test_a_row_no_publication_lists_is_left_at_zero_and_reported(self):
        # The orphan class 0002 cleaned up. None were expected to remain, so it warns
        # rather than quietly defaulting.
        self.make_author('author-1', 'pub-gone')

        output = self.run_migration()

        self.assertEqual(Author.objects.get(uuid='author-1').author_order, 0)
        self.assertIn('WARNING', output)
        self.assertIn('named by no publication', output)

    def test_a_uuid_two_publications_both_list_is_reported(self):
        # Impossible by construction -- _create_authors mints a fresh uuid per
        # publication -- but the slot would be ambiguous, so it does not go quietly.
        self.make_publication('pub-1', ['author-1'])
        self.make_publication('pub-2', ['author-x', 'author-1'])
        self.make_author('author-1', 'pub-1')
        self.make_author('author-x', 'pub-2')

        output = self.run_migration()

        self.assertIn('WARNING', output)
        self.assertIn('more than one', output)
        self.assertIn('author-1', output)

    def test_rerunning_is_idempotent(self):
        # Migrations are not re-run in practice, but the backfill should be safe if it is.
        self.make_publication('pub-1', ['author-b', 'author-a'])
        self.make_author('author-a', 'pub-1')
        self.make_author('author-b', 'pub-1')

        self.run_migration()
        output = self.run_migration()

        self.assertEqual(
            {a.uuid: a.author_order for a in Author.objects.all()},
            {'author-b': 0, 'author-a': 1})
        # Nothing left to change the second time round.
        self.assertIn('set author_order on 0 Author row(s)', output)

    def test_an_empty_table_is_not_an_error(self):
        output = self.run_migration()
        self.assertIn('set author_order on 0 Author row(s)', output)

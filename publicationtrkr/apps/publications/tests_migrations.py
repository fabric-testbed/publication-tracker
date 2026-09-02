"""
Tests for the publications data migrations (issue #24).

Kept out of tests.py because a migration is tested differently from application code:
these call the RunPython function directly against the real model registry rather than
replaying the migration graph, which is enough to pin the query that decides what gets
deleted.

The 0002 cleanup runs once, unattended, at boot on production. The thing worth pinning
is not that it deletes orphans -- it is that it deletes nothing else.
"""

from importlib import import_module
from unittest import mock

from django.apps import apps as global_apps
from django.test import TestCase

from publicationtrkr.apps.publications.models import Author, Publication

delete_orphan_authors = import_module(
    'publicationtrkr.apps.publications.migrations.0002_delete_orphan_authors'
).delete_orphan_authors


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

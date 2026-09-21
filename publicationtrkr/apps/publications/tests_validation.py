"""Typed publication inputs and project integrity across single and bulk writes."""

from unittest import mock

from django.test import TestCase
from rest_framework.test import APIRequestFactory

from publicationtrkr.apps.apiuser.models import ApiUser
from publicationtrkr.apps.publications.api.viewsets import PublicationViewSet
from publicationtrkr.apps.publications.models import Author, Publication
from publicationtrkr.apps.publications.utils.bulk_ingest import collect_records, ingest


PROJECT = '11111111-1111-4111-8111-111111111111'
BIBTEX = '@article{example, title={From BibTeX}, author={Alice Example}, year={2026}}'


class PublicationInputTests(TestCase):
    def setUp(self):
        self.user = ApiUser.objects.create(uuid='admin', fabric_roles=['publication-tracker-admins'])
        self.factory = APIRequestFactory()
        patcher = mock.patch(
            'publicationtrkr.apps.publications.api.viewsets.get_api_user', return_value=self.user)
        patcher.start()
        self.addCleanup(patcher.stop)

    def post(self, data):
        request = self.factory.post('/api/publications/', data, format='json')
        return PublicationViewSet.as_view({'post': 'create'})(request)

    def patch(self, publication, data):
        request = self.factory.patch('/api/publications/' + publication.uuid, data, format='json')
        return PublicationViewSet.as_view({'patch': 'partial_update'})(request, uuid=publication.uuid)

    def record(self, **overrides):
        return {'authors': ['Alice Example'], 'title': 'A paper', 'year': '2026', **overrides}

    def test_malformed_author_shapes_are_400_without_writes(self):
        for authors in ('Alice', {'name': 'Alice'}, [None], [12], ['  '], ['A' * 256]):
            with self.subTest(authors=authors):
                response = self.post(self.record(authors=authors))
                self.assertEqual(response.status_code, 400)
                self.assertIn('authors', response.data['ValidationError'][0])
                self.assertFalse(Publication.objects.exists())
                self.assertFalse(Author.objects.exists())

    def test_scalar_types_and_lengths_fail_before_database_writes(self):
        cases = (
            ('title', ['wrong']), ('year', 2026), ('venue', {'name': 'wrong'}),
            ('link', ['https://example.test']), ('bibtex', False),
            ('project_name', ['wrong']), ('project_uuid', 123),
            ('title', 'x' * 5001), ('year', 'x' * 256), ('venue', 'x' * 256),
            ('bibtex', 'x' * 10001), ('title', '  '), ('year', '\t'),
        )
        for field, value in cases:
            with self.subTest(field=field, value=value):
                response = self.post(self.record(**{field: value}))
                self.assertEqual(response.status_code, 400)
                self.assertTrue(any(field in error for error in response.data['ValidationError']))
        self.assertFalse(Publication.objects.exists())
        self.assertFalse(Author.objects.exists())

    def test_non_object_create_is_a_400(self):
        self.assertEqual(self.post(['not an object']).status_code, 400)

    def test_non_object_update_is_a_400_without_writes(self):
        self.assertEqual(self.post(self.record()).status_code, 201)
        publication = Publication.objects.get()
        before = publication.as_dict()
        self.assertEqual(self.patch(publication, ['not an object']).status_code, 400)
        publication.refresh_from_db()
        self.assertEqual(publication.as_dict(), before)

    def test_uuid_only_project_update_uses_one_authoritative_lookup(self):
        self.user.access_type = ApiUser.TOKEN
        self.assertEqual(self.post(self.record()).status_code, 201)
        publication = Publication.objects.get()
        publication.project_uuid = '22222222-2222-4222-8222-222222222222'
        publication.project_name = 'Previous project'
        publication.save()
        with mock.patch('publicationtrkr.apps.publications.api.validators.query_core_api_by_token',
                        return_value={'status': 200, 'size': 1,
                                      'results': [{'name': 'Canonical project'}]}) as lookup:
            response = self.patch(publication, {'project_uuid': PROJECT})
        self.assertEqual(response.status_code, 200, response.data)
        lookup.assert_called_once()
        self.assertEqual(lookup.call_args.kwargs['query'], '/projects/' + PROJECT)
        publication.refresh_from_db()
        self.assertEqual((publication.project_uuid, publication.project_name),
                         (PROJECT, 'Canonical project'))

    def test_bibtex_fallback_and_optional_updates_keep_existing_semantics(self):
        response = self.post({'bibtex': BIBTEX, 'authors': [], 'title': '', 'year': None})
        self.assertEqual(response.status_code, 201)
        publication = Publication.objects.get()
        self.assertEqual(publication.title, 'From BibTeX')
        self.assertEqual(Author.objects.get().author_name, 'Alice Example')
        before = publication.as_dict()
        response = self.patch(publication, {'title': '', 'year': None, 'venue': '', 'authors': None})
        self.assertEqual(response.status_code, 200)
        publication.refresh_from_db()
        self.assertEqual(publication.as_dict(), before)
        self.assertEqual(self.patch(publication, {'authors': []}).status_code, 400)

    def test_bibtex_derived_values_are_subject_to_the_same_limits(self):
        response = self.post({'bibtex': BIBTEX.replace('Alice Example', 'x' * 256)})
        self.assertEqual(response.status_code, 400)
        self.assertFalse(Publication.objects.exists())
        self.assertFalse(Author.objects.exists())

    def test_create_refuses_mismatched_project_name(self):
        with mock.patch('publicationtrkr.apps.publications.api.validators.query_project', return_value={
            'status': 200, 'size': 1, 'results': [{'name': 'Canonical project'}],
        }):
            response = self.post(self.record(project_uuid=PROJECT, project_name='Wrong name'))
        self.assertEqual(response.status_code, 400)
        self.assertIn('project_name', response.data['ValidationError'][0])
        self.assertFalse(Publication.objects.exists())

    def test_unknown_project_on_update_returns_field_error_without_index_error(self):
        self.assertEqual(self.post(self.record()).status_code, 201)
        publication = Publication.objects.get()
        with mock.patch('publicationtrkr.apps.publications.api.validators.query_project', return_value={
            'status': 404, 'size': 0, 'results': [],
        }):
            response = self.patch(publication, {'project_uuid': PROJECT, 'project_name': 'Unknown'})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(list(response.data['ValidationError'][0]), ['project_uuid'])
        publication.refresh_from_db()
        self.assertIsNone(publication.project_uuid)

    def test_bulk_rejects_invalid_records_but_creates_valid_neighbor(self):
        records, failures = collect_records(data=[
            self.record(authors='Alice'),
            self.record(title='Invalid project', project_uuid=PROJECT, project_name='Wrong name'),
            self.record(title='Valid project', project_uuid=PROJECT, project_name='Canonical project'),
        ])
        resolver = mock.Mock(return_value='Canonical project')
        summary = ingest(records, failures, api_user=self.user, resolve_project_name=resolver)
        self.assertEqual((summary['created'], summary['failed']), (1, 2))
        self.assertEqual(resolver.call_count, 2)
        self.assertEqual(Publication.objects.get().project_name, 'Canonical project')
        self.assertEqual(Author.objects.count(), 1)

    def test_bulk_unknown_project_cannot_be_accepted_using_a_supplied_name(self):
        records, failures = collect_records(data=[
            self.record(project_uuid=PROJECT, project_name='Invented'), self.record(title='Independent'),
        ])
        summary = ingest(records, failures, api_user=self.user, resolve_project_name=lambda uuid: None)
        self.assertEqual((summary['created'], summary['failed']), (1, 1))
        self.assertIn('project_uuid', summary['results'][0]['errors'][0])
        self.assertEqual(Publication.objects.get().title, 'Independent')

    def test_bulk_uuid_only_resolves_once_and_persists_the_canonical_name(self):
        records, failures = collect_records(data=[self.record(project_uuid=PROJECT)])
        resolver = mock.Mock(return_value='Canonical project')
        summary = ingest(records, failures, api_user=self.user, resolve_project_name=resolver)
        self.assertEqual(summary['created'], 1)
        resolver.assert_called_once_with(PROJECT)
        self.assertEqual(Publication.objects.get().project_name, 'Canonical project')

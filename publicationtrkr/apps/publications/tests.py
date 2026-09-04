"""
Tests for the publication builder (issue #24).

Covers the two things this refactor is actually for: the "manual input overrides
BibTeX" merge, which the API create path, the API update path and the web form each
used to implement separately, and the transaction around create, which is what stops
a rejected publication from leaving its Author rows behind. Production accumulated
19 such orphans before this existed, so the orphan test is the one that matters most.
"""

from io import StringIO
from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.db import IntegrityError
from django.http import QueryDict
from django.test import RequestFactory, SimpleTestCase, TestCase, override_settings
from django.utils import timezone
from django.utils.datastructures import MultiValueDict
from rest_framework.exceptions import PermissionDenied

from publicationtrkr.apps.apiuser.models import ApiUser, TaskTimeoutTracker
from publicationtrkr.apps.publications.api.serializers import PublicationSerializer
from publicationtrkr.apps.publications.api.viewsets import (
    PublicationViewSet,
    memoized_project_name_resolver,
)
from publicationtrkr.apps.publications.forms import PublicationForm
from publicationtrkr.apps.publications.models import Author, AuthorClaim, Publication
from publicationtrkr.apps.publications.utils.claim_scoring import (
    WEIGHT_NAME,
    candidates_for_author,
    score_pair,
)
from publicationtrkr.apps.publications.utils.name_matching import (
    FULL_VS_INITIAL,
    name_compatibility,
    parse_name,
)
from publicationtrkr.apps.publications.utils.bibtex_utils import (
    generate_bibtex,
    parse_bibtex,
    parse_bibtex_entries,
)
from publicationtrkr.apps.publications.utils.bulk_ingest import (
    BulkIngestError,
    collect_records,
    ingest,
)
from publicationtrkr.apps.publications.utils.claim_ledger import (
    ClaimDecisionError,
    approve_suggestion,
    record_admin_claim,
    record_self_claim,
    reject_suggestion,
)
from publicationtrkr.apps.publications.utils.publication_builder import (
    _sync_authors,
    create_publication,
    resolve_create_fields,
    resolve_update_fields,
    update_publication,
)

ARTICLE = """
@article{doe2024,
  title = {A Journal Paper},
  author = {Jane Doe and John Roe},
  year = {2024},
  journal = {Journal of Testing},
  url = {https://example.org/a}
}
"""

PROCEEDINGS = """
@inproceedings{roe2023,
  title = {A Conference Paper},
  author = {John Roe},
  year = {2023},
  booktitle = {Proceedings of Testing}
}
"""

PROJECT_A = 'f0e4a6c1-1111-4a2b-9c3d-000000000001'
PROJECT_B = 'f0e4a6c1-2222-4a2b-9c3d-000000000002'

NO_YEAR = """
@article{nobody,
  title = {Missing Its Year},
  author = {Nobody At All}
}
"""


class ParseBibtexTests(SimpleTestCase):
    """No database: parsing is a pure function over the uploaded text."""

    def test_fields_and_entry_type_are_extracted(self):
        parsed = parse_bibtex(ARTICLE)
        self.assertEqual(parsed['authors'], ['Jane Doe', 'John Roe'])
        self.assertEqual(parsed['title'], 'A Journal Paper')
        self.assertEqual(parsed['year'], '2024')
        self.assertEqual(parsed['venue'], 'Journal of Testing')
        self.assertEqual(parsed['link'], 'https://example.org/a')
        # entry_type is new: generate_bibtex used to emit @article for everything.
        self.assertEqual(parsed['entry_type'], 'article')

    def test_booktitle_becomes_venue_for_proceedings(self):
        parsed = parse_bibtex(PROCEEDINGS)
        self.assertEqual(parsed['entry_type'], 'inproceedings')
        self.assertEqual(parsed['venue'], 'Proceedings of Testing')

    def test_unreadable_input_yields_empty_fields_rather_than_raising(self):
        # Every caller feeds this user-supplied text and treats a failure as
        # "no defaults available", so it must never raise.
        for text in ('', 'not bibtex at all', '@article{'):
            parsed = parse_bibtex(text)
            self.assertIsNone(parsed['title'], msg=text)
            self.assertIsNone(parsed['authors'], msg=text)


class ParseBibtexEntriesTests(SimpleTestCase):
    """The multi-entry parser the bulk endpoint needs."""

    def test_every_entry_is_returned_in_source_order(self):
        # parse_bibtex reads entries[0] and discards the rest; a 200-entry upload
        # would have silently become one publication.
        entries, errors = parse_bibtex_entries(ARTICLE + PROCEEDINGS)
        self.assertEqual([e['title'] for e in entries], ['A Journal Paper', 'A Conference Paper'])
        self.assertEqual([e['index'] for e in entries], [0, 1])
        self.assertEqual(errors, [])

    def test_an_unusable_entry_is_reported_by_index_and_skipped(self):
        # The upload keeps going: 199 good entries are not lost to one bad one.
        entries, errors = parse_bibtex_entries(ARTICLE + NO_YEAR + PROCEEDINGS)
        self.assertEqual([e['title'] for e in entries], ['A Journal Paper', 'A Conference Paper'])
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0]['index'], 1)
        self.assertIn('year', errors[0]['error'])

    def test_a_document_with_no_entries_is_empty_not_an_error(self):
        self.assertEqual(parse_bibtex_entries('nothing here'), ([], []))


class GenerateBibtexTests(TestCase):
    """Author names come from the database, so these need one."""

    def make_publication(self, **overrides):
        fields = {
            'authors': [],
            'title': 'A Journal Paper',
            'uuid': 'pub-1',
            'venue': 'Journal of Testing',
            'year': '2024',
        }
        fields.update(overrides)
        publication = Publication.objects.create(**fields)
        Author.objects.create(
            author_name='Jane Doe', display_name='Jane Doe',
            publication_uuid=publication.uuid, uuid='author-1',
        )
        publication.authors = ['author-1']
        publication.save()
        return publication

    def test_default_is_still_an_article(self):
        publication = self.make_publication()
        bibtex = generate_bibtex(publication)
        self.assertTrue(bibtex.startswith('@article{doe2024,'))
        self.assertIn('  journal = {Journal of Testing}', bibtex)

    def test_entry_type_selects_the_venue_field(self):
        # An @inproceedings record's venue is a booktitle. Emitting 'journal = ...'
        # for it produces BibTeX that a reference manager reads back wrong.
        publication = self.make_publication()
        bibtex = generate_bibtex(publication, entry_type='inproceedings')
        self.assertTrue(bibtex.startswith('@inproceedings{doe2024,'))
        self.assertIn('  booktitle = {Journal of Testing}', bibtex)

    def test_entry_type_is_read_back_from_stored_bibtex(self):
        publication = self.make_publication(bibtex=PROCEEDINGS)
        self.assertTrue(generate_bibtex(publication).startswith('@inproceedings{'))


class ResolveFieldsTests(SimpleTestCase):
    """The merge that used to exist in three places."""

    def test_manual_input_overrides_bibtex(self):
        resolved = resolve_create_fields(
            {'title': 'Typed By Hand', 'authors': ['Someone Else']}, parse_bibtex(ARTICLE))
        self.assertEqual(resolved['title'], 'Typed By Hand')
        self.assertEqual(resolved['authors'], ['Someone Else'])

    def test_bibtex_fills_what_was_left_empty(self):
        resolved = resolve_create_fields({'title': ''}, parse_bibtex(ARTICLE))
        self.assertEqual(resolved['title'], 'A Journal Paper')
        self.assertEqual(resolved['venue'], 'Journal of Testing')
        self.assertEqual(resolved['year'], '2024')

    def test_create_reports_every_field_even_when_unset(self):
        resolved = resolve_create_fields({})
        self.assertEqual(resolved['authors'], [])
        self.assertIsNone(resolved['title'])
        self.assertIsNone(resolved['link'])

    def test_update_reports_only_the_fields_the_payload_carries(self):
        # Absent means "leave the stored value alone". If update returned None for
        # unmentioned fields, a PATCH of one field would blank out the rest.
        resolved = resolve_update_fields({'title': 'New Title'})
        self.assertEqual(resolved, {'title': 'New Title'})


class CreatePublicationTests(TestCase):

    def setUp(self):
        self.api_user = ApiUser.objects.create(uuid='user-1')

    def test_authors_are_created_and_linked(self):
        publication = create_publication(
            data={'title': 'A Paper', 'year': '2024', 'authors': ['Jane Doe', 'John Roe']},
            api_user=self.api_user)
        authors = Author.objects.filter(uuid__in=publication.authors)
        self.assertEqual(len(publication.authors), 2)
        self.assertEqual([a.author_name for a in authors.order_by('author_name')],
                         ['Jane Doe', 'John Roe'])
        self.assertEqual({a.publication_uuid for a in authors}, {publication.uuid})

    def test_bibtex_supplies_the_fields_that_were_not_given(self):
        publication = create_publication(data={'bibtex': ARTICLE}, api_user=self.api_user)
        self.assertEqual(publication.title, 'A Journal Paper')
        self.assertEqual(publication.year, '2024')
        self.assertEqual(publication.venue, 'Journal of Testing')
        self.assertEqual(publication.link, 'https://example.org/a')
        self.assertEqual(len(publication.authors), 2)

    def test_a_rejected_publication_leaves_no_orphan_authors(self):
        # This is the bug. create() saved every Author before publication.save(), so
        # a duplicate title returned 400 with the Author rows already committed and
        # no publication left to reference them -- 19 of them on production.
        create_publication(
            data={'title': 'A Paper', 'year': '2024', 'authors': ['Jane Doe']},
            api_user=self.api_user)
        self.assertEqual(Author.objects.count(), 1)

        with self.assertRaises(IntegrityError):
            create_publication(
                data={'title': 'A Paper', 'year': '2025', 'authors': ['John Roe', 'Ann Poe']},
                api_user=self.api_user)

        self.assertEqual(Publication.objects.count(), 1)
        self.assertEqual(Author.objects.count(), 1)
        self.assertEqual(Author.objects.get().author_name, 'Jane Doe')

    def test_project_name_is_resolved_only_when_it_is_missing(self):
        calls = []

        def resolver(project_uuid):
            calls.append(project_uuid)
            return 'Looked Up Project'

        given = create_publication(
            data={'title': 'One', 'year': '2024', 'authors': ['A B'],
                  'project_uuid': 'p-1', 'project_name': 'Already Known'},
            api_user=self.api_user, resolve_project_name=resolver)
        self.assertEqual(given.project_name, 'Already Known')
        self.assertEqual(calls, [])

        looked_up = create_publication(
            data={'title': 'Two', 'year': '2024', 'authors': ['A B'], 'project_uuid': 'p-1'},
            api_user=self.api_user, resolve_project_name=resolver)
        self.assertEqual(looked_up.project_name, 'Looked Up Project')
        self.assertEqual(calls, ['p-1'])


class UpdatePublicationTests(TestCase):

    def setUp(self):
        self.api_user = ApiUser.objects.create(uuid='user-1')
        self.publication = create_publication(
            data={'title': 'A Paper', 'year': '2024', 'venue': 'Journal of Testing',
                  'authors': ['Jane Doe', 'John Roe']},
            api_user=self.api_user)

    def test_fields_the_payload_omits_are_left_alone(self):
        updated = update_publication(
            publication=self.publication, data={'title': 'A Better Paper'}, api_user=self.api_user)
        self.assertEqual(updated.title, 'A Better Paper')
        self.assertEqual(updated.year, '2024')
        self.assertEqual(updated.venue, 'Journal of Testing')
        self.assertEqual(len(updated.authors), 2)

    def test_renaming_one_author_keeps_the_others_claim(self):
        # Author rows are matched by position. Recreating them all on a rename would
        # drop fabric_uuid, silently unclaiming someone else's publication.
        claimed = Author.objects.get(uuid=self.publication.authors[1])
        claimed.fabric_uuid = 'user-1'
        claimed.display_name = 'J. Roe'
        claimed.save()

        updated = update_publication(
            publication=self.publication,
            data={'authors': ['Jane M. Doe', 'John Roe']}, api_user=self.api_user)

        self.assertEqual(updated.authors[1], claimed.uuid)
        claimed.refresh_from_db()
        self.assertEqual(claimed.fabric_uuid, 'user-1')
        self.assertEqual(claimed.display_name, 'J. Roe')
        self.assertEqual(Author.objects.get(uuid=updated.authors[0]).author_name, 'Jane M. Doe')

    def test_a_shorter_author_list_deletes_the_surplus_rows(self):
        surplus_uuid = self.publication.authors[1]
        updated = update_publication(
            publication=self.publication, data={'authors': ['Jane Doe']}, api_user=self.api_user)
        self.assertEqual(len(updated.authors), 1)
        self.assertFalse(Author.objects.filter(uuid=surplus_uuid).exists())
        self.assertEqual(Author.objects.count(), 1)


class PublicationFormTests(TestCase):
    """
    The form and the API must resolve a payload the same way; they did not before
    this refactor -- the form ignored the BibTeX venue that the API took.
    """

    def form_data(self, **overrides):
        data = {'bibtex': '', 'title': '', 'authors': '', 'link': '', 'year': '',
                'venue': '', 'project_name': '', 'project_uuid': ''}
        data.update(overrides)
        return data

    def test_bibtex_alone_is_enough(self):
        form = PublicationForm(data=self.form_data(bibtex=ARTICLE))
        self.assertTrue(form.is_valid(), msg=form.errors.as_text())
        self.assertEqual(form.cleaned_data['title'], 'A Journal Paper')
        self.assertEqual(form.cleaned_data['authors'], ['Jane Doe', 'John Roe'])
        self.assertEqual(form.cleaned_data['year'], '2024')
        self.assertEqual(form.cleaned_data['link'], 'https://example.org/a')

    def test_typed_fields_override_bibtex(self):
        form = PublicationForm(data=self.form_data(
            bibtex=ARTICLE, title='Typed By Hand', authors='Someone Else', year='2020'))
        self.assertTrue(form.is_valid(), msg=form.errors.as_text())
        self.assertEqual(form.cleaned_data['title'], 'Typed By Hand')
        self.assertEqual(form.cleaned_data['authors'], ['Someone Else'])
        self.assertEqual(form.cleaned_data['year'], '2020')

    def test_missing_required_fields_are_reported(self):
        form = PublicationForm(data=self.form_data(title='Only A Title'))
        self.assertFalse(form.is_valid())
        errors = form.errors.as_text()
        self.assertIn('Authors', errors)
        self.assertIn('Year', errors)

    def test_a_non_http_link_is_rejected(self):
        form = PublicationForm(data=self.form_data(
            title='A Paper', authors='Jane Doe', year='2024', link='javascript:alert(1)'))
        self.assertFalse(form.is_valid())
        self.assertIn('Link', form.errors.as_text())


class ViewSetWiringTests(TestCase):
    """
    The viewset calling convention the web form depends on. publications/views.py
    builds a QueryDict, attaches it as request.data and calls
    PublicationViewSet(request=request).create(...) in process, bypassing DRF
    dispatch -- so nothing about that path is covered by an HTTP-level test.
    """

    def setUp(self):
        self.factory = RequestFactory()
        self.api_user = ApiUser.objects.create(uuid='user-1')

    def call(self, method, data, **kwargs):
        request = self.factory.post('/publications/create')
        request.data = QueryDict('', mutable=True)
        request.data.update(data)
        # The admin properties read a role name out of the environment, so they are
        # patched rather than granted through fabric_roles: what is under test here is
        # the wiring, not who is allowed through it.
        with mock.patch('publicationtrkr.apps.publications.api.viewsets.get_api_user',
                        return_value=self.api_user), \
                mock.patch.object(ApiUser, 'is_publication_tracker_admin', True):
            return method(PublicationViewSet(request=request), request=request, **kwargs)

    def test_create_returns_201_and_persists_the_publication(self):
        response = self.call(PublicationViewSet.create,
                             {'title': 'A Paper', 'year': '2024', 'authors': ['Jane Doe']})
        self.assertEqual(response.status_code, 201)
        self.assertEqual(Publication.objects.get().title, 'A Paper')
        self.assertEqual(Author.objects.get().author_name, 'Jane Doe')

    def test_a_duplicate_title_is_a_400_with_no_orphans_left_behind(self):
        # The end-to-end shape of the orphan bug: the caller still gets its 400, but
        # the Author rows the rejected publication would have referenced are gone.
        self.call(PublicationViewSet.create,
                  {'title': 'A Paper', 'year': '2024', 'authors': ['Jane Doe']})
        response = self.call(PublicationViewSet.create,
                             {'title': 'A Paper', 'year': '2025', 'authors': ['John Roe', 'Ann Poe']})
        self.assertEqual(response.status_code, 400)
        self.assertIn('UniqueConstraint', response.data)
        self.assertEqual(Publication.objects.count(), 1)
        self.assertEqual(Author.objects.count(), 1)

    def test_update_returns_200_and_applies_only_what_was_sent(self):
        self.call(PublicationViewSet.create,
                  {'title': 'A Paper', 'year': '2024', 'authors': ['Jane Doe']})
        publication = Publication.objects.get()
        response = self.call(PublicationViewSet.update, {'title': 'A Better Paper'},
                             uuid=publication.uuid)
        self.assertEqual(response.status_code, 200)
        publication.refresh_from_db()
        self.assertEqual(publication.title, 'A Better Paper')
        self.assertEqual(publication.year, '2024')


JSONL_GOOD = (
    '{"title": "Line One", "authors": ["Jane Doe"], "year": "2024"}\n'
    '\n'
    '{"title": "Line Two", "authors": ["John Roe"], "year": "2025"}\n'
)


def upload(name, text):
    return SimpleUploadedFile(name, text.encode('utf-8'), content_type='application/octet-stream')


class CollectRecordsTests(SimpleTestCase):
    """
    Everything that decides what will be written, before anything is. No database:
    a request refused here never reaches one.
    """

    def test_jsonl_reports_bad_lines_by_line_number_and_keeps_the_rest(self):
        text = JSONL_GOOD + 'not json at all\n' + '["a list, not an object"]\n'
        records, failures = collect_records(upload=upload('p.jsonl', text))
        self.assertEqual([r['title'] for r in records], ['Line One', 'Line Two'])
        # Blank lines do not consume a position, but the reported line numbers are the
        # file's own -- that is the number someone needs to fix the file.
        self.assertEqual([r['line'] for r in records], [1, 3])
        self.assertEqual([f['line'] for f in failures], [4, 5])

    def test_bibtex_upload_becomes_one_record_per_entry(self):
        records, failures = collect_records(upload=upload('p.bib', ARTICLE + PROCEEDINGS + NO_YEAR))
        self.assertEqual([r['title'] for r in records], ['A Journal Paper', 'A Conference Paper'])
        self.assertEqual([f['index'] for f in failures], [2])
        # The entry is carried through verbatim so the stored record keeps the fields
        # this app does not model, and the @type it was uploaded as.
        self.assertIn('@inproceedings{', records[1]['bibtex'])

    def test_a_json_body_is_accepted_as_a_list_or_under_publications(self):
        payload = [{'title': 'One'}, {'title': 'Two'}]
        bare, _ = collect_records(data=payload)
        wrapped, _ = collect_records(data={'publications': payload})
        self.assertEqual([r['title'] for r in bare], ['One', 'Two'])
        self.assertEqual([r['index'] for r in wrapped], [0, 1])

    def test_a_json_body_of_the_wrong_shape_is_refused_outright(self):
        with self.assertRaises(BulkIngestError):
            collect_records(data={'not_publications': []})

    def test_an_unsupported_file_type_is_refused_before_it_is_parsed(self):
        with self.assertRaises(BulkIngestError) as caught:
            collect_records(upload=upload('publications.csv', 'title,year\n'))
        self.assertIn('unsupported file type', str(caught.exception))

    @override_settings(BULK_MAX_RECORDS=1)
    def test_over_the_record_cap_is_refused_before_any_write(self):
        with self.assertRaises(BulkIngestError) as caught:
            collect_records(upload=upload('p.jsonl', JSONL_GOOD))
        self.assertIn('over the limit of 1', str(caught.exception))

    @override_settings(BULK_MAX_UPLOAD_BYTES=10)
    def test_over_the_size_cap_is_refused_before_it_is_read(self):
        # FILE_UPLOAD_MAX_MEMORY_SIZE only decides when a part spills to disk and
        # DATA_UPLOAD_MAX_MEMORY_SIZE does not cover multipart at all, so nothing
        # refuses an oversized upload unless this does.
        with self.assertRaises(BulkIngestError) as caught:
            collect_records(upload=upload('p.jsonl', JSONL_GOOD))
        self.assertIn('over the', str(caught.exception))


class IngestTests(TestCase):

    def setUp(self):
        self.api_user = ApiUser.objects.create(uuid='user-1')

    def ingest_text(self, name, text):
        records, failures = collect_records(upload=upload(name, text))
        return ingest(records, failures, api_user=self.api_user)

    def test_every_record_is_reported_on_by_position(self):
        summary = self.ingest_text('p.jsonl', JSONL_GOOD + 'not json\n')
        self.assertEqual((summary['created'], summary['skipped'], summary['failed']), (2, 0, 1))
        self.assertEqual([r['line'] for r in summary['results']], [1, 3, 4])
        self.assertEqual([r['status'] for r in summary['results']],
                         ['created', 'created', 'failed'])

    def test_a_record_missing_a_required_field_fails_without_stopping_the_batch(self):
        text = ('{"title": "Has Everything", "authors": ["Jane Doe"], "year": "2024"}\n'
                '{"title": "No Year Here", "authors": ["John Roe"]}\n')
        summary = self.ingest_text('p.jsonl', text)
        self.assertEqual((summary['created'], summary['failed']), (1, 1))
        self.assertEqual(summary['results'][1]['errors'], [{'year': 'must provide a year'}])
        self.assertEqual(Publication.objects.count(), 1)

    def test_a_duplicate_is_skipped_and_leaves_no_orphan_authors(self):
        # Re-uploading a file that overlaps what is stored is the ordinary case. The
        # duplicate must not take the batch down, and -- this is the whole point of the
        # transaction in create_publication -- must not leave its Author rows behind.
        self.ingest_text('p.jsonl', JSONL_GOOD)
        summary = self.ingest_text('p.jsonl', JSONL_GOOD + '{"title": "Line Three", "authors": ["Ann Poe"], "year": "2026"}\n')

        self.assertEqual((summary['created'], summary['skipped']), (1, 2))
        # The reason is one line. Postgres appends a DETAIL line naming the conflicting
        # values, which made every skipped record a multi-line string in the report.
        reasons = [r['reason'] for r in summary['results'] if r['status'] == 'skipped']
        self.assertTrue(all('\n' not in reason for reason in reasons), reasons)
        self.assertTrue(all(reason.startswith('duplicate key value') for reason in reasons), reasons)
        self.assertEqual(Publication.objects.count(), 3)
        stored = {uuid for pub in Publication.objects.all() for uuid in pub.authors}
        self.assertEqual(Author.objects.exclude(uuid__in=stored).count(), 0)
        self.assertEqual(Author.objects.count(), 3)

    def test_a_bibtex_upload_keeps_its_entry_type(self):
        summary = self.ingest_text('p.bib', PROCEEDINGS)
        self.assertEqual(summary['created'], 1)
        publication = Publication.objects.get()
        self.assertIn('@inproceedings{', publication.bibtex)
        self.assertEqual(PublicationSerializer(instance=publication).data['bibtex'],
                         publication.bibtex)

    def test_the_project_lookup_is_made_once_per_distinct_project(self):
        # An unmemoised lookup is one core-api round trip per record. At the record cap
        # that is 1000 of them against a 60s nginx read timeout.
        calls = []
        text = ''.join(
            '{{"title": "Paper {0}", "authors": ["Jane Doe"], "year": "2024", '
            '"project_uuid": "{1}"}}\n'.format(i, PROJECT_A if i % 2 else PROJECT_B)
            for i in range(6)
        )
        records, failures = collect_records(upload=upload('p.jsonl', text))
        ingest(records, failures, api_user=self.api_user,
               resolve_project_name=lambda project_uuid: calls.append(project_uuid) or 'A Project')
        # ingest passes the resolver through per record and does no caching of its
        # own: six records naming two projects are six calls.
        self.assertEqual(len(calls), 6)

        # The memo is what collapses them, and it is what the endpoint passes in.

        calls.clear()
        with mock.patch('publicationtrkr.apps.publications.api.viewsets.get_project_name_from_uuid',
                        side_effect=lambda request, project_uuid, api_user: calls.append(project_uuid)):
            resolver = memoized_project_name_resolver(None, self.api_user)
            for _ in range(3):
                resolver(PROJECT_A)
                resolver(PROJECT_B)
        self.assertEqual(calls, [PROJECT_A, PROJECT_B])


class BulkEndpointTests(TestCase):
    """The endpoint itself, including the in-process call the upload page makes."""

    def setUp(self):
        self.factory = RequestFactory()
        self.api_user = ApiUser.objects.create(uuid='user-1')

    def call(self, *, files=None, data=None, is_admin=True):
        request = self.factory.post('/api/publications/bulk')
        request.data = data if data is not None else {}
        if files:
            request._files = MultiValueDict({'file': [files]})
        with mock.patch('publicationtrkr.apps.publications.api.viewsets.get_api_user',
                        return_value=self.api_user), \
                mock.patch.object(ApiUser, 'is_publication_tracker_admin', is_admin):
            return PublicationViewSet(request=request).bulk(request=request)

    def test_a_non_admin_is_refused(self):
        # permission_classes covers the HTTP path; this covers the in-process one,
        # which never reaches DRF dispatch.
        with self.assertRaises(PermissionDenied):
            self.call(files=upload('p.jsonl', JSONL_GOOD), is_admin=False)
        self.assertEqual(Publication.objects.count(), 0)

    def test_a_jsonl_upload_creates_and_reports(self):
        response = self.call(files=upload('p.jsonl', JSONL_GOOD))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['created'], 2)
        self.assertEqual(Publication.objects.count(), 2)

    def test_a_json_body_creates_and_reports(self):
        response = self.call(data={'publications': [
            {'title': 'One', 'authors': ['Jane Doe'], 'year': '2024'},
        ]})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['created'], 1)

    @override_settings(BULK_MAX_RECORDS=1)
    def test_over_the_cap_is_a_400_with_nothing_written(self):
        response = self.call(files=upload('p.jsonl', JSONL_GOOD))
        self.assertEqual(response.status_code, 400)
        self.assertIn('BulkIngestError', response.data)
        self.assertEqual(Publication.objects.count(), 0)
        self.assertEqual(Author.objects.count(), 0)


class BulkUploadPageTests(TestCase):
    """
    The /publications/bulk-upload page end to end, through the real URL conf and the
    real template. Nothing else renders this template, and a template error only
    happens at render time -- an endpoint test would not catch one.
    """

    def setUp(self):
        self.api_user = ApiUser.objects.create(uuid='user-1')

    def as_user(self, is_admin=True):
        return (
            mock.patch('publicationtrkr.apps.publications.views.get_api_user',
                       return_value=self.api_user),
            mock.patch('publicationtrkr.apps.publications.api.viewsets.get_api_user',
                       return_value=self.api_user),
            mock.patch.object(ApiUser, 'is_publication_tracker_admin', is_admin),
        )

    def get(self, is_admin=True):
        patches = self.as_user(is_admin)
        with patches[0], patches[1], patches[2]:
            return self.client.get('/publications/bulk-upload')

    def post(self, file_obj=None, is_admin=True):
        patches = self.as_user(is_admin)
        data = {'file': file_obj} if file_obj is not None else {}
        with patches[0], patches[1], patches[2]:
            return self.client.post('/publications/bulk-upload', data)

    def test_the_page_renders_for_an_admin(self):
        response = self.get()
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Bulk Upload')
        self.assertContains(response, 'csrfmiddlewaretoken')

    def test_a_non_admin_sees_no_form(self):
        response = self.get(is_admin=False)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'not able to bulk upload')
        self.assertNotContains(response, 'csrfmiddlewaretoken')

    def test_uploading_a_file_creates_publications_and_renders_the_report(self):
        response = self.post(upload('p.jsonl', JSONL_GOOD))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Publication.objects.count(), 2)
        self.assertContains(response, 'created')
        self.assertContains(response, 'line 1')

    def test_a_failed_record_is_rendered_with_its_error(self):
        response = self.post(upload('p.jsonl', '{"title": "No Year", "authors": ["A B"]}\n'))
        self.assertContains(response, 'failed')
        self.assertContains(response, 'must provide a year')

    def test_posting_without_a_file_says_so_rather_than_failing(self):
        response = self.post()
        self.assertContains(response, 'Choose a .jsonl or .bib file')

    def test_a_non_admin_post_writes_nothing(self):
        response = self.post(upload('p.jsonl', JSONL_GOOD), is_admin=False)
        self.assertContains(response, 'PermissionDenied')
        self.assertEqual(Publication.objects.count(), 0)


# ---------------------------------------------------------------------------
# Author-claim scoring (issue #32, v1.14.0)
# ---------------------------------------------------------------------------


class NameMatchingTests(SimpleTestCase):
    """
    The ambiguity this exists to score, using the issue's own examples.

    Surname agreement is the gate that keeps the queue finite, so the tests that matter
    most are the ones asserting a non-match: without them every author pairs with every
    one of 3,300 users.
    """

    def test_both_name_orders_parse_to_the_same_person(self):
        self.assertEqual(parse_name('Smith, Jane'), parse_name('Jane Smith'))

    def test_surname_particles_stay_with_the_surname_in_either_order(self):
        self.assertEqual(
            parse_name('van der Berg, Anna')['surname'],
            parse_name('Anna van der Berg')['surname'],
        )

    def test_diacritics_and_case_fold(self):
        score, _, matched = name_compatibility('Muñoz, José', 'Jose Munoz')
        self.assertTrue(matched)
        self.assertEqual(score, 1.0)

    def test_suffix_is_not_mistaken_for_a_name(self):
        self.assertEqual(parse_name('Smith, Jane Q., Jr.')['given'], 'jane')

    def test_initial_is_consistent_with_a_full_given_name(self):
        score, detail, matched = name_compatibility('Smith, J.', 'Jane Smith')
        self.assertTrue(matched)
        self.assertEqual(score, FULL_VS_INITIAL)
        self.assertIn('jane', detail)

    def test_conflicting_given_names_score_zero_but_still_match_on_surname(self):
        # Deliberately still a candidate: the paper's author string may be wrong, and the
        # admin sees the conflict spelled out. It just must not outrank a real match.
        score, detail, matched = name_compatibility('Smith, Jane', 'John Smith')
        self.assertTrue(matched)
        self.assertEqual(score, 0.0)
        self.assertIn('differ', detail)

    def test_different_surnames_are_not_candidates_at_all(self):
        _, _, matched = name_compatibility('Smith, J.', 'Jane Okonkwo')
        self.assertFalse(matched)

    def test_surname_component_match_is_whole_component_only(self):
        # 'son' must not match 'johnson', or every short surname matches half the world.
        _, _, matched = name_compatibility('Son, A.', 'Alice Johnson')
        self.assertFalse(matched)

    def test_hyphenated_surname_matches_either_component(self):
        _, _, matched = name_compatibility('Smith-Okonkwo, J.', 'Jane Smith')
        self.assertTrue(matched)


class ClaimScoringTests(SimpleTestCase):
    """The weighting, exercised without a database."""

    class _User:
        def __init__(self, name, projects, uuid='u-1'):
            self.name = name
            self.projects = projects
            self.uuid = uuid

    class _Author:
        def __init__(self, author_name):
            self.author_name = author_name

    class _Publication:
        def __init__(self, project_uuid):
            self.project_uuid = project_uuid

    def test_exact_name_in_the_same_project_is_full_confidence(self):
        result = score_pair(
            self._Author('Smith, Jane'),
            self._User('Jane Smith', ['p-1']),
            self._Publication('p-1'),
        )
        self.assertEqual(result['score'], 1.0)

    def test_project_membership_outweighs_a_bare_initial(self):
        with_project = score_pair(
            self._Author('Smith, J.'), self._User('Jane Smith', ['p-1']),
            self._Publication('p-1'))
        without = score_pair(
            self._Author('Smith, J.'), self._User('Jane Smith', ['p-2']),
            self._Publication('p-1'))
        self.assertGreater(with_project['score'], without['score'])

    def test_a_publication_with_no_project_is_no_signal_not_a_penalty(self):
        result = score_pair(
            self._Author('Smith, Jane'), self._User('Jane Smith', []), None)
        self.assertEqual(result['signals']['project']['value'], 0.0)
        self.assertIn('no project', result['signals']['project']['detail'])
        # The name still carries its full weight.
        self.assertEqual(result['score'], WEIGHT_NAME)

    def test_a_different_surname_is_not_scored_at_all(self):
        self.assertIsNone(score_pair(
            self._Author('Smith, J.'), self._User('Jane Okonkwo', ['p-1']),
            self._Publication('p-1')))

    def test_every_suggestion_carries_a_reason_for_each_signal(self):
        # A score with no breakdown is unreviewable, which is the whole design.
        result = score_pair(
            self._Author('Smith, J.'), self._User('Jane Smith', ['p-1']),
            self._Publication('p-1'))
        for signal in ('project', 'name'):
            self.assertTrue(result['signals'][signal]['detail'])

    def test_candidates_come_back_best_first(self):
        author = self._Author('Smith, J.')
        publication = self._Publication('p-1')
        users = [
            self._User('Jane Smith', [], uuid='no-project'),
            self._User('Jane Smith', ['p-1'], uuid='in-project'),
        ]
        ranked = candidates_for_author(author, users, publication)
        self.assertEqual([u.uuid for _, u, _ in ranked], ['in-project', 'no-project'])


class ScoreAuthorClaimsCommandTests(TestCase):
    """
    The command's contract: it suggests, and it never decides.
    """

    def setUp(self):
        self.publication = Publication.objects.create(
            authors=['Smith, Jane'], project_uuid='p-1', title='A paper',
            uuid='pub-1', year='2026',
        )
        self.author = Author.objects.create(
            author_name='Smith, Jane', display_name='Smith, Jane',
            publication_uuid='pub-1', uuid='auth-1',
        )
        self.api_user = ApiUser.objects.create(
            uuid='user-1', name='Jane Smith', projects=['p-1'], active=True)

    def _run(self, *args):
        out = StringIO()
        call_command('score_author_claims', *args, stdout=out)
        return out.getvalue()

    def test_a_dry_run_writes_nothing(self):
        output = self._run('--dry-run')
        self.assertIn('DRY RUN', output)
        self.assertEqual(AuthorClaim.objects.count(), 0)

    def test_scoring_creates_a_suggestion_and_leaves_attribution_alone(self):
        self._run()
        claim = AuthorClaim.objects.get()
        self.assertEqual(claim.status, AuthorClaim.SUGGESTED)
        self.assertEqual(claim.source, AuthorClaim.MACHINE)
        self.assertEqual(claim.score, 1.0)
        self.author.refresh_from_db()
        self.assertIsNone(self.author.fabric_uuid)

    def test_rerunning_updates_in_place_rather_than_duplicating(self):
        self._run()
        self._run()
        self.assertEqual(AuthorClaim.objects.count(), 1)

    def test_a_rejected_pair_is_never_re_suggested(self):
        AuthorClaim.objects.create(
            author=self.author, api_user=self.api_user, score=0.0, signals={},
            source=AuthorClaim.MACHINE, status=AuthorClaim.REJECTED, uuid='claim-r',
        )
        output = self._run()
        claim = AuthorClaim.objects.get()
        self.assertEqual(claim.status, AuthorClaim.REJECTED)
        self.assertEqual(claim.score, 0.0)
        self.assertIn('Left decided       : 1', output)

    def test_an_approved_pair_is_left_exactly_as_it_is(self):
        AuthorClaim.objects.create(
            author=self.author, api_user=self.api_user, score=0.9, signals={},
            source=AuthorClaim.MACHINE, status=AuthorClaim.APPROVED, uuid='claim-a',
        )
        self._run()
        claim = AuthorClaim.objects.get()
        self.assertEqual(claim.status, AuthorClaim.APPROVED)
        self.assertEqual(claim.score, 0.9)

    def test_claimed_authors_are_skipped_unless_full(self):
        self.author.fabric_uuid = 'user-1'
        self.author.save()
        self._run()
        self.assertEqual(AuthorClaim.objects.count(), 0)
        self._run('--full')
        self.assertEqual(AuthorClaim.objects.count(), 1)

    def test_inactive_users_are_not_suggested(self):
        self.api_user.active = False
        self.api_user.save()
        self._run()
        self.assertEqual(AuthorClaim.objects.count(), 0)

    def test_a_stale_suggestion_is_withdrawn_when_it_stops_scoring(self):
        self._run()
        self.assertEqual(AuthorClaim.objects.count(), 1)
        # A rename through the API path should not leave the old spelling's suggestions.
        self.author.author_name = 'Okonkwo, Jane'
        self.author.save()
        output = self._run()
        self.assertEqual(AuthorClaim.objects.count(), 0)
        self.assertIn('Withdrawn (stale)  : 1', output)

    def test_if_due_is_a_noop_before_the_cadence_elapses(self):
        TaskTimeoutTracker.objects.create(
            description='Author Claim Scoring Check', last_updated=timezone.now(),
            name='claim_scoring_check', timeout_in_seconds=86400,
            uuid='trk-1', value=None,
        )
        output = self._run('--if-due')
        self.assertIn('Not due', output)
        self.assertEqual(AuthorClaim.objects.count(), 0)


# ---------------------------------------------------------------------------
# The claim ledger and the admin queue (issue #32, v1.14.0)
# ---------------------------------------------------------------------------


class ClaimLedgerTests(TestCase):
    """
    The rules every decision path has to agree on: attribution is written once, other
    standing suggestions go, and no decision is ever removed by code.
    """

    def setUp(self):
        self.author = Author.objects.create(
            author_name='Smith, Jane', display_name='Smith, Jane',
            publication_uuid='pub-1', uuid='auth-1',
        )
        self.jane = ApiUser.objects.create(uuid='user-1', name='Jane Smith')
        self.other = ApiUser.objects.create(uuid='user-2', name='John Smith')
        self.admin = ApiUser.objects.create(uuid='admin-1', name='An Admin')
        self.claim = AuthorClaim.objects.create(
            author=self.author, api_user=self.jane, score=1.0, uuid='claim-1',
            signals={'name': {'weight': 0.45, 'value': 1.0, 'detail': 'exact'}},
        )
        self.runner_up = AuthorClaim.objects.create(
            author=self.author, api_user=self.other, score=0.55, uuid='claim-2',
        )

    def test_approving_writes_attribution_and_stamps_the_decision(self):
        approve_suggestion(self.claim, decided_by=self.admin)
        self.author.refresh_from_db()
        self.claim.refresh_from_db()
        self.assertEqual(self.author.fabric_uuid, 'user-1')
        self.assertEqual(self.claim.status, AuthorClaim.APPROVED)
        self.assertEqual(self.claim.decided_by, self.admin)
        self.assertIsNotNone(self.claim.decided_at)

    def test_approving_withdraws_the_other_suggestions_for_that_author(self):
        withdrawn = approve_suggestion(self.claim, decided_by=self.admin)
        self.assertEqual(withdrawn, 1)
        self.assertFalse(AuthorClaim.objects.filter(uuid='claim-2').exists())
        self.assertTrue(AuthorClaim.objects.filter(uuid='claim-1').exists())

    def test_approving_keeps_the_signals_that_justified_it(self):
        approve_suggestion(self.claim, decided_by=self.admin)
        self.claim.refresh_from_db()
        self.assertEqual(self.claim.signals['name']['value'], 1.0)

    def test_an_author_claimed_by_someone_else_is_refused_not_overwritten(self):
        self.author.fabric_uuid = 'user-99'
        self.author.save()
        with self.assertRaises(ClaimDecisionError):
            approve_suggestion(self.claim, decided_by=self.admin)
        self.author.refresh_from_db()
        self.assertEqual(self.author.fabric_uuid, 'user-99')
        self.claim.refresh_from_db()
        self.assertEqual(self.claim.status, AuthorClaim.SUGGESTED)

    def test_approving_an_already_decided_claim_is_refused(self):
        approve_suggestion(self.claim, decided_by=self.admin)
        with self.assertRaises(ClaimDecisionError):
            approve_suggestion(self.claim, decided_by=self.admin)

    def test_rejecting_keeps_the_row_and_leaves_attribution_alone(self):
        reject_suggestion(self.claim, decided_by=self.admin)
        self.claim.refresh_from_db()
        self.assertEqual(self.claim.status, AuthorClaim.REJECTED)
        self.author.refresh_from_db()
        self.assertIsNone(self.author.fabric_uuid)

    def test_rejecting_leaves_the_other_suggestions_in_the_queue(self):
        reject_suggestion(self.claim, decided_by=self.admin)
        self.assertTrue(AuthorClaim.objects.filter(uuid='claim-2').exists())

    def test_withdrawal_never_touches_a_decided_row(self):
        reject_suggestion(self.runner_up, decided_by=self.admin)
        approve_suggestion(self.claim, decided_by=self.admin)
        self.runner_up.refresh_from_db()
        self.assertEqual(self.runner_up.status, AuthorClaim.REJECTED)

    def test_a_self_claim_promotes_the_standing_suggestion_for_that_pair(self):
        record_self_claim(self.author, self.jane)
        self.claim.refresh_from_db()
        self.assertEqual(self.claim.status, AuthorClaim.SELF_ASSERTED)
        self.assertEqual(self.claim.source, AuthorClaim.SELF)
        self.assertEqual(self.claim.decided_by, self.jane)
        # ...and the pair it was competing with is withdrawn.
        self.assertFalse(AuthorClaim.objects.filter(uuid='claim-2').exists())

    def test_a_self_claim_with_no_suggestion_records_one_anyway(self):
        author = Author.objects.create(
            author_name='Nobody, N', display_name='Nobody, N',
            publication_uuid='pub-1', uuid='auth-2',
        )
        claim = record_self_claim(author, self.jane)
        self.assertEqual(claim.status, AuthorClaim.SELF_ASSERTED)
        self.assertIsNotNone(claim.uuid)

    def test_an_admin_entered_uuid_is_recorded_as_an_admin_approval(self):
        claim = record_admin_claim(self.author, 'user-2', decided_by=self.admin)
        self.assertEqual(claim.status, AuthorClaim.APPROVED)
        self.assertEqual(claim.source, AuthorClaim.ADMIN)
        self.assertEqual(claim.decided_by, self.admin)

    def test_an_admin_entered_uuid_naming_no_api_user_records_nothing(self):
        self.assertIsNone(record_admin_claim(self.author, 'not-a-user', decided_by=self.admin))
        self.assertEqual(AuthorClaim.objects.filter(status=AuthorClaim.APPROVED).count(), 0)

    def test_clearing_an_attribution_leaves_the_previous_decision_in_place(self):
        record_admin_claim(self.author, 'user-1', decided_by=self.admin)
        self.assertIsNone(record_admin_claim(self.author, '', decided_by=self.admin))
        self.claim.refresh_from_db()
        self.assertEqual(self.claim.status, AuthorClaim.APPROVED)

    def test_a_rename_through_the_api_path_withdraws_the_suggestions(self):
        publication = Publication.objects.create(
            authors=[self.author.uuid], title='A paper', uuid='pub-1', year='2026',
        )
        _sync_authors(publication, ['Okonkwo, Jane'])
        self.author.refresh_from_db()
        self.assertEqual(self.author.author_name, 'Okonkwo, Jane')
        self.assertEqual(AuthorClaim.objects.filter(author=self.author).count(), 0)

    def test_a_rename_leaves_a_decided_row_alone(self):
        reject_suggestion(self.claim, decided_by=self.admin)
        publication = Publication.objects.create(
            authors=[self.author.uuid], title='A paper', uuid='pub-1', year='2026',
        )
        _sync_authors(publication, ['Okonkwo, Jane'])
        self.claim.refresh_from_db()
        self.assertEqual(self.claim.status, AuthorClaim.REJECTED)


class AuthorClaimQueuePageTests(TestCase):
    """
    /publications/authors/claims end to end, through the real URL conf and the real
    template. Nothing else renders this template, and a template error only happens at
    render time.
    """

    def setUp(self):
        self.api_user = ApiUser.objects.create(uuid='admin-1', name='An Admin')
        self.candidate = ApiUser.objects.create(
            uuid='user-1', name='Jane Smith', email='jane@example.edu',
            affiliation='Example University',
        )
        self.publication = Publication.objects.create(
            authors=['auth-1'], project_name='A Project', project_uuid='p-1',
            title='A paper', uuid='pub-1', year='2026',
        )
        self.author = Author.objects.create(
            author_name='Smith, Jane', display_name='Smith, Jane',
            publication_uuid='pub-1', uuid='auth-1',
        )
        self.claim = AuthorClaim.objects.create(
            author=self.author, api_user=self.candidate, score=1.0, uuid='claim-1',
            signals={'project': {'weight': 0.55, 'value': 1.0,
                                 'detail': 'member of the publication project p-1'}},
        )

    def as_user(self, is_admin=True):
        return (
            mock.patch('publicationtrkr.apps.publications.views.get_api_user',
                       return_value=self.api_user),
            mock.patch.object(ApiUser, 'is_publication_tracker_admin', is_admin),
        )

    def get(self, query='', is_admin=True):
        patches = self.as_user(is_admin)
        with patches[0], patches[1]:
            return self.client.get('/publications/authors/claims' + query)

    def post(self, data, is_admin=True):
        patches = self.as_user(is_admin)
        with patches[0], patches[1]:
            return self.client.post('/publications/authors/claims', data)

    def test_the_queue_renders_a_suggestion_with_its_reasons(self):
        response = self.get()
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Smith, Jane')
        self.assertContains(response, 'Jane Smith')
        self.assertContains(response, 'member of the publication project p-1')
        self.assertContains(response, 'A paper')
        self.assertContains(response, 'csrfmiddlewaretoken')

    def test_a_non_admin_sees_no_claims_and_no_buttons(self):
        response = self.get(is_admin=False)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'PermissionDenied')
        self.assertNotContains(response, 'csrfmiddlewaretoken')
        self.assertNotContains(response, 'Jane Smith')

    def test_approving_from_the_page_writes_the_attribution(self):
        response = self.post({'claim_uuid': 'claim-1', 'action': 'approve'})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Approved')
        self.author.refresh_from_db()
        self.assertEqual(self.author.fabric_uuid, 'user-1')

    def test_rejecting_from_the_page_records_it_and_changes_no_attribution(self):
        response = self.post({'claim_uuid': 'claim-1', 'action': 'reject'})
        self.assertContains(response, 'Rejected')
        self.claim.refresh_from_db()
        self.assertEqual(self.claim.status, AuthorClaim.REJECTED)
        self.author.refresh_from_db()
        self.assertIsNone(self.author.fabric_uuid)

    def test_a_non_admin_post_writes_nothing(self):
        response = self.post({'claim_uuid': 'claim-1', 'action': 'approve'}, is_admin=False)
        self.assertContains(response, 'PermissionDenied')
        self.claim.refresh_from_db()
        self.assertEqual(self.claim.status, AuthorClaim.SUGGESTED)
        self.author.refresh_from_db()
        self.assertIsNone(self.author.fabric_uuid)

    def test_re_posting_the_same_decision_says_so_rather_than_deciding_twice(self):
        self.post({'claim_uuid': 'claim-1', 'action': 'approve'})
        response = self.post({'claim_uuid': 'claim-1', 'action': 'approve'})
        self.assertContains(response, 'already approved')

    def test_approving_an_author_someone_else_holds_is_refused_on_the_page(self):
        self.author.fabric_uuid = 'user-99'
        self.author.save()
        response = self.post({'claim_uuid': 'claim-1', 'action': 'approve'})
        self.assertContains(response, 'already claimed by')
        self.author.refresh_from_db()
        self.assertEqual(self.author.fabric_uuid, 'user-99')

    def test_a_claim_that_has_gone_is_reported_rather_than_raising(self):
        response = self.post({'claim_uuid': 'claim-gone', 'action': 'approve'})
        self.assertContains(response, 'no longer exists')

    def test_an_unknown_action_is_refused(self):
        response = self.post({'claim_uuid': 'claim-1', 'action': 'delete'})
        self.assertContains(response, 'Unknown action')
        self.claim.refresh_from_db()
        self.assertEqual(self.claim.status, AuthorClaim.SUGGESTED)

    def test_the_status_tabs_show_the_ledger(self):
        reject_suggestion(self.claim, decided_by=self.api_user)
        self.assertNotContains(self.get(), 'Jane Smith')
        response = self.get('?status=rejected')
        self.assertContains(response, 'Jane Smith')
        self.assertContains(response, 'An Admin')

    def test_an_unknown_status_falls_back_to_the_queue(self):
        response = self.get('?status=nonsense')
        self.assertContains(response, 'Jane Smith')

    def test_authors_are_ordered_by_their_best_candidate(self):
        weaker_author = Author.objects.create(
            author_name='Aaronson, A', display_name='Aaronson, A',
            publication_uuid='pub-1', uuid='auth-2',
        )
        AuthorClaim.objects.create(
            author=weaker_author, api_user=self.candidate, score=0.45, uuid='claim-2',
        )
        content = self.get().content.decode()
        self.assertLess(content.index('Smith, Jane'), content.index('Aaronson, A'))


class AuthorUpdateLedgerTests(TestCase):
    """
    The two decision paths that predate the queue now write to the ledger as well.
    """

    def setUp(self):
        self.api_user = ApiUser.objects.create(uuid='user-1', name='Jane Smith')
        self.target = ApiUser.objects.create(uuid='user-2', name='John Smith')
        self.publication = Publication.objects.create(
            authors=['auth-1'], title='A paper', uuid='pub-1', year='2026',
        )
        self.author = Author.objects.create(
            author_name='Smith, Jane', display_name='Smith, Jane',
            publication_uuid='pub-1', uuid='auth-1',
        )

    def post(self, data, is_admin=False):
        patches = (
            mock.patch('publicationtrkr.apps.publications.views.get_api_user',
                       return_value=self.api_user),
            mock.patch.object(ApiUser, 'can_create_publication', True),
            mock.patch.object(ApiUser, 'is_publication_tracker_admin', is_admin),
        )
        with patches[0], patches[1], patches[2]:
            return self.client.post('/publications/authors/auth-1/update', data)

    def test_a_self_claim_records_a_self_asserted_row(self):
        self.post({'save': 'save', 'display_name': 'Jane Smith'})
        self.author.refresh_from_db()
        self.assertEqual(self.author.fabric_uuid, 'user-1')
        claim = AuthorClaim.objects.get()
        self.assertEqual(claim.status, AuthorClaim.SELF_ASSERTED)
        self.assertEqual(claim.source, AuthorClaim.SELF)
        self.assertEqual(claim.api_user, self.api_user)

    def test_a_self_claim_withdraws_the_suggestions_it_settles(self):
        AuthorClaim.objects.create(
            author=self.author, api_user=self.target, score=0.55, uuid='claim-1',
        )
        self.post({'save': 'save', 'display_name': 'Jane Smith'})
        self.assertFalse(AuthorClaim.objects.filter(uuid='claim-1').exists())

    def test_an_admin_form_write_records_an_admin_approval(self):
        self.post({
            'save': 'save', 'author_name': 'Smith, Jane', 'display_name': 'Smith, Jane',
            'fabric_uuid': 'user-2', 'publication_uuid': 'pub-1',
        }, is_admin=True)
        self.author.refresh_from_db()
        self.assertEqual(self.author.fabric_uuid, 'user-2')
        claim = AuthorClaim.objects.get()
        self.assertEqual(claim.status, AuthorClaim.APPROVED)
        self.assertEqual(claim.source, AuthorClaim.ADMIN)

    def test_moving_an_author_updates_both_publications_author_arrays(self):
        # Regression: AuthorForm.is_valid() writes the posted values onto the instance, so
        # the "did publication_uuid change?" comparison used to read the new value on both
        # sides and never fired. Both arrays were left wrong.
        Publication.objects.create(authors=[], title='Another paper', uuid='pub-2', year='2026')
        self.post({
            'save': 'save', 'author_name': 'Smith, Jane', 'display_name': 'Smith, Jane',
            'fabric_uuid': '', 'publication_uuid': 'pub-2',
        }, is_admin=True)
        self.assertEqual(Publication.objects.get(uuid='pub-1').authors, [])
        self.assertEqual(Publication.objects.get(uuid='pub-2').authors, ['auth-1'])

    def test_an_admin_rename_withdraws_the_suggestions_computed_from_the_old_name(self):
        AuthorClaim.objects.create(
            author=self.author, api_user=self.target, score=0.55, uuid='claim-1',
        )
        self.post({
            'save': 'save', 'author_name': 'Okonkwo, Jane', 'display_name': 'Smith, Jane',
            'fabric_uuid': '', 'publication_uuid': 'pub-1',
        }, is_admin=True)
        self.author.refresh_from_db()
        self.assertEqual(self.author.author_name, 'Okonkwo, Jane')
        self.assertFalse(AuthorClaim.objects.filter(uuid='claim-1').exists())

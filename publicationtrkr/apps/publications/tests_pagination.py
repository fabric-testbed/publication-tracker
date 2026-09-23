"""
Every paginated listing serves each row exactly once (#75).

LIMIT/OFFSET pages are separate queries. Over a non-unique sort key Postgres does not
promise that consecutive pages slice one consistent order: a bounded top-N sort can
order tied rows differently for different offsets. /api/authors ordered by author_name
alone, and on a production copy walking all 52 pages served 19 authors twice and 19
never -- author_name repeats whenever one person is credited on several papers. These
tests walk every page over a fixture whose tie group is several pages wide, and again
after updating rows inside it, since an UPDATE moves tuples and reshuffles the ties.
"""

from unittest import mock
from uuid import uuid4

from django.core.cache import cache
from django.test import TestCase
from rest_framework.pagination import PageNumberPagination
from rest_framework.test import APIClient

from publicationtrkr.apps.apiuser.models import ApiUser
from publicationtrkr.apps.publications.models import Author, Publication
from publicationtrkr.server.settings import REST_FRAMEWORK

PAGE_SIZE = 7  # not a divisor of any fixture size, so a tie group straddles every boundary
TIED = 60


class PageWalk:
    def setUp(self):
        # A walk is dozens of anonymous requests, and AnonRateThrottle counts them in
        # the process-wide cache: without this, later test modules inherit a spent
        # budget and get 429s.
        cache.clear()
        self.addCleanup(cache.clear)
        super().setUp()

    def walk_api(self, url):
        client = APIClient()
        seen, page = [], 1
        with mock.patch.object(PageNumberPagination, 'page_size', PAGE_SIZE):
            while True:
                sep = '&' if '?' in url else '?'
                data = client.get(f'{url}{sep}page={page}').data
                seen += [row['uuid'] for row in data['results']]
                if not data['next']:
                    return seen, data['count']
                page += 1

    def assert_each_once(self, seen, expected):
        self.assertEqual(len(seen), len(expected), 'rows served != rows listed')
        self.assertEqual(sorted(seen), sorted(expected), 'some rows served twice, others never')


class AuthorListPaginationTests(PageWalk, TestCase):
    def setUp(self):
        super().setUp()
        publication = str(uuid4())
        for i in range(TIED):
            Author.objects.create(author_name='Jane Doe', display_name='Jane Doe',
                                  author_order=i, publication_uuid=publication, uuid=str(uuid4()))
        for name in ('Aaron Aye', 'Zed Zee'):
            Author.objects.create(author_name=name, display_name=name,
                                  publication_uuid=publication, uuid=str(uuid4()))

    def test_every_author_is_served_exactly_once(self):
        seen, count = self.walk_api('/api/authors')
        self.assertEqual(count, TIED + 2)
        self.assert_each_once(seen, Author.objects.values_list('uuid', flat=True))

    def test_still_exactly_once_after_updates_inside_the_tie_group(self):
        for author in Author.objects.filter(author_name='Jane Doe').order_by('?')[:TIED // 2]:
            author.display_name = 'J. Doe'
            author.save()
        seen, _ = self.walk_api('/api/authors')
        self.assert_each_once(seen, Author.objects.values_list('uuid', flat=True))

    def test_search_results_page_the_same_way(self):
        seen, count = self.walk_api('/api/authors?search=Jane')
        self.assertEqual(count, TIED)
        self.assert_each_once(seen, Author.objects.filter(author_name='Jane Doe').values_list('uuid', flat=True))


class PublicationListPaginationTests(PageWalk, TestCase):
    """Titles are unique only per (title, link), so a title can tie. None do in production yet."""

    def setUp(self):
        super().setUp()
        self.project = str(uuid4())
        self.fabric_uuid = str(uuid4())
        for i in range(TIED // 2):
            publication = Publication.objects.create(
                title='Same Title', link=f'https://example.org/{i}', year='2024',
                project_uuid=self.project, uuid=str(uuid4()))
            Author.objects.create(author_name='Jane Doe', display_name='Jane Doe',
                                  fabric_uuid=self.fabric_uuid,
                                  publication_uuid=publication.uuid, uuid=str(uuid4()))

    def test_every_listing_serves_each_publication_once(self):
        expected = Publication.objects.values_list('uuid', flat=True)
        for url in ('/api/publications', '/api/publications?sort_by=title',
                    '/api/publications?sort_by=year&order_by=asc',
                    f'/api/publications/by-project-uuid?project_uuid={self.project}',
                    f'/api/publications/by-author-uuid?fabric_uuid={self.fabric_uuid}'):
            with self.subTest(url=url):
                seen, _ = self.walk_api(url)
                self.assert_each_once(seen, expected)


class ApiUserDirectoryPaginationTests(PageWalk, TestCase):
    def setUp(self):
        super().setUp()
        self.admin = ApiUser.objects.create(uuid=str(uuid4()), name='Admin')
        for _ in range(TIED):
            ApiUser.objects.create(uuid=str(uuid4()), name='Jane Doe')

    def test_admin_directory_serves_each_user_once(self):
        seen, page = [], 1
        with mock.patch('publicationtrkr.apps.apiuser.views.get_api_user', return_value=self.admin), \
                mock.patch.object(ApiUser, 'is_publication_tracker_admin', True), \
                mock.patch.dict(REST_FRAMEWORK, {'PAGE_SIZE': PAGE_SIZE}):
            while page:
                context = self.client.get(f'/apiusers/?page={page}').context
                seen += [user.uuid for user in context['api_users']]
                page = context['next_page']
        self.assert_each_once(seen, ApiUser.objects.values_list('uuid', flat=True))

from uuid import UUID
import os

from django.db.models import Q
from django.shortcuts import get_object_or_404
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema, OpenApiParameter
from rest_framework import filters, permissions, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response

from publicationtrkr.apps.publications.api.serializers import AuthorSerializer, PublicationSerializer, PublicationCreateSerializer
from publicationtrkr.apps.publications.api.validators import validate_publication_create, validate_publication_update
from publicationtrkr.apps.publications.models import Author, Publication
from publicationtrkr.apps.publications.utils.bibtex_utils import generate_bibtex
from publicationtrkr.apps.publications.utils.bulk_ingest import (
    BulkIngestError,
    collect_records,
    ingest,
)
from publicationtrkr.apps.publications.utils.publication_builder import (
    create_publication,
    update_publication,
)
from publicationtrkr.utils.fabric_auth import get_api_user, is_valid_uuid
from publicationtrkr.apps.apiuser.models import ApiUser
from publicationtrkr.utils.core_api import query_core_api_by_cookie, query_core_api_by_token


class IsPublicationTrackerAdminOrReadOnly(permissions.BasePermission):
    """
    Read for anyone, write for publication tracker admins only.

    Identity comes from get_api_user() rather than request.user: this project sets
    DEFAULT_AUTHENTICATION_CLASSES to [], and the caller is resolved from a Vouch
    cookie or a FABRIC bearer token, so request.user is always anonymous here and
    must not be consulted.
    """

    def has_permission(self, request, view):
        if request.method in permissions.SAFE_METHODS:
            return True
        return get_api_user(request=request).is_publication_tracker_admin


class DynamicSearchFilter(filters.SearchFilter):
    def get_search_fields(self, view, request):
        if request.parser_context.get('view').action == 'list':
            return ['title', 'project_name']
        else:
            return []


class AuthorSearchFilter(filters.SearchFilter):
    """
    Search filter for Author objects.
    Only activates when the search term is 3 or more characters.
    Matches against author_name and display_name using icontains (LIKE-style).
    """
    def filter_queryset(self, request, queryset, view):
        search_terms = self.get_search_terms(request)
        if not search_terms or len(search_terms[0]) < 3:
            return queryset
        return super().filter_queryset(request, queryset, view)

    def get_search_fields(self, view, request):
        return ['author_name', 'display_name']

def is_publication_owner(api_user, publication) -> bool:
    """
    Whether api_user created publication.

    Compares uuid strings. This used to read `api_user.uuid == publication.created_by`,
    which compares a str against an ApiUser instance and is therefore always False --
    so in practice only admins could update or delete anything, and an owner acting on
    their own publication was silently refused.

    created_by is on_delete=SET_NULL, so it can be None; a publication whose creator
    was removed has no owner rather than being owned by everyone.
    """
    if api_user is None or publication.created_by is None:
        return False
    return str(api_user.uuid) == str(publication.created_by.uuid)


class PublicationViewSet(viewsets.ModelViewSet):
    """
    API endpoint that allows users to be viewed or edited.
    - list (GET)
    - create (POST)
    - retrieve (GET id)
    - update (PUT id)
    - partial update (PATCH id)
    - destroy (DELETE id)
    """
    serializer_classes = {
        'list': PublicationSerializer,
        'create': PublicationCreateSerializer,
        'retrieve': PublicationSerializer,
        'update': PublicationCreateSerializer,
        'partial_update': PublicationSerializer,
        'destroy': PublicationSerializer,
    }
    default_serializer_class = PublicationSerializer
    queryset = Publication.objects.all().order_by('title')
    permission_classes = [permissions.AllowAny]
    # ScopedRateThrottle is in DEFAULT_THROTTLE_CLASSES and reads this attribute; a
    # falsy scope means "not throttled by scope", so every action except bulk() is
    # left to AnonRateThrottle. It has to exist on the class for @action to be allowed
    # to override it per action.
    throttle_scope = None
    filter_backends = [DynamicSearchFilter]
    lookup_field = 'uuid'

    def get_serializer_class(self):
        return self.serializer_classes.get(self.action, self.default_serializer_class)

    @extend_schema(
        parameters=[
            OpenApiParameter(
                name='sort_by',
                type=str,
                location=OpenApiParameter.QUERY,
                required=False,
                enum=['title', 'year'],
                description='Field to sort results by. Defaults to "year".',
            ),
            OpenApiParameter(
                name='order_by',
                type=str,
                location=OpenApiParameter.QUERY,
                required=False,
                enum=['asc', 'desc'],
                description='Sort direction. Defaults to "desc".',
            ),
        ],
    )
    def list(self, request, *args, **kwargs):
        """
        list (GET)
        """
        sort_by = request.query_params.get('sort_by', 'year').lower()
        order_by = request.query_params.get('order_by', 'desc').lower()
        if sort_by not in ('title', 'year'):
            sort_by = 'year'
        if order_by not in ('asc', 'desc'):
            order_by = 'desc'
        prefix = '-' if order_by == 'desc' else ''
        if sort_by == 'year':
            # secondary sort: title asc within each year
            self.queryset = Publication.objects.all().order_by(f'{prefix}year', 'title')
        else:
            self.queryset = Publication.objects.all().order_by(f'{prefix}title')
        return super().list(request, *args, **kwargs)

    def create(self, request, *args, **kwargs):
        """
        create (POST)
        - authors
        - bibtex
        - created
        - created_by
        - link
        - modified
        - modified_by
        - project_name
        - project_uuid
        - title
        - uuid
        - year
        """
        api_user = get_api_user(request=request)
        if not (api_user.can_create_publication or api_user.is_publication_tracker_admin):
            raise PermissionDenied(
                detail="PermissionDenied: user:'{0}' is unable to create /publications".format(api_user.uuid))
        is_valid, message = validate_publication_create(request, api_user=api_user)
        if not is_valid:
            raise ValidationError(detail={'ValidationError': message})
        try:
            publication = create_publication(
                data=request.data,
                api_user=api_user,
                resolve_project_name=memoized_project_name_resolver(request, api_user),
            )
        except Exception as exc:
            # Chiefly the unique constraint on title/link. The builder runs the whole
            # save in a transaction, so there are no half-written Author rows here.
            return Response(data={'UniqueConstraint': str(exc)}, status=400)
        return Response(data=PublicationSerializer(instance=publication).data, status=201)

    def retrieve(self, request, *args, **kwargs):
        """
        retrieve (GET {int:pk})
        """
        return super().retrieve(request, *args, **kwargs)

    def update(self, request, *args, **kwargs):
        """
        update (PUT {int:pk})
        - authors
        - bibtex
        - link
        - modified
        - modified_by
        - project_name
        - project_uuid
        - title
        - year
        """
        publication_uuid = request.data.get('uuid', None)
        if not publication_uuid:
            publication_uuid = kwargs.get('uuid')
        publication = get_object_or_404(Publication, uuid=publication_uuid)
        api_user = get_api_user(request=request)
        if not (is_publication_owner(api_user, publication) or api_user.is_publication_tracker_admin):
            raise PermissionDenied(
                detail="PermissionDenied: user:'{0}' is unable to update /publications/{1}".format(
                    api_user.uuid, kwargs.get('uuid')))
        is_valid, message = validate_publication_update(request, api_user=api_user)
        if not is_valid:
            raise ValidationError(detail={'ValidationError': message})
        publication = update_publication(
            publication=publication,
            data=request.data,
            api_user=api_user,
            resolve_project_name=memoized_project_name_resolver(request, api_user),
        )
        return Response(data=PublicationSerializer(instance=publication).data, status=200)

    def partial_update(self, request, *args, **kwargs):
        """
        partial_update (PATCH {int:pk})
        """
        return self.update(request, *args, **kwargs)

    def destroy(self, request, *args, **kwargs):
        """
        destroy (DELETE {int:pk})
        """
        publication_uuid = request.data.get('uuid', None)
        if not publication_uuid:
            publication_uuid = kwargs.get('uuid')
        publication = get_object_or_404(Publication, uuid=publication_uuid)
        api_user = get_api_user(request=request)
        if is_publication_owner(api_user, publication) or api_user.is_publication_tracker_admin:
            Author.objects.filter(publication_uuid=publication.uuid).delete()
            publication.delete()
            return Response(status=204)
        else:
            raise PermissionDenied(
                detail="PermissionDenied: user:'{0}' is unable to delete /publications/{1}".format(api_user.uuid,
                                                                                                   kwargs.get('uuid')))

    @extend_schema(
        summary='Bulk create publications',
        description=(
            'Create many publications in one request. Admin only.\n\n'
            'Send either a JSON body -- a list of publication objects, or an object '
            "with a 'publications' list -- or a multipart upload named 'file' holding "
            'a .jsonl document (one publication object per line) or a .bib document '
            '(parsed as BibTeX, one publication per entry).\n\n'
            'Every record is reported on by position. For a .jsonl upload each result '
            'also carries the 1-based file line, which is the number to fix. Records '
            'are created one at a time in their own transactions, so a duplicate is '
            "reported as 'skipped' and the rest of the batch still lands.\n\n"
            'A multipart upload must carry an X-Requested-With header: unsafe requests '
            'under /api/ with a form-encoded, multipart or text/plain body are refused '
            'without one. A JSON body needs no such header.'
        ),
        request=OpenApiTypes.OBJECT,
        responses={
            200: OpenApiTypes.OBJECT,
            400: OpenApiTypes.OBJECT,
            403: OpenApiTypes.OBJECT,
        },
    )
    @action(detail=False, methods=['post'], url_path='bulk',
            permission_classes=[IsPublicationTrackerAdminOrReadOnly],
            throttle_scope='bulk')
    def bulk(self, request, *args, **kwargs):
        """
        POST /api/publications/bulk
        """
        api_user = get_api_user(request=request)
        # permission_classes covers the HTTP path. This covers the other one: the
        # bulk-upload page calls this method in process (publications/views.py), which
        # never reaches DRF dispatch and so never runs a permission class.
        if not api_user.is_publication_tracker_admin:
            raise PermissionDenied(
                detail="PermissionDenied: user:'{0}' is unable to bulk create /publications".format(
                    api_user.uuid))
        try:
            records, failures = collect_records(
                data=getattr(request, 'data', None),
                upload=getattr(request, 'FILES', {}).get('file', None),
            )
        except BulkIngestError as exc:
            # Nothing was written: the caps are checked before any database work.
            return Response(data={'BulkIngestError': str(exc)}, status=400)
        summary = ingest(
            records, failures,
            api_user=api_user,
            resolve_project_name=memoized_project_name_resolver(request, api_user),
        )
        return Response(data=summary, status=200)

    @action(detail=True, methods=['get'], url_path='bibtex')
    def bibtex(self, request, uuid=None):
        """
        GET /api/publications/{uuid}/bibtex
        Returns stored bibtex or auto-generates from model fields.
        """
        publication = get_object_or_404(Publication, uuid=uuid)
        if publication.bibtex:
            bibtex_text = publication.bibtex
        else:
            bibtex_text = generate_bibtex(publication)
        return Response(data={'bibtex': bibtex_text})

    @extend_schema(
        parameters=[
            OpenApiParameter(
                name='fabric_uuid',
                type=str,
                location=OpenApiParameter.QUERY,
                required=True,
                description='Fully-formed FABRIC user UUID (e.g. xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx). '
                            'Partial UUIDs are rejected. Returns all publications where this UUID '
                            'is associated with at least one author.',
            )
        ],
        responses=PublicationSerializer(many=True),
    )
    @action(detail=False, methods=['get'], url_path='by-author-uuid')
    def by_author_uuid(self, request):
        """
        GET /api/publications/by-author-uuid?fabric_uuid=<uuid>
        Returns publications where the given fabric_uuid matches an associated Author.
        Only fully-formed UUIDs are accepted; partial values are rejected.
        """
        fabric_uuid = request.query_params.get('fabric_uuid', '').strip()
        if not fabric_uuid:
            raise ValidationError(detail={'fabric_uuid': 'This query parameter is required.'})
        try:
            UUID(fabric_uuid, version=4)
        except ValueError:
            raise ValidationError(detail={'fabric_uuid': 'A valid UUID is required.'})
        # Find publication UUIDs that have an Author with this fabric_uuid
        pub_uuids = Author.objects.filter(
            fabric_uuid=fabric_uuid
        ).values_list('publication_uuid', flat=True).distinct()
        queryset = Publication.objects.filter(uuid__in=pub_uuids).order_by('title')
        page = self.paginate_queryset(queryset)
        if page is not None:
            serializer = PublicationSerializer(page, many=True)
            return self.get_paginated_response(serializer.data)
        serializer = PublicationSerializer(queryset, many=True)
        return Response(serializer.data)

    @extend_schema(
        parameters=[
            OpenApiParameter(
                name='project_uuid',
                type=str,
                location=OpenApiParameter.QUERY,
                required=True,
                description='Fully-formed FABRIC project UUID (e.g. xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx). '
                            'Partial UUIDs are rejected. Returns all publications associated with this project.',
            ),
            OpenApiParameter(
                name='search',
                type=str,
                location=OpenApiParameter.QUERY,
                required=False,
                description='Optional search term (3 or more characters) to filter results by title or project name.',
            ),
        ],
        responses=PublicationSerializer(many=True),
    )
    @action(detail=False, methods=['get'], url_path='by-project-uuid')
    def by_project_uuid(self, request):
        """
        GET /api/publications/by-project-uuid?project_uuid=<uuid>[&search=<term>][&page=<n>]
        Returns publications associated with the given project_uuid.
        Only fully-formed UUIDs are accepted; partial values are rejected.
        Supports optional ?search= filtering on title and project_name (3+ characters).
        Supports optional ?page= for pagination.
        Open to all users.
        """
        project_uuid = request.query_params.get('project_uuid', '').strip()
        if not project_uuid:
            raise ValidationError(detail={'project_uuid': 'This query parameter is required.'})
        try:
            UUID(project_uuid, version=4)
        except ValueError:
            raise ValidationError(detail={'project_uuid': 'A valid UUID is required.'})
        queryset = Publication.objects.filter(project_uuid=project_uuid).order_by('title')
        search = request.query_params.get('search', '').strip()
        if search and len(search) >= 3:
            queryset = queryset.filter(
                Q(title__icontains=search) | Q(project_name__icontains=search)
            )
        page = self.paginate_queryset(queryset)
        if page is not None:
            serializer = PublicationSerializer(page, many=True)
            return self.get_paginated_response(serializer.data)
        serializer = PublicationSerializer(queryset, many=True)
        return Response(serializer.data)


class AuthorViewSet(viewsets.ModelViewSet):
    """
    Author records.

    Reads are public -- the author directory backs anonymous publication browsing.
    Writes are admin-only: this viewset previously ran with AllowAny and performed no
    authorization in any handler, so POST/PUT/PATCH/DELETE on /api/authors were open
    to anyone on the internet. Nothing in the UI writes through here; author records
    are created by the publication write path, and claiming goes through the
    author_update web view, which has its own checks.
    """
    serializer_class = AuthorSerializer
    queryset = Author.objects.all().order_by('author_name')
    permission_classes = [IsPublicationTrackerAdminOrReadOnly]
    filter_backends = [AuthorSearchFilter]
    lookup_field = 'uuid'


def get_project_name_from_uuid(request, project_uuid, api_user) -> str:
    if project_uuid:
        # project_uuid is interpolated into the outbound request path below, so it is
        # checked here as well as in validators.py. The validators run first on the
        # create and update paths, but this function is the reusable one -- a later
        # caller that skips them would otherwise reintroduce the same hole.
        if not is_valid_uuid(project_uuid):
            print('get_project_name_from_uuid: refusing non-UUID project_uuid')
            return None
        try:
            if api_user.access_type == ApiUser.COOKIE:
                fab_project = query_core_api_by_cookie(
                    query='/projects/{0}'.format(project_uuid),
                    cookie=request.COOKIES.get(os.getenv('VOUCH_COOKIE_NAME'), None))
            else:
                fab_project = query_core_api_by_token(
                    query='/projects/{0}'.format(project_uuid),
                    token=request.headers.get('authorization', 'Bearer ').replace('Bearer ', ''))
            project_name = fab_project.get('results')[0].get('name')
        except Exception as exc:
            print(exc)
            project_name = None
    else:
        project_name = None
    return project_name


def memoized_project_name_resolver(request, api_user):
    """
    A callable(project_uuid) -> str|None over get_project_name_from_uuid, caching for
    the life of one request.

    get_project_name_from_uuid is an uncached core-api round trip. One per request is
    fine; one per record is not. A 1000-record bulk upload naming a handful of projects
    would spend 1000 * FABRIC_HTTP_TIMEOUT_SECONDS in the worst case, against an nginx
    uwsgi_read_timeout measured in seconds -- the memo is what makes the record cap
    survivable rather than theoretical.
    """
    cache = {}

    def resolve(project_uuid):
        if project_uuid not in cache:
            cache[project_uuid] = get_project_name_from_uuid(request, project_uuid, api_user)
        return cache[project_uuid]

    return resolve

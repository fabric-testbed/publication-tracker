from datetime import datetime, timezone
from uuid import uuid4, UUID
import os

from django.db.models import Q
from django.shortcuts import get_object_or_404
from drf_spectacular.utils import extend_schema, OpenApiParameter
from rest_framework import filters, permissions, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response

from publicationtrkr.apps.publications.api.serializers import AuthorSerializer, PublicationSerializer, PublicationCreateSerializer
from publicationtrkr.apps.publications.api.validators import validate_publication_create, validate_publication_update
from publicationtrkr.apps.publications.models import Author, Publication
from publicationtrkr.apps.publications.utils.bibtex_utils import parse_bibtex, generate_bibtex
from publicationtrkr.utils.fabric_auth import get_api_user
from publicationtrkr.apps.apiuser.models import ApiUser
from publicationtrkr.utils.core_api import query_core_api_by_cookie, query_core_api_by_token


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
    filter_backends = [DynamicSearchFilter]
    lookup_field = 'uuid'

    def get_serializer_class(self):
        return self.serializer_classes.get(self.action, self.default_serializer_class)

    def list(self, request, *args, **kwargs):
        """
        list (GET)
        """
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
        if api_user.can_create_publication or api_user.is_publication_tracker_admin:
            is_valid, message = validate_publication_create(request, api_user=api_user)
            if is_valid:
                try:
                    now = datetime.now(timezone.utc)
                    request_data = request.data
                    # parse bibtex if provided (used as defaults)
                    bibtex_string = request_data.get('bibtex', None)
                    bibtex_data = {}
                    if bibtex_string:
                        bibtex_data = parse_bibtex(bibtex_string)
                    publication = Publication()
                    # authors (manual overrides bibtex)
                    authors = request_data.get('authors', [])
                    authors_list = authors if authors else bibtex_data.get('authors', [])
                    # bibtex
                    if bibtex_string and bibtex_string not in ['', ""]:
                        publication.bibtex = bibtex_string
                    # created
                    publication.created = now
                    # created_by
                    publication.created_by = api_user
                    # link (manual overrides bibtex)
                    link = request_data.get('link', None)
                    if link in ['', "", None]:
                        link = bibtex_data.get('link', None)
                    if link in ['', "", None]:
                        publication.link = None
                    else:
                        publication.link = link
                    # modified
                    publication.modified = now
                    # modified_by
                    publication.modified_by = api_user
                    # project_name
                    project_name = request_data.get('project_name', None)
                    if project_name in ['', "", None]:
                        publication.project_name = None
                    else:
                        publication.project_name = project_name
                    # project_uuid
                    project_uuid = request_data.get('project_uuid', None)
                    if project_uuid in ['', "", None]:
                        publication.project_uuid = None
                    else:
                        publication.project_uuid = project_uuid
                    # get project_name if not provided and project_uuid is given
                    if publication.project_uuid and not publication.project_name:
                        publication.project_name = get_project_name_from_uuid(request, publication.project_uuid, api_user)
                    # title (manual overrides bibtex)
                    title = request_data.get('title', None)
                    publication.title = title if title else bibtex_data.get('title', None)
                    # uuid
                    publication.uuid = str(uuid4())
                    # create Author objects for each author name
                    author_uuids = []
                    for author_name in authors_list:
                        author = Author()
                        author.author_name = author_name
                        author.display_name = author_name
                        author.fabric_uuid = None
                        author.publication_uuid = publication.uuid
                        author.uuid = str(uuid4())
                        author.save()
                        author_uuids.append(author.uuid)
                    publication.authors = author_uuids
                    # venue (manual overrides bibtex)
                    venue = request_data.get('venue', None)
                    if venue in ['', "", None]:
                        venue = bibtex_data.get('venue', None)
                    if venue in ['', "", None]:
                        publication.venue = None
                    else:
                        publication.venue = venue
                    # year (manual overrides bibtex)
                    year = request_data.get('year', None)
                    publication.year = year if year else bibtex_data.get('year', None)
                    # save to db
                    publication.save()
                    # return new publication
                    return Response(data=PublicationSerializer(instance=publication).data, status=201)
                except Exception as exc:
                    return Response(data={'UniqueConstraint': str(exc)}, status=400)
            else:
                raise ValidationError(detail={'ValidationError': message})
        else:
            raise PermissionDenied(
                detail="PermissionDenied: user:'{0}' is unable to create /publications".format(api_user.uuid))

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
        print(kwargs)
        print(request.data)
        publication_uuid = request.data.get('uuid', None)
        if not publication_uuid:
            publication_uuid = kwargs.get('uuid')
        publication = get_object_or_404(Publication, uuid=publication_uuid)
        api_user = get_api_user(request=request)
        if api_user.uuid == publication.created_by or api_user.is_publication_tracker_admin:
            is_valid, message = validate_publication_update(request, api_user=api_user)
            if is_valid:
                now = datetime.now(timezone.utc)
                request_data = request.data
                # parse bibtex if provided (used as defaults for fields not explicitly given)
                bibtex_string = request_data.get('bibtex', None)
                bibtex_data = {}
                if bibtex_string:
                    bibtex_data = parse_bibtex(bibtex_string)
                # bibtex
                if bibtex_string and bibtex_string not in ['', ""]:
                    publication.bibtex = bibtex_string
                # authors (manual overrides bibtex)
                authors_list = None
                if request_data.get('authors', None):
                    authors_list = request_data.get('authors', [])
                elif bibtex_data.get('authors'):
                    authors_list = bibtex_data.get('authors')
                if authors_list is not None:
                    # Preserve existing Author records where possible (keeps
                    # display_name, fabric_uuid, and uuid stable).  Match by
                    # position in the Publication.authors array.
                    existing_uuids = list(publication.authors)
                    new_author_uuids = []
                    for i, author_name in enumerate(authors_list):
                        if i < len(existing_uuids):
                            # Update existing Author in place
                            try:
                                author = Author.objects.get(uuid=existing_uuids[i])
                                if author.author_name != author_name:
                                    author.author_name = author_name
                                    author.save(update_fields=['author_name'])
                                new_author_uuids.append(author.uuid)
                                continue
                            except Author.DoesNotExist:
                                pass
                        # New author or missing record — create fresh
                        author = Author()
                        author.author_name = author_name
                        author.display_name = author_name
                        author.fabric_uuid = None
                        author.publication_uuid = publication.uuid
                        author.uuid = str(uuid4())
                        author.save()
                        new_author_uuids.append(author.uuid)
                    # Remove any leftover Authors beyond the new list length
                    for old_uuid in existing_uuids[len(authors_list):]:
                        Author.objects.filter(uuid=old_uuid).delete()
                    publication.authors = new_author_uuids
                # link (manual overrides bibtex)
                if request_data.get('link', None):
                    link = request_data.get('link')
                    if link in ['', ""]:
                        publication.link = None
                    else:
                        publication.link = link
                elif bibtex_data.get('link'):
                    publication.link = bibtex_data.get('link')
                # modified
                publication.modified = now
                # modified_by
                publication.modified_by = api_user
                # project_name
                if request_data.get('project_name', None):
                    project_name = request_data.get('project_name')
                    if project_name in ['', ""]:
                        publication.project_name = None
                    else:
                        publication.project_name = project_name
                # project_uuid
                if request_data.get('project_uuid', None):
                    project_uuid = request_data.get('project_uuid')
                    if project_uuid in ['', ""]:
                        publication.project_uuid = None
                    else:
                        publication.project_uuid = project_uuid
                # get project_name if not provided and project_uuid is given
                if publication.project_uuid and not publication.project_name:
                    publication.project_name = get_project_name_from_uuid(request, publication.project_uuid, api_user)
                # title (manual overrides bibtex)
                if request_data.get('title', None):
                    publication.title = request_data.get('title', None)
                elif bibtex_data.get('title'):
                    publication.title = bibtex_data.get('title')
                # venue (manual overrides bibtex)
                if request_data.get('venue', None):
                    venue = request_data.get('venue')
                    if venue in ['', ""]:
                        publication.venue = None
                    else:
                        publication.venue = venue
                elif bibtex_data.get('venue'):
                    publication.venue = bibtex_data.get('venue')
                # year (manual overrides bibtex)
                if request_data.get('year', None):
                    publication.year = request_data.get('year', None)
                elif bibtex_data.get('year'):
                    publication.year = bibtex_data.get('year')
                # save publication
                publication.save()
                # return updated publication
                return Response(data=PublicationSerializer(instance=publication).data, status=200)
            else:
                raise ValidationError(detail={'ValidationError': message})
        else:
            raise PermissionDenied(
                detail="PermissionDenied: user:'{0}' is unable to update /publications/{1}".format(api_user.uuid,
                                                                                                   kwargs.get('uuid')))

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
        if api_user.uuid == publication.created_by or api_user.is_publication_tracker_admin:
            Author.objects.filter(publication_uuid=publication.uuid).delete()
            publication.delete()
            return Response(status=204)
        else:
            raise PermissionDenied(
                detail="PermissionDenied: user:'{0}' is unable to delete /publications/{1}".format(api_user.uuid,
                                                                                                   kwargs.get('uuid')))

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
    serializer_class = AuthorSerializer
    queryset = Author.objects.all().order_by('author_name')
    permission_classes = [permissions.AllowAny]
    filter_backends = [AuthorSearchFilter]
    lookup_field = 'uuid'


def get_project_name_from_uuid(request, project_uuid, api_user) -> str:
    if project_uuid:
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

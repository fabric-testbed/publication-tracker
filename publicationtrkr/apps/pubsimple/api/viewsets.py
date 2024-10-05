from datetime import datetime, timezone
from uuid import uuid4
import os

from django.db.models import Q
from django.shortcuts import get_object_or_404
from rest_framework import filters, permissions, viewsets
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response

from publicationtrkr.apps.pubsimple.api.serializers import PubSimpleSerializer, PubSimpleCreateSerializer
from publicationtrkr.apps.pubsimple.api.validators import validate_pubsimple_create, validate_pubsimple_update
from publicationtrkr.apps.pubsimple.models import PubSimple
from publicationtrkr.utils.fabric_auth import get_api_user
from publicationtrkr.apps.apiuser.models import ApiUser
from publicationtrkr.utils.core_api import query_core_api_by_cookie, query_core_api_by_token


class DynamicSearchFilter(filters.SearchFilter):
    def get_search_fields(self, view, request):
        if request.parser_context.get('view').action == 'list':
            return ['title', 'project_name']
        else:
            return []

class PubSimpleViewSet(viewsets.ModelViewSet):
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
        'list': PubSimpleSerializer,
        'create': PubSimpleCreateSerializer,
        'retrieve': PubSimpleSerializer,
        'update': PubSimpleCreateSerializer,
        'partial_update': PubSimpleSerializer,
        'destroy': PubSimpleSerializer,
    }
    default_serializer_class = PubSimpleSerializer
    queryset = PubSimple.objects.all().order_by('title')
    permission_classes = [permissions.AllowAny]
    filter_backends = [DynamicSearchFilter]
    lookup_field = 'uuid'

    # def get_queryset(self):
    #     api_user = get_api_user(request=self.request)
    #     if self.kwargs.get('author_uuid', None):
    #         qs1 = PubSimple.objects.filter(
    #             authors__uuid__in=[self.kwargs.get('author_uuid')]
    #         ).distinct().order_by('-created')
    #         qs2 = PubSimple.objects.filter(
    #             Q(visibility=PubSimple.PUBLIC) |
    #             Q(project_uuid__in=api_user.projects) |
    #             Q(authors__uuid__in=[api_user.uuid])
    #         ).distinct().order_by('-created')
    #         return qs1.intersection(qs2).order_by('-created')
    #     else:
    #         return PubSimple.objects.filter(
    #             Q(visibility=PubSimple.PUBLIC) |
    #             Q(project_uuid__in=api_user.projects) |
    #             Q(authors__uuid__contains=api_user.uuid)
    #         ).distinct().order_by('-created')

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
            is_valid, message = validate_pubsimple_create(request, api_user=api_user)
            if is_valid:
                try:
                    now = datetime.now(timezone.utc)
                    request_data = request.data
                    pubsimple = PubSimple()
                    # authors
                    pubsimple.authors = request_data.get('authors', [])
                    # created
                    pubsimple.created = now
                    # created_by
                    pubsimple.created_by = api_user
                    # link
                    pubsimple.link = request_data.get('link', None)
                    # modified
                    pubsimple.modified = now
                    # modified_by
                    pubsimple.modified_by = api_user
                    # project_name
                    pubsimple.project_name = request_data.get('project_name', None)
                    # project_uuid
                    pubsimple.project_uuid = request_data.get('project_uuid', None)
                    if not pubsimple.project_name:
                        pubsimple.project_name = get_project_name_from_uuid(request, pubsimple.project_uuid, api_user)
                    # title
                    pubsimple.title = request_data.get('title', None)
                    # uuid
                    pubsimple.uuid = str(uuid4())
                    # year
                    pubsimple.year = request_data.get('year', None)
                    # save to db
                    pubsimple.save()
                    # return new pubsimple
                    return Response(data=PubSimpleSerializer(instance=pubsimple).data, status=201)
                except Exception as exc:
                    return Response(data={'UniqueConstraint': str(exc)}, status=400)
            else:
                raise ValidationError(detail={'ValidationError': message})
        else:
            raise PermissionDenied(
                detail="PermissionDenied: user:'{0}' is unable to create /pubsimple".format(api_user.uuid))

    def retrieve(self, request, *args, **kwargs):
        """
        retrieve (GET {int:pk})
        """
        return super().retrieve(request, *args, **kwargs)

    def update(self, request, *args, **kwargs):
        """
        update (PUT {int:pk})
        - authors
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
        pubsimple_uuid = request.data.get('uuid', None)
        if not pubsimple_uuid:
            pubsimple_uuid = kwargs.get('uuid')
        pubsimple = get_object_or_404(PubSimple, uuid=pubsimple_uuid)
        api_user = get_api_user(request=request)
        if api_user.uuid == pubsimple.created_by or api_user.is_publication_tracker_admin:
            is_valid, message = validate_pubsimple_update(request, api_user=api_user)
            if is_valid:
                now = datetime.now(timezone.utc)
                request_data = request.data
                # authors
                if request_data.get('authors', None):
                    pubsimple.authors = request_data.get('authors', [])
                # link
                if request_data.get('link', None):
                    pubsimple.link = request_data.get('link', None)
                # modified
                pubsimple.modified = now
                # modified_by
                pubsimple.modified_by = api_user
                # project_name
                if request_data.get('project_name', None):
                    pubsimple.project_name = request_data.get('project_name', None)
                # project_uuid
                if request_data.get('project_uuid', None):
                    pubsimple.project_uuid = request_data.get('project_uuid', None)
                    # print(pubsimple.project_uuid)
                    if not pubsimple.project_name:
                        pubsimple.project_name = get_project_name_from_uuid(request, pubsimple.project_uuid, api_user)
                        # print(pubsimple.project_name)
                # title
                if request_data.get('title', None):
                    pubsimple.title = request_data.get('title', None)
                # year
                if request_data.get('year', None):
                    pubsimple.year = request_data.get('year', None)
                # save pubsimple
                pubsimple.save()
                # return updated artifact
                return Response(data=PubSimpleSerializer(instance=pubsimple).data, status=200)
            else:
                raise ValidationError(detail={'ValidationError': message})
        else:
            raise PermissionDenied(
                detail="PermissionDenied: user:'{0}' is unable to update /pubsimple/{1}".format(api_user.uuid,
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
        pubsimple_uuid = request.data.get('uuid', None)
        if not pubsimple_uuid:
            pubsimple_uuid = kwargs.get('uuid')
        pubsimple = get_object_or_404(PubSimple, uuid=pubsimple_uuid)
        api_user = get_api_user(request=request)
        if api_user.uuid == pubsimple.created_by or api_user.is_publication_tracker_admin:
            pubsimple.delete()
            return Response(status=204)
        else:
            raise PermissionDenied(
                detail="PermissionDenied: user:'{0}' is unable to delete /pubsimple/{1}".format(api_user.uuid,
                                                                                                kwargs.get('uuid')))

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

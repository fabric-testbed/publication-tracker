import json
from urllib.parse import parse_qs, urlparse

from django.db import models
from django.http import QueryDict
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext_lazy as _
from rest_framework import status

from publicationtrkr.apps.pubsimple.api.viewsets import PubSimpleViewSet
from publicationtrkr.apps.pubsimple.forms import PubSimpleForm
from publicationtrkr.apps.pubsimple.models import PubSimple
from publicationtrkr.server.settings import API_DEBUG, REST_FRAMEWORK
from publicationtrkr.utils.fabric_auth import get_api_user


class ListObjectType(models.TextChoices):
    SIMPLEPUB = 'simplepub', _('SimplePub')
    PUBLICATIONS = 'publications', _('Publications')
    AUTHORS = 'authors', _('Authors')
    AUTHOR_BY_UUID = 'author_by_uuid', _('Author by UUID')


def pubsimple_list(request):
    api_user = get_api_user(request=request)
    try:
        simplepubs = list_object_paginator(request=request, object_type=ListObjectType.SIMPLEPUB)
        message = simplepubs.get('message', None)
    except Exception as exc:
        message = exc
        simplepubs = {}
    return render(request,
                  'pubsimple_list.html',
                  {
                      'api_user': api_user.as_dict(),
                      'simplepubs': simplepubs.get('list_objects', {}),
                      'item_range': simplepubs.get('item_range', None),
                      'message': message,
                      'next_page': simplepubs.get('next_page', None),
                      'prev_page': simplepubs.get('prev_page', None),
                      'search': simplepubs.get('search', None),
                      'count': simplepubs.get('count', None),
                      'debug': API_DEBUG
                  })


def pubsimple_create(request):
    api_user = get_api_user(request=request)
    message = None
    if request.method == "POST":
        form = PubSimpleForm(request.POST)
        if form.is_valid():
            try:
                request.data = QueryDict('', mutable=True)
                data_dict = form.cleaned_data
                request.data.update(data_dict)
                p = PubSimpleViewSet(request=request)
                pubsimple = p.create(request=request)
                if pubsimple.status_code == status.HTTP_201_CREATED:
                    pubsimple_uuid = json.loads(json.dumps(pubsimple.data)).get('uuid')
                    return redirect('pubsimple_detail', uuid=pubsimple_uuid)
                else:
                    message = str(pubsimple.data)
            except Exception as exc:
                message = str(exc)
        else:
            message = 'ValidationError: ' + str(form.errors.as_text())
    else:
        form = PubSimpleForm()
    return render(request,
                  'pubsimple_create.html',
                  {
                      'form': form,
                      'message': message,
                      'api_user': api_user.as_dict(),
                  })


def pubsimple_update(request, *args, **kwargs):
    api_user = get_api_user(request=request)
    pubsimple_uuid = kwargs.get('uuid')
    pubsimple = get_object_or_404(PubSimple, uuid=pubsimple_uuid)
    message = None
    if request.method == "POST" and isinstance(request.POST.get('save'), str):
        form = PubSimpleForm(request.POST, instance=pubsimple)
        if form.is_valid():
            print('update pub')
            try:
                request.data = QueryDict('', mutable=True)
                data_dict = form.cleaned_data
                request.data.update(data_dict)
                a = PubSimpleViewSet(request=request)
                pubsimple = a.update(request=request, uuid=pubsimple_uuid)
                if pubsimple.status_code == 200:
                    return redirect('pubsimple_detail', uuid=pubsimple_uuid)
                else:
                    message = str(pubsimple.data)
            except Exception as exc:
                message = str(exc)
        else:
            message = 'ValidationError: ' + str(form.errors.as_text())
    else:
        form = PubSimpleForm(instance=pubsimple, authors=[a for a in pubsimple.authors])
    return render(request,
                  'pubsimple_update.html',
                  {
                      'api_user': api_user.as_dict(),
                      'pubsimple_uuid': pubsimple_uuid,
                      'debug': API_DEBUG,
                      'form': form,
                      'message': message
                  })


def pubsimple_detail(request, *args, **kwargs):
    api_user = get_api_user(request=request)
    message = kwargs.get('message', None)
    if request.method == 'POST':
        try:
            pubsimple_detail_button = request.POST.get('pubsimple_detail_button', None)
            v_api_request = request.POST.copy()
            v_api_request.COOKIES = request.COOKIES
            v_api_request.headers = request.headers
            v_api_request.data = QueryDict('', mutable=True)
            if pubsimple_detail_button == "delete_pubsimple":
                publication_uuid = request.POST.get('pubsimple_uuid', None)
                v_api_request.method = 'DELETE'
                v_api_request.data.update(
                    {
                        'uuid': publication_uuid
                    }
                )
                a = PubSimpleViewSet(request=v_api_request)
                pubsimple = a.destroy(request=v_api_request)
                return redirect('pubsimple_list')
            else:
                return redirect('pubsimple_detail', uuid=kwargs.get('uuid'))

        except Exception as exc:
            message = exc
            print(message)
            return redirect('pubsimple_detail', uuid=kwargs.get('uuid'))
    # get publication detail page when not method: POST
    try:
        publication = PubSimpleViewSet.as_view({'get': 'retrieve'})(request=request, *args, **kwargs)
        if publication.data and publication.status_code == status.HTTP_200_OK:
            publication = json.loads(json.dumps(publication.data))
            is_author = api_user.uuid == publication.get('created_by')
        else:
            message = {'status_code': publication.status_code, 'detail': publication.data.get('detail')}
            is_author = False
        if not message:
            message = publication.get('message', None)
    except Exception as exc:
        message = exc
        publication = {}
        is_author = False
    return render(request,
                  'pubsimple_detail.html',
                  {
                      'api_user': api_user.as_dict(),
                      'publication': publication,
                      'is_author': is_author,
                      'message': message,
                      'debug': API_DEBUG
                  })


def list_object_paginator(request, object_type: str, *args, **kwargs):
    message = None
    try:
        # check for query parameters
        current_page = 1
        search_term = None
        data_dict = {}
        if request.GET.get('search'):
            data_dict['search'] = request.GET.get('search')
            search_term = request.GET.get('search')
        if request.GET.get('page'):
            data_dict['page'] = request.GET.get('page')
            current_page = int(request.GET.get('page'))
        request.query_params = QueryDict('', mutable=True)
        request.query_params.update(data_dict)
        if object_type == ListObjectType.SIMPLEPUB:
            list_objects = PubSimpleViewSet.as_view({'get': 'list'})(request=request)
        # elif object_type == ListObjectType.PUBLICATIONS:
        #     list_objects = ArtifactViewSet.as_view({'get': 'list'})(request=request)
        # elif object_type == ListObjectType.AUTHORS:
        #     list_objects = AuthorViewSet.as_view({'get': 'list'})(request=request)
        # elif object_type == ListObjectType.AUTHOR_BY_UUID:
        #     list_objects = ArtifactViewSet.as_view({'get': 'by_author'})(request=request, *args, **kwargs)
        else:
            list_objects = {}
        # get prev, next and item range
        next_page = None
        prev_page = None
        count = 0
        min_range = 0
        max_range = 0
        if list_objects.data:
            list_objects = json.loads(json.dumps(list_objects.data))
            prev_url = list_objects.get('previous', None)
            if prev_url:
                prev_dict = parse_qs(urlparse(prev_url).query)
                try:
                    prev_page = prev_dict['page'][0]
                except Exception as exc:
                    print(exc)
                    prev_page = 1
            next_url = list_objects.get('next', None)
            if next_url:
                next_dict = parse_qs(urlparse(next_url).query)
                try:
                    next_page = next_dict['page'][0]
                except Exception as exc:
                    print(exc)
                    next_page = 1
            count = int(list_objects.get('count'))
            min_range = int(current_page - 1) * int(REST_FRAMEWORK['PAGE_SIZE']) + 1
            max_range = int(current_page - 1) * int(REST_FRAMEWORK['PAGE_SIZE']) + int(REST_FRAMEWORK['PAGE_SIZE'])
            if max_range > count:
                max_range = count
        else:
            list_objects = {}
        item_range = '{0} - {1}'.format(str(min_range), str(max_range))
    except Exception as exc:
        message = exc
        list_objects = {}
        item_range = None
        next_page = None
        prev_page = None
        search_term = None
        count = 0
    return {
        'list_objects': list_objects,
        'item_range': item_range,
        'message': message,
        'next_page': next_page,
        'prev_page': prev_page,
        'search': search_term,
        'count': count,
        'debug': API_DEBUG
    }

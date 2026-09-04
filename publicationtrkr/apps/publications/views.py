import json
from urllib.parse import parse_qs, urlparse

from django.conf import settings
from django.core.paginator import Paginator
from django.db import models
from django.db.models import Count, Q
from django.http import QueryDict
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext_lazy as _
from rest_framework import status

from publicationtrkr.apps.publications.api.serializers import PublicationSerializer
from publicationtrkr.apps.publications.api.viewsets import AuthorViewSet, PublicationViewSet
from publicationtrkr.apps.publications.forms import AuthorForm, PublicationForm
from publicationtrkr.apps.publications.models import Author, AuthorClaim, Publication
from publicationtrkr.apps.publications.utils.claim_ledger import (
    approve_suggestion,
    record_admin_claim,
    record_self_claim,
    reject_suggestion,
    withdraw_suggestions,
)
from publicationtrkr.server.settings import API_DEBUG, REST_FRAMEWORK
from publicationtrkr.utils.fabric_auth import get_api_user


class ListObjectType(models.TextChoices):
    PUBLICATIONS = 'publications', _('Publications')
    AUTHORS = 'authors', _('Authors')
    AUTHOR_BY_UUID = 'author_by_uuid', _('Author by UUID')


def author_list(request):
    api_user = get_api_user(request=request)
    try:
        authors = list_object_paginator(request=request, object_type=ListObjectType.AUTHORS)
        message = authors.get('message', None)
    except Exception as exc:
        message = exc
        authors = {}
    return render(request,
                  'author_list.html',
                  {
                      'api_user': api_user.as_dict(),
                      'authors': authors.get('list_objects', {}),
                      'item_range': authors.get('item_range', None),
                      'message': message,
                      'next_page': authors.get('next_page', None),
                      'prev_page': authors.get('prev_page', None),
                      'search': authors.get('search', None),
                      'count': authors.get('count', None),
                      'debug': API_DEBUG,
                  })


def publication_list(request):
    api_user = get_api_user(request=request)
    sort_by = request.GET.get('sort_by', 'year')
    order_by = request.GET.get('order_by', 'desc')
    if sort_by not in ('title', 'year'):
        sort_by = 'year'
    if order_by not in ('asc', 'desc'):
        order_by = 'desc'
    try:
        publications = list_object_paginator(request=request, object_type=ListObjectType.PUBLICATIONS)
        message = publications.get('message', None)
    except Exception as exc:
        message = exc
        publications = {}
    return render(request,
                  'publication_list.html',
                  {
                      'api_user': api_user.as_dict(),
                      'publications': publications.get('list_objects', {}),
                      'item_range': publications.get('item_range', None),
                      'message': message,
                      'next_page': publications.get('next_page', None),
                      'prev_page': publications.get('prev_page', None),
                      'search': publications.get('search', None),
                      'sort_by': sort_by,
                      'order_by': order_by,
                      'count': publications.get('count', None),
                      'debug': API_DEBUG
                  })


def publication_create(request):
    api_user = get_api_user(request=request)
    message = None
    if request.method == "POST":
        form = PublicationForm(request.POST)
        if form.is_valid():
            try:
                request.data = QueryDict('', mutable=True)
                data_dict = form.cleaned_data
                request.data.update(data_dict)
                p = PublicationViewSet(request=request)
                publication = p.create(request=request)
                if publication.status_code == status.HTTP_201_CREATED:
                    publication_uuid = json.loads(json.dumps(publication.data)).get('uuid')
                    return redirect('publication_detail', uuid=publication_uuid)
                else:
                    message = str(publication.data)
            except Exception as exc:
                message = str(exc)
        else:
            message = 'ValidationError: ' + str(form.errors.as_text())
    else:
        form = PublicationForm()
    return render(request,
                  'publication_create.html',
                  {
                      'form': form,
                      'message': message,
                      'api_user': api_user.as_dict(),
                  })


def publication_bulk_upload(request):
    """
    Admin-only bulk upload page.

    Posts here rather than to /api/publications/bulk on purpose. A multipart POST to
    /api/ is refused without an X-Requested-With header (server/middleware.py), and
    this page has something better available to it: it is a same-origin Django form,
    so {% csrf_token %} and CsrfViewMiddleware already apply. The viewset action is
    then called in process, the same way publication_create does.
    """
    api_user = get_api_user(request=request)
    message = None
    summary = None
    if request.method == 'POST':
        if not api_user.is_publication_tracker_admin:
            message = 'PermissionDenied: you do not have permission to bulk upload publications.'
        elif not request.FILES.get('file', None):
            message = 'Choose a .jsonl or .bib file to upload.'
        else:
            try:
                # The action reads request.FILES; there is no JSON body on this path.
                request.data = {}
                response = PublicationViewSet(request=request).bulk(request=request)
                if response.status_code == status.HTTP_200_OK:
                    summary = response.data
                else:
                    message = str(response.data)
            except Exception as exc:
                message = str(exc)
    return render(request,
                  'publication_bulk_upload.html',
                  {
                      'api_user': api_user.as_dict(),
                      'message': message,
                      'summary': summary,
                      'max_records': settings.BULK_MAX_RECORDS,
                      'max_upload_mb': settings.BULK_MAX_UPLOAD_BYTES // (1024 * 1024),
                      'debug': API_DEBUG,
                  })


def publication_update(request, *args, **kwargs):
    api_user = get_api_user(request=request)
    publication_uuid = kwargs.get('uuid')
    publication = get_object_or_404(Publication, uuid=publication_uuid)
    message = None
    if request.method == "POST" and isinstance(request.POST.get('save'), str):
        form = PublicationForm(request.POST, instance=publication)
        if form.is_valid():
            print('update pub')
            try:
                request.data = QueryDict('', mutable=True)
                data_dict = form.cleaned_data
                request.data.update(data_dict)
                a = PublicationViewSet(request=request)
                publication = a.update(request=request, uuid=publication_uuid)
                if publication.status_code == 200:
                    return redirect('publication_detail', uuid=publication_uuid)
                else:
                    message = str(publication.data)
            except Exception as exc:
                message = str(exc)
        else:
            message = 'ValidationError: ' + str(form.errors.as_text())
    else:
        author_names = []
        for author_uuid in publication.authors:
            try:
                author = Author.objects.get(uuid=author_uuid)
                author_names.append(author.author_name)
            except Author.DoesNotExist:
                author_names.append(author_uuid)
        form = PublicationForm(instance=publication, authors=author_names)
    return render(request,
                  'publication_update.html',
                  {
                      'api_user': api_user.as_dict(),
                      'publication_uuid': publication_uuid,
                      'debug': API_DEBUG,
                      'form': form,
                      'message': message
                  })


def publication_detail(request, *args, **kwargs):
    api_user = get_api_user(request=request)
    message = kwargs.get('message', None)
    if request.method == 'POST':
        try:
            publication_detail_button = request.POST.get('publication_detail_button', None)
            v_api_request = request.POST.copy()
            v_api_request.COOKIES = request.COOKIES
            v_api_request.headers = request.headers
            v_api_request.data = QueryDict('', mutable=True)
            if publication_detail_button == "delete_publication":
                publication_uuid = request.POST.get('publication_uuid', None)
                v_api_request.method = 'DELETE'
                v_api_request.data.update(
                    {
                        'uuid': publication_uuid
                    }
                )
                a = PublicationViewSet(request=v_api_request)
                publication = a.destroy(request=v_api_request)
                return redirect('publication_list')
            else:
                return redirect('publication_detail', uuid=kwargs.get('uuid'))

        except Exception as exc:
            message = exc
            print(message)
            return redirect('publication_detail', uuid=kwargs.get('uuid'))
    # get publication detail page when not method: POST
    try:
        publication = PublicationViewSet.as_view({'get': 'retrieve'})(request=request, *args, **kwargs)
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
                  'publication_detail.html',
                  {
                      'api_user': api_user.as_dict(),
                      'publication': publication,
                      'is_author': is_author,
                      'message': message,
                      'debug': API_DEBUG
                  })


def author_update(request, *args, **kwargs):
    api_user = get_api_user(request=request)
    author_uuid = kwargs.get('uuid')
    author = get_object_or_404(Author, uuid=author_uuid)
    message = None

    # The stored values, read before any form touches this row.
    #
    # AuthorForm is a ModelForm bound to `author`, and a ModelForm's _post_clean() writes
    # the submitted data onto its instance as part of is_valid(). Anything read off
    # `author` after that point is therefore the *new* value, not the old one, however
    # much the code below looks like it is comparing before with after. That is not
    # hypothetical: the publication_uuid comparison in the admin branch has always read
    # its "old" value after validation, so `old != new` was never true and the branch that
    # keeps Publication.authors consistent when an author moves between publications has
    # never run -- verified against a seeded database, where the move left auth-1 listed
    # on the publication it had left and absent from the one it joined.
    original_author_name = author.author_name
    original_publication_uuid = author.publication_uuid

    if not (api_user.can_create_publication or api_user.is_publication_tracker_admin):
        return render(request, 'author_update.html', {
            'api_user': api_user.as_dict(),
            'author': {
                'uuid': author.uuid,
                'author_name': author.author_name,
                'display_name': author.display_name,
                'fabric_uuid': author.fabric_uuid,
                'publication_uuid': author.publication_uuid,
            },
            'message': 'PermissionDenied: you do not have permission to edit authors.',
            'debug': API_DEBUG,
        })

    # Claiming an *unclaimed* author is the honor system, and is deliberate: author
    # strings on a paper are ambiguous ('Smith, J.' / 'Jane Smith' / 'J. Smith') and
    # self-assertion is the only signal available today. Taking a claim away from the
    # user who already made it is not part of that bargain -- the non-admin branch below
    # overwrites fabric_uuid unconditionally, so without this check any user holding
    # CAN_CREATE_PUBLICATION_ROLE could silently de-attribute someone else's work.
    # Admins keep the reassignment branch below as the escape hatch for a wrong claim.
    if not api_user.is_publication_tracker_admin and author.fabric_uuid \
            and author.fabric_uuid != api_user.uuid:
        return render(request, 'author_update.html', {
            'api_user': api_user.as_dict(),
            'author': {
                'uuid': author.uuid,
                'author_name': author.author_name,
                'display_name': author.display_name,
                'fabric_uuid': author.fabric_uuid,
                'publication_uuid': author.publication_uuid,
            },
            'message': 'PermissionDenied: this author is already claimed by another '
                       'FABRIC user. Contact a publication tracker admin if the claim '
                       'is incorrect.',
            'debug': API_DEBUG,
        })

    if request.method == "POST" and isinstance(request.POST.get('save'), str):
        form = AuthorForm(request.POST, instance=author, api_user=api_user)
        if form.is_valid():
            try:
                if api_user.is_publication_tracker_admin:
                    old_publication_uuid = original_publication_uuid
                    old_author_name = original_author_name
                    new_publication_uuid = form.cleaned_data['publication_uuid']
                    author.author_name = form.cleaned_data['author_name']
                    author.display_name = form.cleaned_data['display_name']
                    author.publication_uuid = new_publication_uuid
                    author.fabric_uuid = form.cleaned_data.get('fabric_uuid')
                    author.save()
                    # The ledger entry this path never had. An attribution typed in here
                    # is a decision exactly as much as one made on the queue is, and one
                    # that goes unrecorded leaves the queue offering suggestions for an
                    # author that is already attributed until the next scoring run.
                    record_admin_claim(author, author.fabric_uuid, decided_by=api_user)
                    if author.author_name != old_author_name:
                        # Suggestions were computed from the old spelling, so they are no
                        # longer about this author. The next scoring run recomputes them.
                        withdraw_suggestions(author)
                    # Keep publication.authors arrays consistent when publication_uuid changes
                    if old_publication_uuid != new_publication_uuid:
                        try:
                            old_pub = Publication.objects.get(uuid=old_publication_uuid)
                            if author.uuid in old_pub.authors:
                                old_pub.authors.remove(author.uuid)
                                old_pub.save()
                        except Publication.DoesNotExist:
                            pass
                        new_pub = Publication.objects.get(uuid=new_publication_uuid)
                        if author.uuid not in new_pub.authors:
                            new_pub.authors.append(author.uuid)
                            new_pub.save()
                else:
                    author.display_name = form.cleaned_data['display_name']
                    author.fabric_uuid = api_user.uuid
                    author.save()
                    # Option (a) on the issue: the self-claim stays immediate, and the
                    # ledger records that it happened. Migration 0003 backfilled the same
                    # row for every claim made before this existed.
                    record_self_claim(author, api_user)
                return redirect('publication_detail', uuid=author.publication_uuid)
            except Exception as exc:
                message = str(exc)
        else:
            message = 'ValidationError: ' + str(form.errors.as_text())
    else:
        form = AuthorForm(instance=author, api_user=api_user)

    return render(request, 'author_update.html', {
        'api_user': api_user.as_dict(),
        'author': {
            'uuid': author.uuid,
            'author_name': author.author_name,
            'display_name': author.display_name,
            'fabric_uuid': author.fabric_uuid,
            'publication_uuid': author.publication_uuid,
        },
        'author_uuid': author_uuid,
        'form': form,
        'message': message,
        'debug': API_DEBUG,
    })


# Statuses the claim queue can be filtered to, in the order the tabs appear. 'suggested'
# is the queue proper -- the rest are the ledger, and they are reachable because a
# rejection nobody can look up afterwards is no better than a deleted one.
CLAIM_QUEUE_TABS = (
    (AuthorClaim.SUGGESTED, 'Suggested'),
    (AuthorClaim.APPROVED, 'Approved'),
    (AuthorClaim.REJECTED, 'Rejected'),
    (AuthorClaim.SELF_ASSERTED, 'Self-asserted'),
)


def _claim_queue_page(status, page_number, page_size):
    """
    One page of the claim queue, grouped by author and best-scoring author first.

    Pagination is over *authors*, not claim rows, because an author is the unit an admin
    decides: "is this Smith, J. one of these five people?" is one judgement made with all
    five in front of you, and paginating rows would scatter those five across pages and
    invite approving two of them. Ordering by each author's best score puts the
    high-confidence decisions -- the ones that go quickly -- first.
    """
    authors_page = Author.objects.filter(
        claims__status=status
    ).annotate(
        best_score=models.Max('claims__score', filter=Q(claims__status=status)),
    ).order_by('-best_score', 'author_name', 'id')

    paginator = Paginator(authors_page, page_size)
    page_obj = paginator.get_page(page_number)
    authors = list(page_obj.object_list)

    # One query for the claims of every author on this page, rather than one per author.
    claims_by_author = {}
    for claim in AuthorClaim.objects.filter(
        author__in=authors, status=status
    ).select_related('api_user', 'decided_by').order_by('-score', 'api_user__name'):
        claims_by_author.setdefault(claim.author_id, []).append(claim)

    # Author carries publication_uuid as a plain string, so there is no select_related to
    # lean on here either.
    publications = {
        p.uuid: p for p in Publication.objects.filter(
            uuid__in={a.publication_uuid for a in authors}
        )
    }

    groups = [
        {
            'author': author,
            'publication': publications.get(author.publication_uuid),
            'claims': claims_by_author.get(author.id, []),
        }
        for author in authors
    ]
    return groups, page_obj, paginator.count


def author_claim_list(request):
    """
    The admin claim queue (#32): scored (author, FABRIC user) suggestions, approve/reject.

    Gated in-view rather than by a decorator, the same way the rest of this module is:
    there is no Django auth session here, only the ApiUser resolved from the FABRIC cookie
    or token, so `is_publication_tracker_admin` is the check and a non-admin gets the page
    with an explanation rather than a 403 they cannot act on.

    POST decides one claim and re-renders rather than redirecting, which is what
    publication_bulk_upload does. A refresh therefore re-posts, and that is safe by
    construction: both decisions refuse a claim that is already decided, so the second
    POST reports "already approved" and changes nothing.
    """
    api_user = get_api_user(request=request)
    message = None
    notice = None

    status = request.GET.get('status') or AuthorClaim.SUGGESTED
    if status not in dict(CLAIM_QUEUE_TABS):
        status = AuthorClaim.SUGGESTED

    if not api_user.is_publication_tracker_admin:
        return render(request, 'publications/author_claim_list.html', {
            'api_user': api_user.as_dict(),
            'groups': [],
            'message': 'PermissionDenied: you do not have permission to review author '
                       'claims.',
            'status': status,
            'tabs': CLAIM_QUEUE_TABS,
            'count': 0,
            'item_range': '0 - 0',
            'next_page': None,
            'prev_page': None,
            'debug': API_DEBUG,
        })

    if request.method == 'POST':
        action = request.POST.get('action')
        claim = AuthorClaim.objects.select_related('author', 'api_user').filter(
            uuid=request.POST.get('claim_uuid') or ''
        ).first()
        if claim is None:
            message = 'That claim no longer exists. It may have been withdrawn by a '\
                      'scoring run, or decided in another tab.'
        elif action not in ('approve', 'reject'):
            message = 'Unknown action: choose Approve or Reject.'
        else:
            try:
                if action == 'approve':
                    withdrawn = approve_suggestion(claim, decided_by=api_user)
                    notice = 'Approved: "{0}" is now attributed to {1}.'.format(
                        claim.author.author_name, claim.api_user.name or claim.api_user.uuid
                    )
                    if withdrawn:
                        notice += ' {0} other suggestion(s) for that author withdrawn.'.format(
                            withdrawn
                        )
                else:
                    reject_suggestion(claim, decided_by=api_user)
                    notice = 'Rejected: {0} is not "{1}". The pair will not be suggested '\
                             'again.'.format(
                                 claim.api_user.name or claim.api_user.uuid,
                                 claim.author.author_name,
                             )
            except Exception as exc:
                # ClaimDecisionError -- a refusal the admin needs to read, such as an
                # author someone else already holds -- carries a message written for
                # exactly this. Anything else is reported the way the rest of this module
                # reports an unexpected failure.
                message = str(exc)

    groups = []
    count = 0
    item_range = '0 - 0'
    prev_page = next_page = None
    try:
        page_size = int(REST_FRAMEWORK['PAGE_SIZE'])
        current_page = int(request.GET.get('page', 1))
        groups, page_obj, count = _claim_queue_page(status, current_page, page_size)
        min_range = (current_page - 1) * page_size + 1 if count else 0
        max_range = min(current_page * page_size, count)
        item_range = '{0} - {1}'.format(min_range, max_range)
        prev_page = page_obj.previous_page_number() if page_obj.has_previous() else None
        next_page = page_obj.next_page_number() if page_obj.has_next() else None
    except Exception as exc:
        message = message or str(exc)

    return render(request, 'publications/author_claim_list.html', {
        'api_user': api_user.as_dict(),
        'groups': groups,
        'message': message,
        'notice': notice,
        'status': status,
        'tabs': CLAIM_QUEUE_TABS,
        'count': count,
        'item_range': item_range,
        'next_page': next_page,
        'prev_page': prev_page,
        'debug': API_DEBUG,
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
        if request.GET.get('sort_by'):
            data_dict['sort_by'] = request.GET.get('sort_by')
        if request.GET.get('order_by'):
            data_dict['order_by'] = request.GET.get('order_by')
        if request.GET.get('page'):
            data_dict['page'] = request.GET.get('page')
            current_page = int(request.GET.get('page'))
        request.query_params = QueryDict('', mutable=True)
        request.query_params.update(data_dict)
        if object_type == ListObjectType.PUBLICATIONS:
            list_objects = PublicationViewSet.as_view({'get': 'list'})(request=request)
        elif object_type == ListObjectType.AUTHORS:
            list_objects = AuthorViewSet.as_view({'get': 'list'})(request=request)
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


def publication_project_list(request):
    api_user = get_api_user(request=request)
    message = None
    search = None
    projects_page = []
    count = 0
    item_range = '0 - 0'
    prev_page = None
    next_page = None

    try:
        page_size = int(REST_FRAMEWORK['PAGE_SIZE'])
        search = request.GET.get('search', None)
        base_qs = Publication.objects.exclude(
            project_uuid__isnull=True
        ).exclude(
            project_uuid=''
        )
        if search and len(search) >= 3:
            base_qs = base_qs.filter(project_name__icontains=search)
        elif search:
            base_qs = Publication.objects.none()
        queryset = base_qs.values('project_uuid', 'project_name').annotate(
            pub_count=Count('uuid')
        ).order_by('project_name')
        count = queryset.count()
        paginator = Paginator(queryset, page_size)
        current_page = int(request.GET.get('page', 1))
        page_obj = paginator.get_page(current_page)
        projects_page = page_obj.object_list
        min_range = (current_page - 1) * page_size + 1
        max_range = min(current_page * page_size, count)
        item_range = '{0} - {1}'.format(min_range, max_range)
        prev_page = page_obj.previous_page_number() if page_obj.has_previous() else None
        next_page = page_obj.next_page_number() if page_obj.has_next() else None
    except Exception as exc:
        message = str(exc)

    return render(request,
                  'publications/publication_project_list.html',
                  {
                      'api_user': api_user.as_dict(),
                      'projects': projects_page,
                      'item_range': item_range,
                      'message': message,
                      'next_page': next_page,
                      'prev_page': prev_page,
                      'search': search,
                      'count': count,
                      'debug': API_DEBUG,
                  })


def publication_project_detail(request, *args, **kwargs):
    api_user = get_api_user(request=request)
    project_uuid = kwargs.get('project_uuid')
    message = None
    publications = []
    count = 0
    item_range = '0 - 0'
    prev_page = None
    next_page = None
    search = None
    project_name = None

    try:
        page_size = int(REST_FRAMEWORK['PAGE_SIZE'])
        search = request.GET.get('search', None)
        base_qs = Publication.objects.filter(project_uuid=project_uuid).order_by('title')
        if search and len(search) >= 3:
            base_qs = base_qs.filter(
                Q(title__icontains=search) | Q(project_name__icontains=search)
            )
        elif search:
            base_qs = Publication.objects.none()
        count = base_qs.count()
        paginator = Paginator(base_qs, page_size)
        current_page = int(request.GET.get('page', 1))
        page_obj = paginator.get_page(current_page)
        publications = PublicationSerializer(page_obj.object_list, many=True).data
        min_range = (current_page - 1) * page_size + 1
        max_range = min(current_page * page_size, count)
        item_range = '{0} - {1}'.format(min_range, max_range)
        prev_page = page_obj.previous_page_number() if page_obj.has_previous() else None
        next_page = page_obj.next_page_number() if page_obj.has_next() else None
        first_pub = Publication.objects.filter(project_uuid=project_uuid).first()
        if first_pub:
            project_name = first_pub.project_name
    except Exception as exc:
        message = str(exc)

    return render(request,
                  'publications/publication_project_detail.html',
                  {
                      'api_user': api_user.as_dict(),
                      'publications': publications,
                      'project_uuid': project_uuid,
                      'project_name': project_name,
                      'item_range': item_range,
                      'message': message,
                      'next_page': next_page,
                      'prev_page': prev_page,
                      'search': search,
                      'count': count,
                      'debug': API_DEBUG,
                  })


def publication_author_detail(request, *args, **kwargs):
    api_user = get_api_user(request=request)
    fabric_uuid = kwargs.get('fabric_uuid')
    message = None
    publications = []
    count = 0
    item_range = '0 - 0'
    prev_page = None
    next_page = None
    search = None
    author_name = None

    try:
        page_size = int(REST_FRAMEWORK['PAGE_SIZE'])
        search = request.GET.get('search', None)
        pub_uuids = Author.objects.filter(
            fabric_uuid=fabric_uuid
        ).values_list('publication_uuid', flat=True).distinct()
        base_qs = Publication.objects.filter(uuid__in=pub_uuids).order_by('title')
        if search and len(search) >= 3:
            base_qs = base_qs.filter(
                Q(title__icontains=search) | Q(project_name__icontains=search)
            )
        elif search:
            base_qs = Publication.objects.none()
        count = base_qs.count()
        paginator = Paginator(base_qs, page_size)
        current_page = int(request.GET.get('page', 1))
        page_obj = paginator.get_page(current_page)
        publications = PublicationSerializer(page_obj.object_list, many=True).data
        min_range = (current_page - 1) * page_size + 1
        max_range = min(current_page * page_size, count)
        item_range = '{0} - {1}'.format(min_range, max_range)
        prev_page = page_obj.previous_page_number() if page_obj.has_previous() else None
        next_page = page_obj.next_page_number() if page_obj.has_next() else None
        first_author = Author.objects.filter(fabric_uuid=fabric_uuid).first()
        if first_author:
            author_name = first_author.display_name
    except Exception as exc:
        message = str(exc)

    return render(request,
                  'publications/publication_author_detail.html',
                  {
                      'api_user': api_user.as_dict(),
                      'publications': publications,
                      'fabric_uuid': fabric_uuid,
                      'author_name': author_name,
                      'item_range': item_range,
                      'message': message,
                      'next_page': next_page,
                      'prev_page': prev_page,
                      'search': search,
                      'count': count,
                      'debug': API_DEBUG,
                  })

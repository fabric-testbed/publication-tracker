import os
from collections.abc import Mapping

from django.core.exceptions import ValidationError
from django.core.validators import URLValidator

from publicationtrkr.apps.apiuser.models import ApiUser
from publicationtrkr.apps.publications.models import Author, Publication
from publicationtrkr.apps.publications.utils.bibtex_utils import parse_bibtex
from publicationtrkr.utils.core_api import query_core_api_by_cookie, query_core_api_by_token
from publicationtrkr.utils.fabric_auth import is_valid_uuid


def is_http_url(link: str) -> bool:
    """
    Return True when link is a well formed http:// or https:// URL.

    Any other scheme - notably 'javascript:' - is rejected: link is rendered
    into an href attribute, where an active scheme runs script in the page
    origin regardless of target="_blank".
    """
    try:
        URLValidator(schemes=['http', 'https'])(str(link).strip())
    except ValidationError:
        return False
    return True


def query_project(request, api_user: ApiUser, project_uuid: str) -> dict:
    """
    Ask core-api about one project, as whoever is making this request. Shared by the
    create and update validators, which asked in identical but separately written ways.
    """
    cache = getattr(request, '_publication_project_cache', None)
    if cache is None:
        cache = request._publication_project_cache = {}
    key = (api_user.uuid, api_user.access_type, project_uuid)
    if key not in cache:
        if api_user.access_type == ApiUser.COOKIE:
            cache[key] = query_core_api_by_cookie(
                query='/projects/{0}'.format(project_uuid),
                cookie=request.COOKIES.get(os.getenv('VOUCH_COOKIE_NAME'), None))
        else:
            cache[key] = query_core_api_by_token(
                query='/projects/{0}'.format(project_uuid),
                token=request.headers.get('authorization', 'Bearer ').replace('Bearer ', ''))
    return cache[key]


def validate_publication_data(data, bibtex_data=None, *, required=True) -> list:
    """
    Field-level checks on one publication payload, independent of the request that
    carried it. Returns a list of {field: message} dicts; empty means valid.

    `required` is the difference between create (title, authors and year must resolve
    from the payload or its BibTeX) and update (every field optional, but an explicitly
    empty author list is still refused).

    Nothing here touches the network. The core-api project-existence check stays with
    the caller because who makes it differs: the single-record paths make it once per
    request, and the bulk path makes it once per distinct project uuid through a memo
    rather than once per record.
    """
    if not isinstance(data, Mapping):
        return [{'payload': 'must be a JSON object'}]
    message = []
    bibtex_data = bibtex_data or {}

    # Validate supplied types even when falsy: False and 0 are not omitted strings.
    # Null/empty strings retain the existing fallback/no-op update semantics.
    string_fields = ('bibtex', 'link', 'project_name', 'project_uuid', 'title', 'venue', 'year')
    for field in string_fields:
        value = data.get(field)
        if value is None or value == '':
            value = bibtex_data.get(field)
        if value is None:
            continue
        if not isinstance(value, str):
            message.append({field: 'must be a string'})
            continue
        maximum = Publication._meta.get_field(field).max_length
        if len(value) > maximum:
            message.append({field: 'must be at most {0} characters'.format(maximum)})
        if field in ('title', 'year') and value and not value.strip():
            message.append({field: 'must not be blank'})
        if field == 'link' and value and not is_http_url(value):
            message.append({'link': 'must be an http:// or https:// URL'})

    authors = data.get('authors')
    if authors is not None and not isinstance(authors, list):
        message.append({'authors': 'must be a list of non-empty strings'})
    elif authors == [] and not required:
        message.append({'authors': 'must provide at least one author'})
    else:
        authors = authors or bibtex_data.get('authors')
        if not authors:
            if required:
                message.append({'authors': 'must provide at least one author'})
        elif not isinstance(authors, list) or any(
            not isinstance(name, str) or not name.strip() for name in authors
        ):
            message.append({'authors': 'must be a list of non-empty strings'})
        elif any(len(name) > Author._meta.get_field('author_name').max_length for name in authors):
            message.append({'authors': 'each author must be at most 255 characters'})

    project_name = data.get('project_name')
    project_uuid = data.get('project_uuid')
    if project_name and not project_uuid:
        message.append({'project_name': 'must also provide a project_uuid when providing a project_name'})
    if isinstance(project_uuid, str) and project_uuid and not is_valid_uuid(project_uuid):
        message.append({'project_uuid': 'must be a valid UUID'})

    if required:
        for field in ('title', 'year'):
            if not data.get(field) and not bibtex_data.get(field):
                # A wrong type has its own actionable error above.
                if not any(field in error for error in message):
                    message.append({field: 'must provide a {0}'.format(field)})
    return message


def _validate_publication_request(request, api_user, *, required):
    data = request.data
    bibtex = data.get('bibtex') if isinstance(data, Mapping) else None
    bibtex_data = parse_bibtex(bibtex) if isinstance(bibtex, str) and bibtex else {}
    message = validate_publication_data(data, bibtex_data, required=required)
    if message:
        return False, message
    project_uuid = data.get('project_uuid')
    if project_uuid:
        try:
            project = query_project(request, api_user, project_uuid)
            results = project.get('results')
            if (project.get('size') != 1 or project.get('status') != 200
                    or not isinstance(results, list) or len(results) != 1
                    or not isinstance(results[0], dict)
                    or not isinstance(results[0].get('name'), str) or not results[0]['name'].strip()):
                message.append({'project_uuid': "unable to find project: '{0}'".format(project_uuid)})
            elif data.get('project_name') and data['project_name'] != results[0]['name']:
                message.append({'project_name': "does not match name found for project_uuid: '{0}'".format(project_uuid)})
        except Exception as exc:
            message.append({'APIException': str(exc)})
    return (False, message) if message else (True, None)


def validate_publication_create(request, api_user: ApiUser) -> tuple:
    """
    POST /api/publications
    - 'authors': ['string', ...] - required (or provided via bibtex)
    - 'bibtex': 'string' - optional
    - 'link': 'string' - optional
    - 'project_name': 'string' - optional
    - 'project_uuid': 'string' - optional
    - 'title': 'string' - required (or provided via bibtex)
    - 'venue': 'string' - optional
    - 'year': 'string' - required (or provided via bibtex)
    """
    return _validate_publication_request(request, api_user, required=True)


def validate_publication_update(request, api_user: ApiUser) -> tuple:
    """
    PUT/PATCH /api/publications/{uuid}
    - 'authors': ['string', ...] - optional
    - 'bibtex': 'string' - optional
    - 'link': 'string' - optional
    - 'project_name': 'string' - optional
    - 'project_uuid': 'string' - optional
    - 'title': 'string' - optional
    - 'venue': 'string' - optional
    - 'year': 'string' - optional
    """
    return _validate_publication_request(request, api_user, required=False)

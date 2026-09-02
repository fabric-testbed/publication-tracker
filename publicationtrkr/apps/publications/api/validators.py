import os

from django.core.exceptions import ValidationError
from django.core.validators import URLValidator

from publicationtrkr.apps.apiuser.models import ApiUser
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
    if api_user.access_type == ApiUser.COOKIE:
        return query_core_api_by_cookie(
            query='/projects/{0}'.format(project_uuid),
            cookie=request.COOKIES.get(os.getenv('VOUCH_COOKIE_NAME'), None))
    return query_core_api_by_token(
        query='/projects/{0}'.format(project_uuid),
        token=request.headers.get('authorization', 'Bearer ').replace('Bearer ', ''))


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
    message = []
    bibtex_data = bibtex_data or {}

    # 'authors': ['string', ...] - required on create, but never an empty list
    authors = data.get('authors', None)
    if required:
        if not authors and not bibtex_data.get('authors'):
            message.append({'authors': 'must provide at least one author'})
    elif authors == []:
        message.append({'authors': 'must provide at least one author'})

    # 'link': 'string' - optional (check payload and bibtex)
    link = data.get('link', None)
    if not link:
        link = bibtex_data.get('link', None)
    if link and not is_http_url(link):
        message.append({'link': 'must be an http:// or https:// URL'})

    # 'project_name' / 'project_uuid': 'string' - optional, but paired
    project_name = data.get('project_name', None)
    project_uuid = data.get('project_uuid', None)
    if project_name and not project_uuid:
        message.append({'project_name': 'must also provide a project_uuid when providing a project_name'})
    if project_uuid and not is_valid_uuid(project_uuid):
        # project_uuid is interpolated into an outbound core-api request path. Unvalidated,
        # a value like '../people/<uuid>' reaches a different endpoint, whose 'name' would
        # then be stored as this publication's project_name and served to anonymous readers.
        message.append({'project_uuid': 'must be a valid UUID'})

    if required:
        # 'title': 'string' - required (check payload and bibtex)
        if not data.get('title', None) and not bibtex_data.get('title'):
            message.append({'title': 'must provide a title'})
        # 'venue': 'string' - optional, no constraint to check
        # 'year': 'string' - required (check payload and bibtex)
        if not data.get('year', None) and not bibtex_data.get('year'):
            message.append({'year': 'must provide a year'})

    return message


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
    message = []
    try:
        request_data = request.data
        bibtex = request_data.get('bibtex', None)
        bibtex_data = parse_bibtex(bibtex) if bibtex else {}
        message = validate_publication_data(request_data, bibtex_data, required=True)
        # verify the project exists, once the uuid is known to be well formed
        project_uuid = request_data.get('project_uuid', None)
        if project_uuid and is_valid_uuid(project_uuid):
            fab_project = query_project(request, api_user, project_uuid)
            if fab_project.get('size') != 1 or fab_project.get('status') != 200:
                message.append({'project_uuid': 'unable to find project: \'{0}\''.format(project_uuid)})
    except Exception as exc:
        message.append({'APIException': exc})
    if len(message) > 0:
        return False, message
    else:
        return True, None


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
    message = []
    try:
        request_data = request.data
        bibtex = request_data.get('bibtex', None)
        bibtex_data = parse_bibtex(bibtex) if bibtex else {}
        message = validate_publication_data(request_data, bibtex_data, required=False)
        # verify the project exists and, if a name was given, that it is the right one
        project_name = request_data.get('project_name', None)
        project_uuid = request_data.get('project_uuid', None)
        if project_uuid and is_valid_uuid(project_uuid):
            fab_project = query_project(request, api_user, project_uuid)
            if fab_project.get('size') != 1 or fab_project.get('status') != 200:
                message.append({'project_uuid': 'unable to find project: \'{0}\''.format(project_uuid)})
            if project_name and project_name != fab_project.get('results')[0].get('name'):
                message.append({'project_name': 'does not match name found for project_uuid: \'{0}\''.format(project_uuid)})
    except Exception as exc:
        message.append({'APIException': exc})
    if len(message) > 0:
        return False, message
    else:
        return True, None

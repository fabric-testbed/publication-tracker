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
        # parse bibtex if provided
        bibtex = request_data.get('bibtex', None)
        bibtex_data = {}
        if bibtex:
            bibtex_data = parse_bibtex(bibtex)
        # 'authors': ['string', ...] - required (check request data and bibtex)
        authors = request_data.get('authors', [])
        if authors == [] and not bibtex_data.get('authors'):
            message.append({'authors': 'must provide at least one author'})
        # 'link': 'string' - optional (check request data and bibtex)
        link = request_data.get('link', None)
        if not link:
            link = bibtex_data.get('link', None)
        if link and not is_http_url(link):
            message.append({'link': 'must be an http:// or https:// URL'})
        # 'project_name': 'string' - optional
        project_name = request_data.get('project_name', None)
        # 'project_uuid': 'string' - optional
        project_uuid = request_data.get('project_uuid', None)
        if project_name and not project_uuid:
            message.append({'project_name': 'must also provide a project_uuid when providing a project_name'})
        if project_uuid and not is_valid_uuid(project_uuid):
            # project_uuid is interpolated into an outbound core-api request path below.
            # Unvalidated, a value like '../people/<uuid>' reaches a different endpoint,
            # whose 'name' would then be stored as this publication's project_name and
            # served to anonymous readers. by_project_uuid (viewsets.py) already
            # validated; create and update did not.
            message.append({'project_uuid': 'must be a valid UUID'})
        elif project_uuid:
            # verify project exists - get project_name if not provided in request
            if api_user.access_type == ApiUser.COOKIE:
                fab_project = query_core_api_by_cookie(
                    query='/projects/{0}'.format(project_uuid),
                    cookie=request.COOKIES.get(os.getenv('VOUCH_COOKIE_NAME'), None))
            else:
                fab_project = query_core_api_by_token(
                    query='/projects/{0}'.format(project_uuid),
                    token=request.headers.get('authorization', 'Bearer ').replace('Bearer ', ''))
            if fab_project.get('size') != 1 or fab_project.get('status') != 200:
                message.append({'project_uuid': 'unable to find project: \'{0}\''.format(project_uuid)})
            if not project_name:
                project_name = fab_project.get('name')
        # 'title': 'string' - required (check request data and bibtex)
        title = request_data.get('title', None)
        if not title and not bibtex_data.get('title'):
            message.append({'title': 'must provide a title'})
        # 'venue': 'string' - optional, no constraint to check
        # 'year': 'string' - required (check request data and bibtex)
        year = request_data.get('year', None)
        if not year and not bibtex_data.get('year'):
            message.append({'year': 'must provide a year'})
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
        # 'authors': ['string', ...] - optional, but cannot be empty list
        authors = request_data.get('authors', None)
        if authors == []:
            message.append({'authors': 'must provide at least one author'})
        # 'link': 'string' - optional (check request data and bibtex)
        link = request_data.get('link', None)
        if not link:
            bibtex = request_data.get('bibtex', None)
            if bibtex:
                link = parse_bibtex(bibtex).get('link', None)
        if link and not is_http_url(link):
            message.append({'link': 'must be an http:// or https:// URL'})
        # 'project_name': 'string' - optional
        project_name = request_data.get('project_name', None)
        # 'project_uuid': 'string' - optional
        project_uuid = request_data.get('project_uuid', None)
        if project_name and not project_uuid:
            message.append({'project_name': 'must also provide a project_uuid when providing a project_name'})
        if project_uuid and not is_valid_uuid(project_uuid):
            # See validate_publication_create() for why this is checked before the value
            # reaches an outbound request path.
            message.append({'project_uuid': 'must be a valid UUID'})
        elif project_uuid:
            # verify project exists - get project_name if not provided in request
            if api_user.access_type == ApiUser.COOKIE:
                fab_project = query_core_api_by_cookie(
                    query='/projects/{0}'.format(project_uuid),
                    cookie=request.COOKIES.get(os.getenv('VOUCH_COOKIE_NAME'), None))
            else:
                fab_project = query_core_api_by_token(
                    query='/projects/{0}'.format(project_uuid),
                    token=request.headers.get('authorization', 'Bearer ').replace('Bearer ', ''))
            if fab_project.get('size') != 1 or fab_project.get('status') != 200:
                message.append({'project_uuid': 'unable to find project: \'{0}\''.format(project_uuid)})
            if project_name and project_name != fab_project.get('results')[0].get('name'):
                message.append({'project_name': 'does not match name found for project_uuid: \'{0}\''.format(project_uuid)})
        # 'title', 'venue' and 'year' are optional on update with no constraint to
        # check, so there is nothing to read here.
    except Exception as exc:
        message.append({'APIException': exc})
    if len(message) > 0:
        return False, message
    else:
        return True, None

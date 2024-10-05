import os

from publicationtrkr.apps.apiuser.models import ApiUser
from publicationtrkr.utils.core_api import query_core_api_by_cookie, query_core_api_by_token


def validate_pubsimple_create(request, api_user: ApiUser) -> tuple:
    """
    POST /api/pubsimple
    - 'authors': ['string', ...] - required
    - 'link': 'string' - optional
    - 'project_name': 'string' - optional
    - 'project_uuid': 'string' - optional
    - 'title': 'string' - required
    - 'year': 'string' - required
    """
    message = []
    try:
        request_data = request.data
        # 'authors': ['string', ...] - required
        authors = request_data.get('authors', [])
        if authors == []:
            message.append({'authors': 'must provide at least one author'})
        # 'link': 'string' - optional
        link = request_data.get('link', None)
        # 'project_name': 'string' - optional
        project_name = request_data.get('project_name', None)
        # 'project_uuid': 'string' - optional
        project_uuid = request_data.get('project_uuid', None)
        if project_name and not project_uuid:
            message.append({'project_name': 'must also provide a project_uuid when providing a project_name'})
        if project_uuid:
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
        # 'title': 'string' - required
        title = request_data.get('title', None)
        if not title:
            message.append({'title': 'must provide a title'})
        # 'year': 'string' - required
        year = request_data.get('year', None)
        if not year:
            message.append({'year': 'must provide a year'})
    except Exception as exc:
        message.append({'APIException': exc})
    if len(message) > 0:
        return False, message
    else:
        return True, None


def validate_pubsimple_update(request, api_user: ApiUser) -> tuple:
    """
    PUT/PATCH /api/pubsimple/{uuid}
    - 'authors': ['string', ...] - optional
    - 'link': 'string' - optional
    - 'project_name': 'string' - optional
    - 'project_uuid': 'string' - optional
    - 'title': 'string' - optional
    - 'year': 'string' - optional
    """
    message = []
    try:
        request_data = request.data
        # 'authors': ['string', ...] - optional, but cannot be empty list
        authors = request_data.get('authors', None)
        if authors == []:
            message.append({'authors': 'must provide at least one author'})
        # 'link': 'string' - optional
        link = request_data.get('link', None)
        # 'project_name': 'string' - optional
        project_name = request_data.get('project_name', None)
        # 'project_uuid': 'string' - optional
        project_uuid = request_data.get('project_uuid', None)
        if project_name and not project_uuid:
            message.append({'project_name': 'must also provide a project_uuid when providing a project_name'})
        if project_uuid:
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
        # 'title': 'string' - optional
        title = request_data.get('title', None)
        # 'year': 'string' - optional
        year = request_data.get('year', None)
    except Exception as exc:
        message.append({'APIException': exc})
    if len(message) > 0:
        return False, message
    else:
        return True, None

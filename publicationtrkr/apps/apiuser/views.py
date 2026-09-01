from django.core.paginator import Paginator
from django.db.models import Q
from django.shortcuts import get_object_or_404, render

from publicationtrkr.apps.apiuser.models import ApiUser
from publicationtrkr.server.settings import API_DEBUG, REST_FRAMEWORK
from publicationtrkr.utils.fabric_auth import get_api_user


def apiuser_list(request):
    api_user = get_api_user(request=request)
    message = None
    search = None
    api_users_page = []
    count = 0
    item_range = '0 - 0'
    prev_page = None
    next_page = None

    if api_user.is_publication_tracker_admin:
        try:
            page_size = int(REST_FRAMEWORK['PAGE_SIZE'])
            queryset = ApiUser.objects.all().order_by('name')
            search = request.GET.get('search', None)
            if search and len(search) >= 3:
                queryset = queryset.filter(
                    Q(name__icontains=search) |
                    Q(email__icontains=search) |
                    Q(affiliation__icontains=search)
                )
            elif search:
                queryset = ApiUser.objects.none()
            count = queryset.count()
            paginator = Paginator(queryset, page_size)
            current_page = int(request.GET.get('page', 1))
            page_obj = paginator.get_page(current_page)
            api_users_page = page_obj.object_list
            min_range = (current_page - 1) * page_size + 1
            max_range = min(current_page * page_size, count)
            item_range = '{0} - {1}'.format(min_range, max_range)
            prev_page = page_obj.previous_page_number() if page_obj.has_previous() else None
            next_page = page_obj.next_page_number() if page_obj.has_next() else None
        except Exception as exc:
            message = str(exc)

    return render(request,
                  'apiuser/apiuser_list.html',
                  {
                      'api_user': api_user.as_dict(),
                      'api_users': api_users_page,
                      'item_range': item_range,
                      'message': message,
                      'next_page': next_page,
                      'prev_page': prev_page,
                      'search': search,
                      'count': count,
                      'debug': API_DEBUG,
                  })


def apiuser_detail(request, *args, **kwargs):
    api_user = get_api_user(request=request)
    # Enforce the admin gate in the view, not only in the template. apiuser_detail.html
    # hides the record behind {% if %}, but the record was fetched and passed into the
    # context regardless -- one template edit away from disclosing another user's email,
    # cilogon_id, roles and project membership. apiuser_list already gates in the view;
    # this matches it.
    if not api_user.is_publication_tracker_admin:
        return render(request,
                      'apiuser/apiuser_detail.html',
                      {
                          'api_user': api_user.as_dict(),
                          'apiuser': None,
                          'message': 'PermissionDenied: you do not have permission to '
                                     'view API users.',
                          'debug': API_DEBUG,
                      })
    apiuser = get_object_or_404(ApiUser, uuid=kwargs.get('uuid'))
    return render(request,
                  'apiuser/apiuser_detail.html',
                  {
                      'api_user': api_user.as_dict(),
                      'apiuser': apiuser,
                      'debug': API_DEBUG,
                  })

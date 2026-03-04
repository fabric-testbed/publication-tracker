import os

from django.shortcuts import redirect, render

from publicationtrkr.utils.fabric_auth import get_api_user


def landing_page(request):
    api_user = get_api_user(request=request)
    return render(request,
                  'home.html',
                  {
                      'api_user': api_user.as_dict()
                  })


def logout_view(request):
    response = redirect('home')
    vouch_cookie_name = os.getenv('VOUCH_COOKIE_NAME', '')
    if vouch_cookie_name:
        response.delete_cookie(vouch_cookie_name)
    return response
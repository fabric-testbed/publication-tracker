from django.shortcuts import render
from publicationtrkr.utils.fabric_auth import get_api_user


def landing_page(request):
    api_user = get_api_user(request=request)
    return render(request,
                  'home.html',
                  {
                      'api_user': api_user.as_dict()
                  })
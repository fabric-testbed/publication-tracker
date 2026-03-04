from django.urls import path

from publicationtrkr.apps.apiuser.views import apiuser_detail, apiuser_list

urlpatterns = [
    path('', apiuser_list, name='apiuser_list'),
    path('<uuid>', apiuser_detail, name='apiuser_detail'),
]

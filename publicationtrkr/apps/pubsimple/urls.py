"""
URL configuration for publicationtrkr project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/5.1/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.urls import path

# from artifactmgr.apps.artifacts.views import artifact_create, artifact_detail, artifact_list, artifact_update, \
#     author_detail, author_list

from publicationtrkr.apps.pubsimple.views import pubsimple_list, pubsimple_create, pubsimple_detail, pubsimple_update

urlpatterns = [
    path('', pubsimple_list, name='pubsimple_list'),
    path('create/', pubsimple_create, name='pubsimple_create'),
    path('<uuid>', pubsimple_detail, name='pubsimple_detail'),
    path('<uuid>/update', pubsimple_update, name='pubsimple_update'),
    # path('people/', author_list, name='author_list'),
    # path('people/<uuid>', author_detail, name='author_detail'),
]

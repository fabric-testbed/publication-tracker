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
from django.contrib import admin
from django.urls import path, include
from drf_spectacular.views import SpectacularAPIView, SpectacularRedocView, SpectacularSwaggerView
from rest_framework import routers

from publicationtrkr.server.views import landing_page, logout_view

from publicationtrkr.apps.publications.api.viewsets import AuthorViewSet, PublicationViewSet

router = routers.DefaultRouter(trailing_slash=False)
router.register(r'authors', AuthorViewSet, basename='authors')
router.register(r'publications', PublicationViewSet, basename='publications')

urlpatterns = [
    path('', landing_page, name='home'),
    path('logout', logout_view, name='logout'),
    path('apiusers/', include('publicationtrkr.apps.apiuser.urls')),
    path('publications/', include('publicationtrkr.apps.publications.urls')),
    path('admin/', admin.site.urls),
    path('api/', include((router.urls, 'publicationtrkr.apps'))),
    # 'api-auth/' (DRF's browsable-API login) is deliberately absent: it exposed
    # unthrottled password authentication on the internet and nothing referenced it.
    # '/admin/' stays reachable -- no superuser is provisioned by this repo or any
    # fixture, so there is no account to attack; restricting it by network is recorded
    # as an accepted residual rather than done here.
    path('api/schema/', SpectacularAPIView.as_view(), name='schema'),
    # Optional UI:
    path('api/swagger/', SpectacularSwaggerView.as_view(url_name='schema'), name='swagger-ui'),
    path('api/redoc/', SpectacularRedocView.as_view(url_name='schema'), name='redoc'),
]

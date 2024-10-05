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
from django.views.generic.base import TemplateView
from drf_spectacular.views import SpectacularAPIView, SpectacularRedocView, SpectacularSwaggerView
from rest_framework import routers

from publicationtrkr.server.views import landing_page

from publicationtrkr.apps.pubsimple.api.viewsets import PubSimpleViewSet

router = routers.DefaultRouter(trailing_slash=False)
router.register(r'pubsimple', PubSimpleViewSet, basename='pubsimple')

urlpatterns = [
    path('', landing_page, name='home'),
    path('pubsimple/', include('publicationtrkr.apps.pubsimple.urls')),
    path('admin/', admin.site.urls),
    path('api/', include((router.urls, 'publicationtrkr.apps.pubsimple'))),
    path('api-auth/', include('rest_framework.urls', namespace='rest_framework')),
    path('api/schema/', SpectacularAPIView.as_view(), name='schema'),
    # Optional UI:
    path('api/swagger/', SpectacularSwaggerView.as_view(url_name='schema'), name='swagger-ui'),
    path('api/redoc/', SpectacularRedocView.as_view(url_name='schema'), name='redoc'),
]

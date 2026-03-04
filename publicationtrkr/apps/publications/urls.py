from django.urls import path

from publicationtrkr.apps.publications.views import (
    author_list,
    author_update,
    publication_author_detail,
    publication_create,
    publication_detail,
    publication_list,
    publication_project_detail,
    publication_project_list,
    publication_update,
)

urlpatterns = [
    path('', publication_list, name='publication_list'),
    path('create/', publication_create, name='publication_create'),
    path('authors/', author_list, name='author_list'),
    path('authors/<uuid>/update', author_update, name='author_update'),
    path('by-author-uuid/<fabric_uuid>', publication_author_detail, name='publication_author_detail'),
    path('projects/', publication_project_list, name='publication_project_list'),
    path('projects/<project_uuid>', publication_project_detail, name='publication_project_detail'),
    path('<uuid>', publication_detail, name='publication_detail'),
    path('<uuid>/update', publication_update, name='publication_update'),
]

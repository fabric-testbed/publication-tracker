from django.contrib import admin
from publicationtrkr.apps.publications.models import AuthorCorrection


@admin.register(AuthorCorrection)
class AuthorCorrectionAdmin(admin.ModelAdmin):
    list_display = ('created', 'author_uuid', 'actor_uuid', 'reason')
    search_fields = ('author_uuid', 'publication_uuid', 'actor_uuid')
    readonly_fields = ('uuid', 'created', 'actor_uuid', 'author_uuid', 'publication_uuid', 'reason', 'before', 'after')

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

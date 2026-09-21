from django.contrib import admin
from publicationtrkr.apps.publications.models import Author, AuthorCorrection


@admin.register(Author)
class AuthorAdmin(admin.ModelAdmin):
    """
    Read-only: display_name_source (#73) is shown here and nowhere public. Edits go through
    the author form or API, which keep membership, claims and corrections consistent.
    """
    list_display = ('author_name', 'display_name', 'display_name_source', 'fabric_uuid', 'publication_uuid')
    list_filter = ('display_name_source',)
    search_fields = ('author_name', 'display_name', 'uuid', 'fabric_uuid', 'publication_uuid')
    readonly_fields = ('uuid', 'author_name', 'author_order', 'display_name', 'display_name_source',
                       'fabric_uuid', 'publication_uuid')

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


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

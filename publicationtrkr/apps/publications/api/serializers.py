from rest_framework import serializers

from publicationtrkr.apps.publications.models import Author, Publication
from publicationtrkr.apps.publications.utils.bibtex_utils import generate_bibtex


class AuthorSerializer(serializers.ModelSerializer):
    class Meta:
        model = Author
        fields = ['author_name', 'display_name', 'fabric_uuid', 'publication_uuid', 'uuid']


class PublicationSerializer(serializers.ModelSerializer):
    """
    Publication
    - authors
    - bibtex
    - created
    - created_by
    - link
    - modified
    - modified_by
    - project_name
    - project_uuid
    - title
    - uuid
    - venue
    - year
    """
    authors = serializers.SerializerMethodField(method_name='get_authors')
    bibtex = serializers.SerializerMethodField(method_name='get_bibtex')
    created = serializers.SerializerMethodField(method_name='get_created')
    created_by = serializers.SerializerMethodField(method_name='get_created_by')
    lookup_field = 'uuid'
    modified = serializers.SerializerMethodField(method_name='get_modified')
    modified_by = serializers.SerializerMethodField(method_name='get_modified_by')

    class Meta:
        model = Publication
        fields = ['authors', 'bibtex', 'created', 'created_by', 'link', 'modified', 'modified_by', 'project_name',
                  'project_uuid', 'title', 'uuid', 'venue', 'year']

    @staticmethod
    def get_authors(self) -> list:
        authors = Author.objects.filter(uuid__in=self.authors)
        return AuthorSerializer(authors, many=True).data

    @staticmethod
    def get_bibtex(self) -> str:
        if self.bibtex:
            return self.bibtex
        return generate_bibtex(self)

    @staticmethod
    def get_created(self) -> str:
        return str(self.created.isoformat(' '))

    @staticmethod
    def get_created_by(self) -> str | None:
        # created_by is on_delete=SET_NULL. Without this guard a single row whose
        # creator was removed raises AttributeError inside ListSerializer, which
        # takes down the whole page -- /api/publications, /publications/,
        # by-author-uuid and by-project-uuid all 500, not just the affected record.
        return str(self.created_by.uuid) if self.created_by else None

    @staticmethod
    def get_modified(self) -> str:
        return str(self.modified.isoformat(' '))

    @staticmethod
    def get_modified_by(self) -> str | None:
        return str(self.modified_by.uuid) if self.modified_by else None


class PublicationCreateSerializer(serializers.ModelSerializer):
    """
    Publication
    - authors
    - bibtex
    - link
    - project_name
    - project_uuid
    - title
    - venue
    - year
    """
    lookup_field = 'uuid'

    class Meta:
        model = Publication
        fields = ['authors', 'bibtex', 'link', 'project_name', 'project_uuid', 'title', 'venue', 'year']

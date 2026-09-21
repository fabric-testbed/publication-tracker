from rest_framework import serializers

from publicationtrkr.apps.publications.models import Author, Publication
from publicationtrkr.apps.publications.utils.bibtex_utils import generate_bibtex


class AuthorSerializer(serializers.ModelSerializer):
    correction_reason = serializers.CharField(required=False, allow_blank=True, max_length=2000, write_only=True)

    class Meta:
        model = Author
        fields = ['author_name', 'author_order', 'display_name', 'fabric_uuid', 'publication_uuid',
                  'uuid', 'correction_reason']


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
        """
        The publication's authors in the order they appear on the publication.

        `filter(uuid__in=...)` answers "which rows", never "in what order": the uuid list
        is an unordered set as far as SQL is concerned. Author.Meta.ordering now supplies
        an ORDER BY, but it is not relied on alone -- a `.distinct()`, a join or a future
        annotation on this queryset could displace it, and the failure would be silent.
        Re-mapping through self.authors, which is the authoritative order, cannot be
        undone by any of that. This is the same shape as
        bibtex_utils._resolve_author_names, and keeping the two alike is what makes the
        `bibtex` string and the `authors` array in one response agree.

        Rows named in the array but missing from the table are skipped rather than raising
        -- the same tolerance _resolve_author_names has always had.
        """
        by_uuid = {author.uuid: author for author in Author.objects.filter(uuid__in=self.authors)}
        ordered = [by_uuid[author_uuid] for author_uuid in self.authors if author_uuid in by_uuid]
        return AuthorSerializer(ordered, many=True).data

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

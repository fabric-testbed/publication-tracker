from rest_framework import serializers

from publicationtrkr.apps.pubsimple.models import PubSimple


class PubSimpleSerializer(serializers.ModelSerializer):
    """
    PubSimple - publications simple format
    - authors
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
    created = serializers.SerializerMethodField(method_name='get_created')
    created_by = serializers.SerializerMethodField(method_name='get_created_by')
    lookup_field = 'uuid'
    modified = serializers.SerializerMethodField(method_name='get_modified')
    modified_by = serializers.SerializerMethodField(method_name='get_modified_by')

    class Meta:
        model = PubSimple
        fields = ['authors', 'created', 'created_by', 'link', 'modified', 'modified_by', 'project_name',
                  'project_uuid', 'title', 'uuid', 'venue', 'year']

    @staticmethod
    def get_created(self) -> str:
        return str(self.created.isoformat(' '))

    @staticmethod
    def get_created_by(self) -> str:
        return str(self.created_by.uuid)

    @staticmethod
    def get_modified(self) -> str:
        return str(self.modified.isoformat(' '))

    @staticmethod
    def get_modified_by(self) -> str:
        return str(self.modified_by.uuid)


class PubSimpleCreateSerializer(serializers.ModelSerializer):
    """
    PubSimple - publications simple format
    - authors
    - link
    - project_name
    - project_uuid
    - title
    - venue
    - year
    """
    lookup_field = 'uuid'

    class Meta:
        model = PubSimple
        fields = ['authors', 'link', 'project_name', 'project_uuid', 'title', 'venue', 'year']
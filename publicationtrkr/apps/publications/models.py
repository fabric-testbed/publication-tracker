from django.db import models
import os
from datetime import datetime, timedelta, timezone
from django.db.models import Q
from django.db.models import Deferrable, UniqueConstraint
from django.contrib.postgres.fields import ArrayField
from django.db import models
from publicationtrkr.apps.apiuser.models import ApiUser

# Create your models here.
class Publication(models.Model):
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
    authors = ArrayField(models.CharField(max_length=255, blank=False, null=False), default=list)
    bibtex = models.TextField(max_length=10000, blank=True, null=True, default=None)
    created = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(
        ApiUser,
        related_name='publications_publication_created_by',
        null=True,
        on_delete=models.SET_NULL,
    )
    link = models.TextField(max_length=5000, blank=True, null=True, default=None)
    modified = models.DateTimeField(auto_now=True)
    modified_by = models.ForeignKey(
        ApiUser,
        related_name='publications_publication_modified_by',
        null=True,
        on_delete=models.SET_NULL,
    )
    project_name = models.TextField(max_length=5000, blank=True, null=True, default=None)
    project_uuid = models.CharField(primary_key=False, max_length=255, blank=True, null=True, default=None)
    title = models.TextField(max_length=5000, blank=False, null=False)
    uuid = models.CharField(primary_key=False, max_length=255, blank=False, null=False)
    venue = models.CharField(max_length=255, blank=True, null=True, default=None)
    year = models.CharField(max_length=255, blank=False, null=False)

    class Meta:
        constraints = [
            UniqueConstraint(fields=['title'], name='unique_publications_publication_link_is_null', condition=Q(link__isnull=True)),
            UniqueConstraint(fields=['title', 'link'], name='unique_publications_publication'),
        ]

    def as_dict(self):
        return {
            'authors': self.authors,
            'bibtex': self.bibtex,
            'link': self.link,
            'project_name': self.project_name,
            'project_uuid': self.project_uuid,
            'title': self.title,
            'uuid': self.uuid,
            'venue': self.venue,
            'year': self.year,
        }

    def __str__(self):
        return self.uuid


class Author(models.Model):
    """
    Author - represents an author entry tied to a specific Publication
    - author_name: name as found in the publication object when created
    - publication_uuid: reference to the Publication this author belongs to
    - display_name: editable name (defaults to author_name, modifiable by claimed user)
    - uuid: unique identifier for this author record
    - fabric_uuid: reference to the ApiUser uuid (set when claimed)
    """
    author_name = models.CharField(max_length=255, blank=False, null=False)
    display_name = models.CharField(max_length=255, blank=False, null=False)
    fabric_uuid = models.CharField(max_length=255, blank=True, null=True, default=None)
    publication_uuid = models.CharField(max_length=255, blank=False, null=False)
    uuid = models.CharField(primary_key=False, max_length=255, blank=False, null=False)

    def __str__(self):
        return self.uuid

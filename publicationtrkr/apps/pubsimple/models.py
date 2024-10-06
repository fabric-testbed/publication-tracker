from django.db import models
import os
from datetime import datetime, timedelta, timezone
from django.db.models import Q
from django.db.models import Deferrable, UniqueConstraint
from django.contrib.postgres.fields import ArrayField
from django.db import models
from publicationtrkr.apps.apiuser.models import ApiUser

# Create your models here.
class PubSimple(models.Model):
    """
    Publication - simple format
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
    - year
    """
    authors = ArrayField(models.CharField(max_length=255, blank=False, null=False), default=list)
    created = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(
        ApiUser,
        related_name='publication_created_by',
        null=True,
        on_delete=models.SET_NULL,
    )
    link = models.TextField(max_length=5000, blank=True, null=True, default=None)
    modified = models.DateTimeField(auto_now=True)
    modified_by = models.ForeignKey(
        ApiUser,
        related_name='artifact_modified_by',
        null=True,
        on_delete=models.SET_NULL,
    )
    project_name = models.TextField(max_length=5000, blank=True, null=True, default=None)
    project_uuid = models.CharField(primary_key=False, max_length=255, blank=True, null=True, default=None)
    title = models.TextField(max_length=5000, blank=False, null=False)
    uuid = models.CharField(primary_key=False, max_length=255, blank=False, null=False)
    year = models.CharField(max_length=255, blank=False, null=False)

    class Meta:
        constraints = [
            UniqueConstraint(fields=['title'], name='unique_publication_link_is_null', condition=Q(link__isnull=True)),
            UniqueConstraint(fields=['title', 'link'], name='unique_publication'),
        ]

    def as_dict(self):
        return {
            'authors': self.authors,
            'link': self.link,
            'project_name': self.project_name,
            'project_uuid': self.project_uuid,
            'title': self.title,
            'uuid': self.uuid,
            'year': self.year,
        }

    def __str__(self):
        return self.uuid
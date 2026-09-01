import os
from datetime import datetime, timedelta, timezone

from django.contrib.postgres.fields import ArrayField
from django.db import models
from django.db.models import UniqueConstraint


# Create your models here.
class ApiUser(models.Model):
    """
    ApiUser
    - FABRIC user or Anonymous user as set by fabric cookie or token
    """
    COOKIE = "cookie"
    TOKEN = "token"
    ACCESS_TYPE_CHOICES = (
        (COOKIE, "Cookie"),
        (TOKEN, "Token"),
    )
    access_expires = models.DateTimeField(blank=True, null=True)
    access_type = models.CharField(
        max_length=24, choices=ACCESS_TYPE_CHOICES, default=COOKIE
    )
    # Mirrors FabricPeople.active from core-api. Deactivated people are kept rather
    # than deleted -- created_by/modified_by are SET_NULL, so removing an ApiUser
    # silently destroys publication provenance.
    active = models.BooleanField(default=True)
    affiliation = models.CharField(max_length=255, blank=True)
    # blank=True: a row created by sync_fabric_users has no cilogon_id.
    # /journey-tracker/people does not return one, and the login path fills it in the
    # first time that person authenticates (see utils/fabric_auth.py).
    cilogon_id = models.CharField(max_length=255, blank=True)
    email = models.CharField(max_length=255, blank=True)
    fabric_roles = ArrayField(models.CharField(max_length=255, blank=True), default=list)
    # False on a row that only exists because the directory sync created it; set True
    # by the login path. Distinguishes "a FABRIC user" from "a user of this app".
    has_logged_in = models.BooleanField(default=False)
    # Last time sync_fabric_users wrote this row. Null on a row that was only ever
    # created by a login.
    last_synced = models.DateTimeField(blank=True, null=True, default=None)
    name = models.CharField(max_length=255, blank=True)
    projects = ArrayField(models.CharField(max_length=255, blank=True), default=list)
    uuid = models.CharField(primary_key=False, max_length=255, blank=False, unique=True)

    @property
    def can_create_publication(self):
        return os.getenv('CAN_CREATE_PUBLICATION_ROLE') in self.fabric_roles

    @property
    def is_publication_tracker_admin(self):
        return os.getenv('PUBLICATION_TRACKER_ADMINS_ROLE') in self.fabric_roles

    @property
    def is_authenticated(self):
        return self.uuid != os.getenv('API_USER_ANON_UUID')

    @property
    def is_project_member(self, project_uuid: str) -> bool:
        return project_uuid in self.projects

    def as_dict(self):
        return {
            'access_expires': str(self.access_expires),
            'access_type': self.access_type,
            'active': self.active,
            'affiliation': self.affiliation,
            'can_create_publication': self.can_create_publication,
            'cilogon_id': self.cilogon_id,
            'email': self.email,
            'fabric_roles': self.fabric_roles,
            'has_logged_in': self.has_logged_in,
            'is_publication_tracker_admin': self.is_publication_tracker_admin,
            'is_authenticated': self.is_authenticated,
            'last_synced': str(self.last_synced) if self.last_synced else None,
            'name': self.name,
            'projects': self.projects,
            'uuid': self.uuid
        }

    def __str__(self):
        return self.uuid


class TaskTimeoutTracker(models.Model):
    """
    Task Timeout Tracker
    - description
    - last_updated
    - name
    - timeout_in_seconds
    - uuid
    - value
    """
    description = models.CharField(max_length=255, blank=True, null=True)
    last_updated = models.DateTimeField(blank=False, null=False)
    name = models.CharField(max_length=255, blank=False, null=False)
    timeout_in_seconds = models.IntegerField(default=0, blank=False, null=False)
    uuid = models.CharField(primary_key=True, max_length=255, blank=False, null=False)
    value = models.TextField(blank=True, null=True)

    # Order by name
    class Meta:
        constraints = [
            UniqueConstraint(fields=['name'], name='unique_task_timeout_tracker_name'),
        ]
        db_table = "task_timeout_tracker"
        ordering = ("name",)

    def __str__(self):
        return self.name

    def timed_out(self) -> bool:
        if datetime.now(timezone.utc) > (self.last_updated + timedelta(seconds=int(self.timeout_in_seconds))):
            return True
        else:
            return False

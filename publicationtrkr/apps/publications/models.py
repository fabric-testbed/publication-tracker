from django.db import models
from django.db.models import Q
from django.db.models import UniqueConstraint
from django.contrib.postgres.fields import ArrayField
from publicationtrkr.apps.apiuser.models import ApiUser
from uuid import uuid4

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
    - author_order: 0-based slot of this author within its publication's author list
    - publication_uuid: reference to the Publication this author belongs to
    - display_name: editable name (defaults to author_name, modifiable by claimed user)
    - uuid: unique identifier for this author record
    - fabric_uuid: reference to the ApiUser uuid (set when claimed)

    `author_order` exists because author order is a fact about the publication -- it is
    the credit order printed on the paper -- and until now it had no home on the row that
    carries the author. It was implied only by the position of this row's uuid inside
    Publication.authors, so every read path that did not walk that array lost it.

    The read path that lost it was PublicationSerializer.get_authors, which resolved the
    array with `filter(uuid__in=...)` and no ordering. Author had no Meta.ordering either,
    so no ORDER BY was emitted at all and Postgres was free to return heap order. Heap
    order is not stable: an UPDATE rewrites a row and moves it, so claiming an author,
    correcting a spelling or editing a display_name silently reordered that publication's
    author list from then on. Measured against the production data of 2026-09-14, 146 of
    178 multi-author publications were already being served out of order, including one
    34-author paper whose first author came back last.

    Publication.authors stays authoritative for *membership*: it is what the update path
    reconciles against and what migration 0005 reads to backfill this column. This column
    is the ordering key, written from that same array position inside the same
    transaction (publication_builder._create_authors / _sync_authors), which is what keeps
    the two from drifting.
    """
    author_name = models.CharField(max_length=255, blank=False, null=False)
    author_order = models.PositiveIntegerField(default=0)
    display_name = models.CharField(max_length=255, blank=False, null=False)
    fabric_uuid = models.CharField(max_length=255, blank=True, null=True, default=None)
    publication_uuid = models.CharField(max_length=255, blank=False, null=False)
    uuid = models.CharField(primary_key=False, max_length=255, blank=False, null=False, unique=True)

    class Meta:
        # The ordering every consumer inherits, so a caller that forgets to order is
        # correct by default rather than subtly wrong. `id` is the tie-break: it keeps
        # output deterministic among rows still at the default 0, which is what an author
        # belonging to no publication's array would be.
        ordering = ('author_order', 'id')

    def __str__(self):
        return self.uuid


class AuthorClaim(models.Model):
    """
    A scored (Author, ApiUser) pair, and the ledger of how that pair was decided (#32).

    `Author.fabric_uuid` stays the single authoritative field: it is written only when a
    claim is approved or self-asserted, never by scoring. A row here is a *suggestion or a
    record*, and the distinction is what lets the queue be regenerated at will without
    touching attribution.

    Four statuses, three sources. `suggested` rows come from score_author_claims and mean
    nothing has been decided. `approved` and `rejected` are an admin's decision;
    a rejection is kept rather than deleted, because "we already looked at this pair and
    said no" is the only thing that stops it being re-suggested every night, and because
    rejections are the training data for any future scoring work. `self_asserted` records
    a claim the user made about themselves through the existing immediate path -- option
    (a) on the issue -- so the ledger is complete even for claims the queue never saw.

    `source` is who *proposed* the pair, which is not the same question as who decided it.
    `machine` is score_author_claims, `self` is the subject, and `admin` is a fabric_uuid
    typed straight into the author edit form. The third value earns its place by keeping
    the approved rows readable as evidence: without it a hand-entered attribution is
    indistinguishable from a suggestion the scorer got right, and every later attempt to
    measure the scorer against real decisions would be measuring its own input.

    The unique constraint on (author, api_user) is what makes re-scoring idempotent:
    a nightly run updates a standing suggestion in place rather than appending a second
    one. It also means a decided pair cannot be quietly re-suggested, since the scorer
    has to look at the existing row and leave decided ones alone.

    on_delete=CASCADE on `author` is deliberate and load-bearing: the API update path
    deletes surplus Author rows when a publication's author list shrinks
    (viewsets.py:319-320), and a claim pointing at a deleted author is meaningless.
    `api_user` is CASCADE for a different reason -- sync_fabric_users never deletes
    ApiUser rows, so it should not fire in practice, and if it ever does the claim is
    equally meaningless.
    """
    SUGGESTED = 'suggested'
    APPROVED = 'approved'
    REJECTED = 'rejected'
    SELF_ASSERTED = 'self_asserted'
    STATUS_CHOICES = (
        (SUGGESTED, 'Suggested'),
        (APPROVED, 'Approved'),
        (REJECTED, 'Rejected'),
        (SELF_ASSERTED, 'Self-asserted'),
    )

    MACHINE = 'machine'
    SELF = 'self'
    ADMIN = 'admin'
    SOURCE_CHOICES = (
        (MACHINE, 'Machine'),
        (SELF, 'Self'),
        (ADMIN, 'Admin'),
    )

    # A decided status is one the scorer must not overwrite.
    DECIDED_STATUSES = (APPROVED, REJECTED, SELF_ASSERTED)

    api_user = models.ForeignKey(
        ApiUser,
        related_name='publications_authorclaim_api_user',
        on_delete=models.CASCADE,
    )
    author = models.ForeignKey(
        Author,
        related_name='claims',
        on_delete=models.CASCADE,
    )
    created = models.DateTimeField(auto_now_add=True)
    decided_at = models.DateTimeField(blank=True, null=True, default=None)
    decided_by = models.ForeignKey(
        ApiUser,
        related_name='publications_authorclaim_decided_by',
        blank=True,
        null=True,
        default=None,
        on_delete=models.SET_NULL,
    )
    modified = models.DateTimeField(auto_now=True)
    score = models.FloatField(default=0.0)
    # Per-signal contributions, kept for display rather than for computation: an admin
    # judging a suggestion in seconds needs to see *why* it scored, and a score with no
    # breakdown is unreviewable. Shape is {signal_name: {"weight": w, "value": v,
    # "detail": "..."}}, written by score_author_claims and read by the queue.
    signals = models.JSONField(default=dict, blank=True)
    source = models.CharField(max_length=24, choices=SOURCE_CHOICES, default=MACHINE)
    status = models.CharField(max_length=24, choices=STATUS_CHOICES, default=SUGGESTED)
    uuid = models.CharField(max_length=255, blank=False, null=False)

    class Meta:
        constraints = [
            UniqueConstraint(fields=['author', 'api_user'], name='unique_author_claim'),
        ]
        indexes = [
            # The queue's only ordering: undecided suggestions, best first.
            models.Index(fields=['status', '-score'], name='authorclaim_status_score'),
        ]

    @property
    def is_decided(self) -> bool:
        return self.status in self.DECIDED_STATUSES

    def as_dict(self):
        return {
            'api_user_uuid': self.api_user.uuid,
            'author_uuid': self.author.uuid,
            'created': str(self.created),
            'decided_at': str(self.decided_at) if self.decided_at else None,
            'decided_by': self.decided_by.uuid if self.decided_by else None,
            'score': self.score,
            'signals': self.signals,
            'source': self.source,
            'status': self.status,
            'uuid': self.uuid,
        }

    def __str__(self):
        return self.uuid


class AuthorCorrection(models.Model):
    """Immutable evidence retained independently of corrected/deleted author rows."""

    uuid = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    created = models.DateTimeField(auto_now_add=True)
    actor_uuid = models.CharField(max_length=255)
    author_uuid = models.CharField(max_length=255, db_index=True)
    publication_uuid = models.CharField(max_length=255)
    reason = models.TextField()
    before = models.JSONField()
    after = models.JSONField(null=True)

    class Meta:
        ordering = ('created', 'uuid')

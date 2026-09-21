"""Transactional author edits shared by the API and HTML form.

Lock publications in primary-key order, then authors, then claims. Scoring only
locks an author and its claims; it never acquires a publication lock afterwards.
Correction snapshots have no cascading FK: deleting an author cannot erase them.
"""

from django.db import transaction
from django.core.serializers.json import DjangoJSONEncoder
from django.utils import timezone
from rest_framework.exceptions import ValidationError
import json

from publicationtrkr.apps.apiuser.models import ApiUser
from publicationtrkr.apps.publications.models import Author, AuthorClaim, AuthorCorrection, Publication
from publicationtrkr.apps.publications.utils.claim_ledger import (
    record_admin_claim, record_self_claim, withdraw_suggestions,
)


AUTHOR_FIELDS = ('uuid', 'author_name', 'display_name', 'fabric_uuid', 'publication_uuid', 'author_order')


def snapshot(author):
    claims = list(author.claims.order_by('id').values(
        'uuid', 'created', 'modified', 'status', 'source', 'score', 'signals',
        'decided_at', 'decided_by__uuid', 'api_user__uuid',
    ))
    return json.loads(json.dumps({
        'author': {field: getattr(author, field) for field in AUTHOR_FIELDS},
        'claims': claims,
    }, cls=DjangoJSONEncoder))


def has_history(author):
    return bool(author.fabric_uuid) or author.claims.exclude(status=AuthorClaim.SUGGESTED).exists()


def correction_reason(actor, reason):
    if not actor.is_publication_tracker_admin:
        raise ValidationError({'correction_reason': 'An admin must explicitly correct existing attribution.'})
    if not isinstance(reason, str) or not reason.strip():
        raise ValidationError({'correction_reason': 'Provide a reason to explicitly correct existing attribution.'})
    if len(reason) > 2000:
        raise ValidationError({'correction_reason': 'Use at most 2000 characters.'})
    return reason.strip()


def record_correction(author, actor, reason, before, *, deleted=False):
    return AuthorCorrection.objects.create(
        actor_uuid=actor.uuid, author_uuid=before['author']['uuid'],
        publication_uuid=before['author']['publication_uuid'], reason=reason,
        before=before, after=None if deleted else snapshot(author),
    )


def _publications(uuids):
    pubs = list(Publication.objects.select_for_update().filter(uuid__in=uuids).order_by('pk'))
    if len(pubs) != len(set(uuids)):
        raise ValidationError({'publication_uuid': 'Must identify an existing publication.'})
    return {p.uuid: p for p in pubs}


def _save_membership(pub, uuids, actor):
    pub.authors = uuids
    pub.modified_by = actor
    pub.modified = timezone.now()
    pub.save(update_fields=['authors', 'modified_by', 'modified'])
    for position, uuid in enumerate(uuids):
        Author.objects.filter(uuid=uuid).update(author_order=position)


@transaction.atomic
def mutate_author(*, actor, data, author=None, delete=False, self_claim=False):
    """Apply validated fields, maintaining membership, order, attribution and history."""
    data = dict(data)
    reason = data.pop('correction_reason', '')
    if not self_claim and not actor.is_publication_tracker_admin:
        raise ValidationError({'detail': 'Only admins may edit author records.'})
    old_pub_uuid = None
    if author is not None:
        # ModelForm may already have mutated the passed instance: read identity anew.
        stored = Author.objects.get(pk=author.pk)
        old_pub_uuid = stored.publication_uuid
    destination = data.get('publication_uuid', old_pub_uuid)
    pubs = _publications({p for p in (old_pub_uuid, destination) if p})
    before = None
    if author is not None:
        author = Author.objects.select_for_update().get(pk=author.pk)
        if author.publication_uuid != old_pub_uuid:
            raise ValidationError({'detail': 'This author moved during the edit. Reload and try again.'})
        before = snapshot(author)
        if self_claim:
            if author.fabric_uuid and author.fabric_uuid != actor.uuid:
                raise ValidationError({'fabric_uuid': 'This author is already claimed by another user.'})
            data = {'display_name': data.get('display_name', author.display_name), 'fabric_uuid': actor.uuid}
        if 'uuid' in data and data['uuid'] != author.uuid:
            raise ValidationError({'uuid': 'The author UUID cannot be changed.'})
        changes_identity = any(
            field in data and (data[field] or '') != (getattr(author, field) or '')
            for field in ('author_name', 'publication_uuid')
        )
        changes_attribution = 'fabric_uuid' in data and (data['fabric_uuid'] or '') != (author.fabric_uuid or '')
        requires_correction = (has_history(author) and (delete or changes_identity)) or (
            bool(author.fabric_uuid) and changes_attribution)
        if requires_correction:
            reason = correction_reason(actor, reason)
    else:
        requires_correction = False
        if destination not in pubs:
            raise ValidationError({'publication_uuid': 'Must identify an existing publication.'})
        if Author.objects.filter(uuid=data.get('uuid')).exists():
            raise ValidationError({'uuid': 'An author with this UUID already exists.'})
        author = Author()

    if delete:
        pub = pubs[old_pub_uuid]
        _save_membership(pub, [u for u in pub.authors if u != author.uuid], actor)
        if requires_correction:
            record_correction(author, actor, reason, before, deleted=True)
        author.delete()
        return None

    fabric_uuid = data.get('fabric_uuid', author.fabric_uuid)
    if fabric_uuid and not ApiUser.objects.filter(uuid=fabric_uuid).exists():
        raise ValidationError({'fabric_uuid': 'Must identify an existing FABRIC user.'})
    for field in AUTHOR_FIELDS:
        if field in data:
            setattr(author, field, data[field])
    if before and author.author_name != before['author']['author_name']:
        if 'display_name' not in data and author.display_name == before['author']['author_name']:
            author.display_name = author.author_name
    if before and any(getattr(author, field) != before['author'][field]
                      for field in ('author_name', 'publication_uuid')):
        # Project and coauthor evidence belongs to the original publication.
        # A move invalidates those suggestions just as a changed author name does.
        withdraw_suggestions(author)
    author.save()
    if self_claim:
        record_self_claim(author, actor)
    elif not before or (before['author']['fabric_uuid'] or '') != (author.fabric_uuid or ''):
        record_admin_claim(author, author.fabric_uuid, decided_by=actor)
        # Clearing attribution must also withdraw suggestions, while retaining decisions.
        withdraw_suggestions(author)

    if old_pub_uuid and old_pub_uuid != author.publication_uuid:
        old_pub = pubs[old_pub_uuid]
        _save_membership(old_pub, [u for u in old_pub.authors if u != author.uuid], actor)
    pub = pubs[author.publication_uuid]
    membership = [u for u in pub.authors if u != author.uuid]
    if 'author_order' in data:
        position = data['author_order']
        if position > len(membership):
            raise ValidationError({'author_order': 'Position exceeds the publication author list.'})
    elif author.uuid in pub.authors:
        position = pub.authors.index(author.uuid)
    else:
        position = len(membership)
    membership.insert(position, author.uuid)
    _save_membership(pub, membership, actor)
    author.refresh_from_db()
    if requires_correction:
        record_correction(author, actor, reason, before)
    elif before and (before['author']['fabric_uuid'] or '') != (author.fabric_uuid or '') and any(
            c['status'] != AuthorClaim.SUGGESTED for c in before['claims']):
        # A new self-claim stays immediate even if a machine suggestion was once
        # rejected. Keep the prior decision if recording this claim supersedes it.
        record_correction(author, actor, 'Self-assertion' if self_claim else 'Admin attribution', before)
    return author


@transaction.atomic
def delete_publication(*, publication, actor, reason=''):
    publication = Publication.objects.select_for_update().get(pk=publication.pk)
    authors = list(Author.objects.select_for_update().filter(publication_uuid=publication.uuid).order_by('pk'))
    for author in authors:
        if has_history(author):
            approved_reason = correction_reason(actor, reason)
            record_correction(author, actor, approved_reason, snapshot(author), deleted=True)
    Author.objects.filter(pk__in=[a.pk for a in authors]).delete()
    publication.delete()

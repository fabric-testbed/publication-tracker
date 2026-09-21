"""Transactional author edits shared by the API and HTML form.

Lock publications in primary-key order, then authors, then claims. Scoring only
locks an author and its claims; it never acquires a publication lock afterwards.
Correction snapshots have no cascading FK: deleting an author cannot erase them.

display_name is presentation, not identity (#73): changing it never needs a correction.
Which name a row shows, and why, is settled by _apply_display_name.
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
from publicationtrkr.apps.publications.utils.display_names import (
    account_name_for, choose_display_name, recheck_after_commit, restore_byline, use_automatic,
)


AUTHOR_FIELDS = ('uuid', 'author_name', 'display_name', 'fabric_uuid', 'publication_uuid', 'author_order')
# Snapshotted so a correction records whether a name was chosen, but never taken from
# request data: the source follows from what was submitted (_apply_display_name).
SNAPSHOT_FIELDS = AUTHOR_FIELDS + ('display_name_source',)


def snapshot(author):
    claims = list(author.claims.order_by('id').values(
        'uuid', 'created', 'modified', 'status', 'source', 'score', 'signals',
        'decided_at', 'decided_by__uuid', 'api_user__uuid',
    ))
    return json.loads(json.dumps({
        'author': {field: getattr(author, field) for field in SNAPSHOT_FIELDS},
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


def _apply_display_name(author, before, submitted, use_account_name):
    """
    Settle display_name and display_name_source once the other fields are applied (#73).

    `use_account_name` is tri-state. True returns the row to automatic naming -- the
    credited person's usable account name, else the byline. False pins the name as
    `custom`, even one equal to the account name: that is the form's unchecked box
    saying "keep this name", and it has to stick. None, which is every API call that does
    not send it, counts `submitted` as a choice only when it differs from what is stored,
    so a PUT that echoes a GET back pins nothing; a changed name that equals the account
    name keeps following it. On create, a display_name equal to the byline is the default
    rather than a choice.

    With no choice made, the name still has to stay true to what changed around it.
    Removing the credit restores the byline, even over a custom name: that name was the
    previous claimant's. Moving the credit to someone else drops a followed account name
    before the ledger applies the new person's (claim_ledger). Renaming the byline carries
    a byline copy along with it.
    """
    was = before['author'] if before else None
    account_name = account_name_for(author.fabric_uuid)
    stored = was['display_name'] if was else author.author_name
    changed = submitted is not None and submitted != stored
    if use_account_name:
        if changed and was is not None:
            raise ValidationError({'use_account_name': 'Send a new display_name or use_account_name, not both.'})
        use_automatic(author, account_name)
    elif use_account_name is False:
        author.display_name = submitted or author.display_name or author.author_name
        author.display_name_source = Author.CUSTOM
    elif changed:
        choose_display_name(author, submitted, account_name)
    elif was is None:
        use_automatic(author, account_name)
    else:
        old_credit, new_credit = was['fabric_uuid'] or None, author.fabric_uuid or None
        if old_credit and not new_credit:
            restore_byline(author)
        elif old_credit and new_credit != old_credit and author.display_name_source == Author.ACCOUNT:
            restore_byline(author)
        elif author.display_name_source == Author.BYLINE and author.author_name != was['author_name'] \
                and author.display_name == was['author_name']:
            author.display_name = author.author_name


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
    use_account_name = data.pop('use_account_name', None)
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
            chosen = {'display_name': data['display_name']} if 'display_name' in data else {}
            data = {**chosen, 'fabric_uuid': actor.uuid}
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
    submitted_display_name = data.pop('display_name', None)
    for field in AUTHOR_FIELDS:
        if field in data:
            setattr(author, field, data[field])
    _apply_display_name(author, before, submitted_display_name, use_account_name)
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
    if author.display_name_source == Author.ACCOUNT:
        recheck_after_commit(author.pk)
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

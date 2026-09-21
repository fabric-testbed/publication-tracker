"""
The one place request data becomes Publication and Author rows.

The API create path, the API update path and the web form's clean() each carried
their own copy of the "manual input overrides BibTeX" merge, and the three copies
had drifted -- create resolved venue from BibTeX, clean() did not; update could
not clear a field that create could. They share resolve_create_fields() and
resolve_update_fields() now.

create and update also share the persistence half, which runs inside
transaction.atomic(). That is what closes the orphan-Author bug: the old create()
saved every Author row before publication.save(), so a title/link unique-constraint
failure returned 400 with those rows already committed and no publication left to
reference them. Production accumulated 19 of them that way.
"""

from collections import defaultdict, deque
from datetime import datetime, timezone
from uuid import uuid4

from django.db import transaction
from rest_framework.exceptions import ValidationError

from publicationtrkr.apps.publications.models import Author, Publication
from publicationtrkr.apps.publications.utils.bibtex_utils import parse_bibtex
from publicationtrkr.apps.publications.utils.claim_ledger import withdraw_suggestions
from publicationtrkr.apps.publications.utils.author_mutations import (
    has_history, correction_reason, snapshot, record_correction,
)


def _first_present(*values):
    """
    First non-empty value, or None. This is the "manual input overrides BibTeX"
    rule, and emptiness is plain falsiness so that '', None and [] all defer to the
    next source -- the same test the code this replaces used.
    """
    for value in values:
        if value:
            return value
    return None


def bibtex_defaults(data) -> dict:
    """Parsed BibTeX for a payload that carries one, or {} when it does not."""
    bibtex_string = data.get('bibtex', None)
    return parse_bibtex(bibtex_string) if bibtex_string else {}


def resolve_create_fields(data, bibtex_data=None) -> dict:
    """
    Every publication field for a create, with BibTeX filling whatever manual input
    left empty. Fields neither source supplies come back None, and authors comes
    back [] -- create has always written an empty array rather than refusing, and
    validate_publication_create is what rejects that case before this runs.
    """
    bibtex_data = bibtex_data or {}
    return {
        'authors': _first_present(data.get('authors'), bibtex_data.get('authors')) or [],
        'bibtex': _first_present(data.get('bibtex')),
        'link': _first_present(data.get('link'), bibtex_data.get('link')),
        'project_name': _first_present(data.get('project_name')),
        'project_uuid': _first_present(data.get('project_uuid')),
        'title': _first_present(data.get('title'), bibtex_data.get('title')),
        'venue': _first_present(data.get('venue'), bibtex_data.get('venue')),
        'year': _first_present(data.get('year'), bibtex_data.get('year')),
    }


def resolve_update_fields(data, bibtex_data=None) -> dict:
    """
    Only the fields this request actually carries, so every other stored value is
    left alone.

    The asymmetry with create is deliberate and preserved from the code this
    replaces: an update has never been able to clear link, venue, project_name or
    project_uuid by sending an empty string, because an empty value reads as "not
    provided" and the stored one stands. Changing that is a behaviour change, not a
    refactor, so it is not made here.
    """
    bibtex_data = bibtex_data or {}
    resolved = {}
    for field in ('authors', 'link', 'title', 'venue', 'year'):
        value = _first_present(data.get(field), bibtex_data.get(field))
        if value:
            resolved[field] = value
    # bibtex, project_name and project_uuid have no BibTeX-derived default.
    for field in ('bibtex', 'project_name', 'project_uuid'):
        if data.get(field):
            resolved[field] = data.get(field)
    return resolved


def _new_author(publication_uuid: str, author_name: str, author_order: int) -> Author:
    author = Author()
    author.author_name = author_name
    author.author_order = author_order
    author.display_name = author_name
    author.fabric_uuid = None
    author.publication_uuid = publication_uuid
    author.uuid = str(uuid4())
    author.save()
    return author


def _create_authors(publication_uuid: str, author_names) -> list:
    """
    One fresh Author row per name, returned in Publication.authors order.

    The enumerate() index is the whole point: it is the credit order the caller gave us
    -- parse_bibtex splits the BibTeX `author` field on ' and ' in order, and the web
    form splits its comma-separated field in order -- and stamping it here is what makes
    that order survive into every later read.
    """
    return [
        _new_author(publication_uuid, author_name, i).uuid
        for i, author_name in enumerate(author_names)
    ]


@transaction.atomic
def _sync_authors(publication, author_names, *, explicit_slots=None, actor=None, reason=None) -> list:
    """Keep exact-name identities across reorder/insertion; never guess claimed renames.

    Only the reviewed repair command supplies explicit_slots. Public string-list
    updates cannot express replacement of a claimed person; use an admin correction.
    """
    by_uuid = {a.uuid: a for a in Author.objects.select_for_update().filter(
        uuid__in=publication.authors).order_by('pk')}
    existing = [by_uuid[u] for u in publication.authors if u in by_uuid]
    if explicit_slots is not None:
        correction_reason(actor, reason)
        if len(explicit_slots) != len(author_names):
            raise ValidationError({'authors': 'Explicit author mapping has the wrong length.'})
        used = [u for u in explicit_slots if u is not None]
        if len(set(used)) != len(used) or any(u not in by_uuid for u in used):
            raise ValidationError({'authors': 'Explicit author mapping is invalid.'})
        matched = [by_uuid.get(u) for u in explicit_slots]
    else:
        names = defaultdict(deque)
        for row in existing:
            names[row.author_name].append(row)
        matched = [names[name].popleft() if names[name] else None for name in author_names]
        used = {row.uuid for row in matched if row is not None}
        remaining = [row for row in existing if row.uuid not in used]
        if any(has_history(row) for row in remaining):
            raise ValidationError({'authors': 'This edit removes or replaces an author with attribution or claim history. Use an explicit admin author correction first.'})
        # Reuse unclaimed unmatched rows for spelling edits, without shifting the
        # exact-name matches (including every claimed identity) around them.
        unmatched = iter(remaining)
        matched = [row if row is not None else next(unmatched, None) for row in matched]

    kept = set()
    result = []
    for position, (name, row) in enumerate(zip(author_names, matched)):
        if row is None:
            row = _new_author(publication.uuid, name, position)
        else:
            renamed = row.author_name != name
            before = snapshot(row) if renamed and has_history(row) else None
            if renamed:
                if row.display_name == row.author_name:
                    row.display_name = name
                row.author_name = name
                withdraw_suggestions(row)
            row.author_order = position
            row.save(update_fields=['author_name', 'display_name', 'author_order'])
            if before:
                record_correction(row, actor, correction_reason(actor, reason), before)
        kept.add(row.uuid)
        result.append(row.uuid)
    for row in existing:
        if row.uuid not in kept:
            if has_history(row):
                # A repair must relocate all decided claims before deleting a slot.
                raise ValidationError({'authors': 'Cannot delete an author carrying attribution or claim history.'})
            row.delete()
    return result


def create_publication(*, data, api_user, resolve_project_name=None) -> Publication:
    """
    Build and save a Publication and its Author rows in one transaction.

    data is the raw request or form payload; the BibTeX merge happens here so every
    caller gets the same answer. resolve_project_name is an optional
    callable(project_uuid) -> str|None used to fill project_name when only the uuid
    was given; the bulk path passes a memoised one because it is a Core API round
    trip.

    Raises whatever the save raises -- the unique constraint on title/link, most
    often. Nothing is left behind when it does; that is the point of the
    transaction.
    """
    fields = resolve_create_fields(data, bibtex_defaults(data))

    project_name = fields['project_name']
    project_uuid = fields['project_uuid']
    # Resolved before the transaction opens: this is an outbound HTTP call, and
    # holding a write transaction across it would pin a connection for as long as
    # the Core API takes to answer.
    if project_uuid and not project_name and resolve_project_name:
        project_name = resolve_project_name(project_uuid)

    now = datetime.now(timezone.utc)
    publication = Publication()
    publication.bibtex = fields['bibtex']
    publication.created = now
    publication.created_by = api_user
    publication.link = fields['link']
    publication.modified = now
    publication.modified_by = api_user
    publication.project_name = project_name
    publication.project_uuid = project_uuid
    publication.title = fields['title']
    publication.uuid = str(uuid4())
    publication.venue = fields['venue']
    publication.year = fields['year']

    with transaction.atomic():
        publication.authors = _create_authors(publication.uuid, fields['authors'])
        publication.save()
    return publication


def update_publication(*, publication, data, api_user, resolve_project_name=None,
                       author_slots=None, author_correction_reason=None) -> Publication:
    """
    Apply an update payload to an existing publication and its Author rows in one
    transaction. Fields the payload does not carry keep their stored values; see
    resolve_update_fields() for what counts as carried.
    """
    fields = resolve_update_fields(data, bibtex_defaults(data))

    project_name = fields.get('project_name', publication.project_name)
    project_uuid = fields.get('project_uuid', publication.project_uuid)
    # Outside the transaction, for the reason given in create_publication().
    if project_uuid and resolve_project_name and (not project_name or (
            project_uuid != publication.project_uuid and 'project_name' not in fields)):
        project_name = resolve_project_name(project_uuid)
        if not project_name:
            raise ValidationError({'project_uuid': 'Unable to find project.'})

    with transaction.atomic():
        publication = Publication.objects.select_for_update().get(pk=publication.pk)
        # Omitted project fields must use the locked current row, not a stale form.
        if 'project_uuid' not in fields and 'project_name' not in fields:
            project_name, project_uuid = publication.project_name, publication.project_uuid
        if 'bibtex' in fields:
            publication.bibtex = fields['bibtex']
        if 'authors' in fields:
            publication.authors = _sync_authors(
                publication, fields['authors'], explicit_slots=author_slots,
                actor=api_user, reason=author_correction_reason)
        if 'link' in fields:
            publication.link = fields['link']
        publication.modified = datetime.now(timezone.utc)
        publication.modified_by = api_user
        publication.project_name = project_name
        publication.project_uuid = project_uuid
        if 'title' in fields:
            publication.title = fields['title']
        if 'venue' in fields:
            publication.venue = fields['venue']
        if 'year' in fields:
            publication.year = fields['year']
        publication.save()
    return publication

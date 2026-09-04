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

from datetime import datetime, timezone
from uuid import uuid4

from django.db import transaction

from publicationtrkr.apps.publications.models import Author, Publication
from publicationtrkr.apps.publications.utils.bibtex_utils import parse_bibtex
from publicationtrkr.apps.publications.utils.claim_ledger import withdraw_suggestions


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


def _new_author(publication_uuid: str, author_name: str) -> Author:
    author = Author()
    author.author_name = author_name
    author.display_name = author_name
    author.fabric_uuid = None
    author.publication_uuid = publication_uuid
    author.uuid = str(uuid4())
    author.save()
    return author


def _create_authors(publication_uuid: str, author_names) -> list:
    """One fresh Author row per name, returned in Publication.authors order."""
    return [_new_author(publication_uuid, author_name).uuid for author_name in author_names]


def _sync_authors(publication, author_names) -> list:
    """
    Reconcile a publication's Author rows against a new list of names.

    Rows are matched by position in Publication.authors, which keeps uuid,
    display_name and fabric_uuid stable when a different author in the same list is
    renamed -- losing fabric_uuid here would silently unclaim someone's publication.
    Surplus rows are deleted rather than orphaned.
    """
    existing_uuids = list(publication.authors)
    new_author_uuids = []
    for i, author_name in enumerate(author_names):
        if i < len(existing_uuids):
            # Update the existing Author in place
            try:
                author = Author.objects.get(uuid=existing_uuids[i])
                if author.author_name != author_name:
                    author.author_name = author_name
                    author.save(update_fields=['author_name'])
                    # The claim suggestions for this row were scored against the old
                    # spelling and are no longer about this author, so they are withdrawn
                    # rather than left in the queue for up to a day until the next scoring
                    # run recomputes them. Decisions are untouched -- withdraw_suggestions
                    # only ever removes `suggested` rows.
                    withdraw_suggestions(author)
                new_author_uuids.append(author.uuid)
                continue
            except Author.DoesNotExist:
                pass
        # New author, or the matched record has gone missing -- create fresh
        new_author_uuids.append(_new_author(publication.uuid, author_name).uuid)
    # Remove any leftover Authors beyond the new list length
    for old_uuid in existing_uuids[len(author_names):]:
        Author.objects.filter(uuid=old_uuid).delete()
    return new_author_uuids


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


def update_publication(*, publication, data, api_user, resolve_project_name=None) -> Publication:
    """
    Apply an update payload to an existing publication and its Author rows in one
    transaction. Fields the payload does not carry keep their stored values; see
    resolve_update_fields() for what counts as carried.
    """
    fields = resolve_update_fields(data, bibtex_defaults(data))

    project_name = fields.get('project_name', publication.project_name)
    project_uuid = fields.get('project_uuid', publication.project_uuid)
    # Outside the transaction, for the reason given in create_publication().
    if project_uuid and not project_name and resolve_project_name:
        project_name = resolve_project_name(project_uuid)

    with transaction.atomic():
        if 'bibtex' in fields:
            publication.bibtex = fields['bibtex']
        if 'authors' in fields:
            publication.authors = _sync_authors(publication, fields['authors'])
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

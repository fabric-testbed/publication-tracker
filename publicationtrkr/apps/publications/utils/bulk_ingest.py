"""
Bulk ingest: one uploaded document becomes many publications (issue #24).

Three input shapes, all reduced to the same list of record dicts before anything is
written:

  - a JSON body, either a bare list or {"publications": [...]}
  - a .jsonl / .ndjson file, one JSON object per line
  - a .bib / .bibtex file, parsed with parse_bibtex_entries()

Every record is reported on by position, whatever the shape it arrived in, because the
answer to "the upload said 3 skipped" has to be "which three". A .jsonl record also
carries its 1-based file line, which is the number a person needs to fix the file.

Two rules the endpoint depends on:

  - Caps are enforced here, before any database work. An upload over the record cap
    costs one parse and no writes, which is what makes the cap a defence rather than a
    slower way to fail.
  - One record cannot fail another. Each create runs in its own transaction (inside
    create_publication), so a duplicate title is a skipped row in the report, not a
    rolled-back batch. That also means a partial success is a real outcome: the report
    is the record of what landed.
"""

import json

from django.conf import settings
from django.db import IntegrityError

from publicationtrkr.apps.publications.api.validators import validate_publication_data
from publicationtrkr.apps.publications.utils.bibtex_utils import parse_bibtex, parse_bibtex_entries
from publicationtrkr.apps.publications.utils.publication_builder import create_publication

JSONL_SUFFIXES = ('.jsonl', '.ndjson')
BIBTEX_SUFFIXES = ('.bib', '.bibtex')

CREATED = 'created'
SKIPPED = 'skipped'
FAILED = 'failed'


class BulkIngestError(Exception):
    """
    A rejection of the whole request, raised before any database work: an unreadable
    or oversized upload, an unsupported file type, or more records than the cap allows.
    """


def _read_upload(upload) -> str:
    """
    Decode an uploaded file, refusing one that is too large before reading it.

    The size check is explicit because Django has no setting that does it.
    DATA_UPLOAD_MAX_MEMORY_SIZE does not apply to multipart file parts, and
    FILE_UPLOAD_MAX_MEMORY_SIZE is only the threshold at which a part spills from
    memory to a temp file -- neither refuses anything.
    """
    if upload.size is not None and upload.size > settings.BULK_MAX_UPLOAD_BYTES:
        raise BulkIngestError(
            'upload is {0} bytes, over the {1} byte limit'.format(
                upload.size, settings.BULK_MAX_UPLOAD_BYTES))
    try:
        return upload.read().decode('utf-8')
    except UnicodeDecodeError as exc:
        raise BulkIngestError('upload must be UTF-8 encoded: {0}'.format(exc))


def _first_line(text) -> str:
    """
    The first line of an exception message.

    Postgres reports a constraint violation over two lines -- the violation, then a
    DETAIL line naming the conflicting values. Only the first carries anything a
    caller can act on, and the second turns every skipped record in the report into a
    multi-line string that reads badly in JSON and worse in the page's table.
    """
    stripped = str(text).strip()
    return stripped.splitlines()[0] if stripped else ''


def _failure(errors, index=None, line=None) -> dict:
    entry = {'index': index, 'status': FAILED, 'errors': errors}
    if line is not None:
        entry['line'] = line
    return entry


def records_from_jsonl(text: str) -> tuple:
    """One JSON object per line. Blank lines are skipped, bad lines are reported."""
    records = []
    failures = []
    index = 0
    for line_number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except ValueError as exc:
            failures.append(_failure([{'json': str(exc)}], line=line_number))
            continue
        if not isinstance(record, dict):
            failures.append(_failure([{'json': 'expected a JSON object'}], line=line_number))
            continue
        record = dict(record)
        record['index'] = index
        record['line'] = line_number
        records.append(record)
        index += 1
    return records, failures


def records_from_bibtex(text: str) -> tuple:
    """
    Every entry in a BibTeX document. Entries missing a title, author or year are
    reported at their position rather than silently dropped, and each usable entry
    carries the single-entry BibTeX it came from, so the stored record keeps the
    fields this app does not model and the @type it was uploaded as.
    """
    entries, errors = parse_bibtex_entries(text)
    failures = [_failure([{'bibtex': error['error']}], index=error['index'])
                for error in errors]
    return entries, failures


def records_from_json(data) -> tuple:
    """A bare JSON list of records, or an object with a 'publications' list."""
    payload = data
    if isinstance(data, dict):
        payload = data.get('publications', None)
    if not isinstance(payload, list):
        raise BulkIngestError(
            "expected a JSON list of publications, or an object with a 'publications' list")
    records = []
    failures = []
    for index, record in enumerate(payload):
        if not isinstance(record, dict):
            failures.append(_failure([{'json': 'expected a JSON object'}], index=index))
            continue
        record = dict(record)
        record['index'] = index
        records.append(record)
    return records, failures


def collect_records(*, data=None, upload=None) -> tuple:
    """
    Normalise a request into (records, failures), refusing the whole request if it is
    over a cap or in a shape this endpoint cannot read. Nothing is written here.
    """
    if upload is not None:
        name = (upload.name or '').lower()
        text = _read_upload(upload)
        if name.endswith(JSONL_SUFFIXES):
            records, failures = records_from_jsonl(text)
        elif name.endswith(BIBTEX_SUFFIXES):
            records, failures = records_from_bibtex(text)
        else:
            raise BulkIngestError(
                "unsupported file type '{0}': expected {1}".format(
                    upload.name, ', '.join(JSONL_SUFFIXES + BIBTEX_SUFFIXES)))
    else:
        records, failures = records_from_json(data)

    if len(records) > settings.BULK_MAX_RECORDS:
        raise BulkIngestError(
            '{0} records is over the limit of {1} for one request'.format(
                len(records), settings.BULK_MAX_RECORDS))
    return records, failures


def _position(entry) -> int:
    """Sort key that puts the report back into the order of the uploaded document."""
    for key in ('line', 'index'):
        if entry.get(key) is not None:
            return entry[key]
    return -1


def ingest(records, failures=None, *, api_user, resolve_project_name=None) -> dict:
    """
    Create one publication per record and report on every one of them.

    resolve_project_name should be the memoised resolver: a 1000-record upload that
    names one project must make one core-api call, not a thousand.
    """
    results = list(failures or [])
    created = 0
    skipped = 0
    for record in records:
        index = record.get('index')
        line = record.get('line')
        bibtex = record.get('bibtex', None)
        errors = validate_publication_data(
            record, parse_bibtex(bibtex) if bibtex else {}, required=True)
        if errors:
            results.append(_failure(errors, index=index, line=line))
            continue
        try:
            publication = create_publication(
                data=record, api_user=api_user, resolve_project_name=resolve_project_name)
        except IntegrityError as exc:
            # The unique constraint on title/link. Re-uploading a file that overlaps
            # what is already stored is the ordinary case, not an error: the record is
            # skipped and the batch carries on.
            skipped += 1
            entry = {'index': index, 'status': SKIPPED, 'reason': _first_line(exc)}
            if line is not None:
                entry['line'] = line
            results.append(entry)
            continue
        except Exception as exc:
            results.append(_failure([{'exception': _first_line(exc)}], index=index, line=line))
            continue
        created += 1
        entry = {'index': index, 'status': CREATED, 'uuid': publication.uuid}
        if line is not None:
            entry['line'] = line
        results.append(entry)

    results.sort(key=_position)
    return {
        'total': len(results),
        'created': created,
        'skipped': skipped,
        'failed': sum(1 for entry in results if entry['status'] == FAILED),
        'results': results,
    }

"""Extract (uuid, project_uuid, observed_at) CSV from a plain pg_dump of this database (#72).

The 2026-09-21 `--full` sync removed 156 ended memberships from ApiUser.projects; the
pre-deploy dumps are the only record of them. This reads the ApiUser COPY block out of a
`.sql` or `.sql.gz` dump and writes one CSV row per project in every user's `projects`,
for `manage.py import_project_memberships` to load.

Standalone on purpose -- no Django, no database, no .env, and nothing newer than
Python 3.6, which is what a RHEL 8 host's system `python3` is. It runs on the host where the
dumps live, and its output is a plain file an operator can read before anything is
imported.

observed_at is when that row's projects were last *written*, which is not the dump time:
a stale row -- the whole reason this exists -- still lists a project the person left
weeks before the dump. So it is the later of `last_synced` (the directory sync) and
`access_expires` (a login refresh sets it a few minutes after its write -- close
enough), capped at the dump time, and the dump time only when neither is set.

Usage:
    python3 scripts/extract-memberships-from-dump.py DUMP.sql.gz > memberships.csv
    python3 scripts/extract-memberships-from-dump.py DUMP.sql.gz --dumped-at '2026-09-21 18:53:39+00:00'

The dump time is read from the UTC stamp in the file name -- `YYYYMMDD-HHMMSS` or
`YYYYMMDDTHHMMSSZ`, both of which the pre-deploy backups use -- unless --dumped-at is
given.

Dumps from before v1.12.0 have no `last_synced` column (0003 added it). They are read
all the same, dated by `access_expires` alone: every row in them came from a login.
"""

import argparse
import csv
import gzip
import re
import sys
from datetime import datetime, timedelta, timezone

TABLE = 'apiuser_apiuser'
COPY_RE = re.compile(r'^COPY (?:public\.)?"?(?P<table>\w+)"? \((?P<columns>[^)]*)\) FROM stdin;$')
# What pg_dump writes for timestamptz: fractional seconds optional, offset '+00' or
# '+05:30'. Parsed by hand because datetime.fromisoformat needs Python 3.7.
TIMESTAMP_RE = re.compile(
    r'^(\d{4})-(\d\d)-(\d\d)[ T](\d\d):(\d\d):(\d\d)(?:\.(\d{1,6}))?'
    r'(?:([+-])(\d\d)(?::?(\d\d))?)?$'
)
STAMP_RE = re.compile(r'(\d{8})[-T](\d{6})Z?')
ESCAPES = {'b': '\b', 'f': '\f', 'n': '\n', 'r': '\r', 't': '\t', 'v': '\v', '\\': '\\'}


class DumpError(Exception):
    pass


def unescape(field):
    """One COPY text-format field. None for SQL NULL."""
    if field == '\\N':
        return None
    return re.sub(r'\\(.)', lambda m: ESCAPES.get(m.group(1), m.group(1)), field)


def parse_array(text):
    """A Postgres text[] literal: {a,b} or {"a b",c}. Only one dimension is ever stored."""
    if text is None:
        return []
    if not (text.startswith('{') and text.endswith('}')):
        raise DumpError('not an array literal: {0!r}'.format(text))
    body, items, i = text[1:-1], [], 0
    while i < len(body):
        if body[i] == '"':
            j, value = i + 1, []
            while body[j] != '"':
                if body[j] == '\\':
                    j += 1
                value.append(body[j])
                j += 1
            items.append(''.join(value))
            i = j + 2  # closing quote and the comma after it
        else:
            j = body.find(',', i)
            j = len(body) if j == -1 else j
            value = body[i:j]
            items.append(None if value == 'NULL' else value)
            i = j + 1
    return [item for item in items if item]


def parse_timestamp(text):
    """A timestamptz as pg_dump writes it, or an ISO 8601 --dumped-at. Naive if no offset."""
    if not text:
        return None
    match = TIMESTAMP_RE.match(text.strip())
    if not match:
        raise DumpError('not a timestamp: {0!r}'.format(text))
    year, month, day, hour, minute, second, fraction, sign, off_h, off_m = match.groups()
    tzinfo = None
    if sign:
        offset = timedelta(hours=int(off_h), minutes=int(off_m or 0))
        tzinfo = timezone(-offset if sign == '-' else offset)
    return datetime(int(year), int(month), int(day), int(hour), int(minute), int(second),
                    int((fraction or '0').ljust(6, '0')), tzinfo=tzinfo)


def dump_time_from_name(path):
    match = STAMP_RE.search(path)
    if not match:
        return None
    return datetime.strptime(''.join(match.groups()), '%Y%m%d%H%M%S').replace(tzinfo=timezone.utc)


def extract(lines, dumped_at, table=TABLE):
    """Yield (uuid, project_uuid, observed_at) for every project on every row of `table`."""
    columns = None
    for line in lines:
        line = line.rstrip('\n')
        if columns is None:
            match = COPY_RE.match(line)
            if match and match.group('table') == table:
                columns = [c.strip().strip('"') for c in match.group('columns').split(',')]
                missing = {'uuid', 'projects', 'access_expires'} - set(columns)
                if missing:
                    raise DumpError('{0} has no column(s) {1}'.format(table, sorted(missing)))
            continue
        if line == '\\.':
            return
        row = dict(zip(columns, (unescape(f) for f in line.split('\t'))))
        written = [t for t in (parse_timestamp(row.get('last_synced')),
                               parse_timestamp(row['access_expires'])) if t]
        observed_at = min(max(written), dumped_at) if written else dumped_at
        for project_uuid in sorted(set(parse_array(row['projects']))):
            yield row['uuid'], project_uuid, observed_at
    if columns is None:
        raise DumpError('no COPY block for {0} in this dump'.format(table))
    raise DumpError('COPY block for {0} is not terminated -- truncated dump?'.format(table))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('dump', help='plain-format pg_dump, .sql or .sql.gz')
    parser.add_argument('--dumped-at', help='when the dump was taken (ISO 8601, with offset)')
    parser.add_argument('--table', default=TABLE)
    args = parser.parse_args(argv)

    try:
        dumped_at = parse_timestamp(args.dumped_at) if args.dumped_at else dump_time_from_name(args.dump)
    except DumpError as exc:
        parser.error(str(exc))
    if dumped_at is None:
        parser.error('no YYYYMMDD-HHMMSS or YYYYMMDDTHHMMSSZ stamp in the file name; pass --dumped-at')
    if dumped_at.tzinfo is None:
        parser.error('--dumped-at needs a UTC offset')

    opener = gzip.open if args.dump.endswith('.gz') else open
    writer = csv.writer(sys.stdout)
    writer.writerow(['uuid', 'project_uuid', 'observed_at'])
    rows = users = 0
    last_uuid = None
    try:
        with opener(args.dump, 'rt', encoding='utf-8') as lines:
            for uuid, project_uuid, observed_at in extract(lines, dumped_at, args.table):
                # UTC, whatever the dumping session's TimeZone was, so two CSVs can be
                # compared by eye.
                writer.writerow([uuid, project_uuid, observed_at.astimezone(timezone.utc).isoformat()])
                rows += 1
                if uuid != last_uuid:
                    users, last_uuid = users + 1, uuid
    except DumpError as exc:
        print('extract-memberships: {0}'.format(exc), file=sys.stderr)
        return 1
    print('extract-memberships: {0} membership(s) for {1} user(s); dump taken {2}'.format(
        rows, users, dumped_at.isoformat()), file=sys.stderr)
    return 0


if __name__ == '__main__':
    sys.exit(main())

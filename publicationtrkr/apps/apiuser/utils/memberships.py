"""
Record project membership history (#72). The only writer of ApiUserProjectMembership.

Three callers offer observations: the directory sync, the login refresh, and the import
of pre-deploy dumps. They arrive in no particular date order -- an import replays a dump
older than rows the sync already wrote -- so the upsert is order-independent:

  * `first_seen` only ever moves earlier (LEAST), and `source` moves with it, so it keeps
    naming whoever supplied the date it describes;
  * `last_seen` only ever moves later (GREATEST).

That is also why this is raw SQL rather than `bulk_create(update_conflicts=True)`: that
assigns the incoming value outright, and replaying an old dump would drag `last_seen`
backwards and relabel a synced row as seeded.

**Nothing here deletes.** A project missing from `projects` is simply not refreshed; its
row keeps its last `last_seen`. That absence is the whole record of a membership ending.
"""

from django.db import connection

from publicationtrkr.apps.apiuser.models import ApiUserProjectMembership


def record_memberships(api_user_id: int, projects, seen_at, source: str) -> int:
    """
    Upsert one observation of `projects` for one user. Returns how many rows were new.

    `projects` is the full current list or any subset of it; blanks and duplicates are
    dropped, because Postgres refuses an ON CONFLICT statement that touches the same row
    twice. An empty list is a no-op, never a deletion.
    """
    projects = sorted({p for p in (projects or []) if p})
    if not projects:
        return 0
    if source not in dict(ApiUserProjectMembership.SOURCE_CHOICES):
        raise ValueError('unknown membership source {0!r}'.format(source))
    table = connection.ops.quote_name(ApiUserProjectMembership._meta.db_table)
    with connection.cursor() as cursor:
        cursor.execute(
            '''
            INSERT INTO {t} AS m (api_user_id, project_uuid, first_seen, last_seen, source)
            SELECT %s, p, %s, %s, %s FROM unnest(%s::varchar[]) AS p
            ON CONFLICT (api_user_id, project_uuid) DO UPDATE SET
                source = CASE WHEN EXCLUDED.first_seen < m.first_seen
                              THEN EXCLUDED.source ELSE m.source END,
                first_seen = LEAST(m.first_seen, EXCLUDED.first_seen),
                last_seen = GREATEST(m.last_seen, EXCLUDED.last_seen)
            RETURNING (xmax = 0)
            '''.format(t=table),
            [api_user_id, seen_at, seen_at, source, projects],
        )
        # xmax is 0 on a freshly inserted tuple and non-zero on one ON CONFLICT updated.
        return sum(1 for (inserted,) in cursor.fetchall() if inserted)


def load_last_seen() -> dict:
    """{api_user uuid: {project_uuid: last_seen}} for every membership ever recorded."""
    last_seen = {}
    for api_user_uuid, project_uuid, seen in ApiUserProjectMembership.objects.values_list(
            'api_user__uuid', 'project_uuid', 'last_seen').iterator():
        last_seen.setdefault(api_user_uuid, {})[project_uuid] = seen
    return last_seen

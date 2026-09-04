"""
Postgres advisory locks, shared by every command that must not run twice at once.

This lived inside sync_fabric_users. A second scheduled command (score_author_claims,
issue #32) needs the identical guard, and the choice was to copy eleven lines or to lift
them; copied locking primitives drift, and a drifted one fails silently -- both copies
take a lock, but of a different flavour or a different key, and neither excludes the
other.

The lock is session-level and tied to the command's own database connection, so it is
released when the process ends, crash included. Keys are integers in one namespace:
every caller's key belongs in KEYS below, so that a collision is visible in one place
rather than discovered when two unrelated jobs mysteriously exclude each other.
"""

from contextlib import contextmanager

from django.db import connection

# One namespace, one place to look. Each key records what it protects.

# Two syncs running concurrently -- the cron sidecar firing while an operator runs a
# backfill by hand -- would interleave their watermark writes, and the loser's window
# would be recorded as covered when it was not.
SYNC_ADVISORY_LOCK_KEY = 823_100_023

# Two scoring runs would race on the same (author, api_user) rows. The unique constraint
# makes that a crash rather than a duplicate, which is safe but noisy; more to the point,
# a run started by hand while the 03:30 job is working would double the query load
# against a table scan of every unclaimed author.
CLAIM_SCORING_ADVISORY_LOCK_KEY = 823_100_032

KEYS = {
    'sync_fabric_users': SYNC_ADVISORY_LOCK_KEY,
    'score_author_claims': CLAIM_SCORING_ADVISORY_LOCK_KEY,
}


@contextmanager
def advisory_lock(key: int):
    """
    Hold a Postgres session-level advisory lock, or yield False if another run holds it.

    Yields True when the lock was acquired and False when it was not; it never blocks
    and never raises on contention. The caller decides whether losing the race is a
    routine skip or an error.
    """
    with connection.cursor() as cursor:
        cursor.execute('SELECT pg_try_advisory_lock(%s)', [key])
        acquired = cursor.fetchone()[0]
    try:
        yield acquired
    finally:
        if acquired:
            with connection.cursor() as cursor:
                cursor.execute('SELECT pg_advisory_unlock(%s)', [key])

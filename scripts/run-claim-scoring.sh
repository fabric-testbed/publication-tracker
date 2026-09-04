#!/usr/bin/env bash
set -uo pipefail

# Wrapper invoked by the cron sidecar's crontab for author-claim scoring (#32). A twin of
# run-user-sync.sh: cron hands a job almost no environment, so everything the command
# needs is established here.
#
# flock stops two runs colliding on this host. It is belt-and-braces: score_author_claims
# also takes a Postgres advisory lock, which is the guard that actually holds across
# containers -- an operator running the backfill by hand in the django container is not on
# this host's lock file. The lock file is its own, not the sync's: the two jobs write
# different tables and there is no reason for one to block the other.

cd /code

# Without `set -e`, so a failed `source` does not carry on with no environment at all and
# die thirty traceback lines later on "DJANGO_SECRET_KEY is not set", which names the
# wrong problem. The job runs as appuser (uid/gid 20049) from 1.13.1 on, so an unreadable
# .env is a real possibility.
if [ ! -r .env ]; then
    echo "claim scoring: FAILED - /code/.env is not readable by $(id -un) (uid $(id -u), gid $(id -g))"
    echo "claim scoring: share it by group -- chown <operator>:20049 .env && chmod 0640 .env"
    exit 1
fi
source .env

# The venv's interpreter by absolute path rather than a bare `python` off PATH. Under cron
# there is no inherited PATH to speak of, and a `python` that silently resolved to the
# system interpreter would fail on the first import.
PYTHON=/opt/venv/bin/python

echo "=== claim scoring starting $(date -u '+%Y-%m-%d %H:%M:%S') UTC ==="

# -E 100 gives "someone else holds the lock" its own exit code, so it can be reported as
# the routine skip it is rather than as a failure.
flock --nonblock --conflict-exit-code 100 /tmp/claim-scoring.lock \
  "${PYTHON}" manage.py score_author_claims "$@"
status=$?

if [ "${status}" -eq 100 ]; then
    echo "claim scoring: another run on this host holds the lock - skipped"
    status=0
elif [ "${status}" -ne 0 ]; then
    echo "claim scoring: FAILED with exit ${status}"
fi

echo "=== claim scoring finished $(date -u '+%Y-%m-%d %H:%M:%S') UTC ==="
exit "${status}"

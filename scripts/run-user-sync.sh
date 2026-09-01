#!/usr/bin/env bash
set -uo pipefail

# Wrapper invoked by the user-sync sidecar's crontab. Cron hands a job almost no
# environment, so everything the command needs is established here.
#
# flock stops two runs colliding on this host. It is belt-and-braces: sync_fabric_users
# also takes a Postgres advisory lock, which is the guard that actually holds across
# containers -- an operator running a backfill by hand in the django container is not
# on this host's lock file.

cd /code
source .env

# The venv's interpreter by absolute path rather than a bare `python` off PATH. Under
# cron there is no inherited PATH to speak of, and a `python` that silently resolved to
# the system interpreter would fail on the first import.
PYTHON=/code/.venv/bin/python

echo "=== user sync starting $(date -u '+%Y-%m-%d %H:%M:%S') UTC ==="

# -E 100 gives "someone else holds the lock" its own exit code, so it can be reported
# as the routine skip it is rather than as a failure.
flock --nonblock --conflict-exit-code 100 /tmp/user-sync.lock \
  "${PYTHON}" manage.py sync_fabric_users "$@"
status=$?

if [ "${status}" -eq 100 ]; then
    echo "user sync: another run on this host holds the lock - skipped"
    status=0
elif [ "${status}" -ne 0 ]; then
    echo "user sync: FAILED with exit ${status}"
fi

echo "=== user sync finished $(date -u '+%Y-%m-%d %H:%M:%S') UTC ==="
exit "${status}"

#!/usr/bin/env bash
set -euo pipefail

# Entrypoint for the user-sync sidecar (the `cron` service in docker-compose.yml).
#
# Deliberately NOT docker-entrypoint.sh. That one calls run_server.sh, which applies
# migrations, loads fixtures, collects static files and execs uwsgi -- a second
# container doing any of that would race the django container at every boot. This one
# waits for its dependencies and then does nothing but run cron.

source .env

# The django container owns .venv: its entrypoint runs `uv sync` at boot against the
# same ./:/code bind mount. Two containers syncing one virtualenv concurrently is a
# race, so wait for it to appear rather than creating it here.
until [ -x /code/.venv/bin/python ]; do
  >&2 echo "cron sidecar: waiting for /code/.venv (created by the django container)"
  sleep 2
done
>&2 echo "cron sidecar: /code/.venv is present"

until pg_isready -h "${POSTGRES_HOST}" -q; do
  >&2 echo "cron sidecar: Postgres is unavailable - sleeping"
  sleep 1
done
>&2 echo "cron sidecar: Postgres is up"

SCHEDULE="${USER_SYNC_CRON_SCHEDULE:-0 3 * * *}"

# Output goes to /proc/1/fd/1 -- cron's own stdout, and therefore the container's, so
# the run shows up in `docker compose logs`. Cron's default is to mail its output,
# which in a container goes nowhere at all.
#
# The job runs a wrapper rather than `python manage.py ...` directly because cron gives
# a job almost no environment: the wrapper sources .env and the virtualenv itself.
cat >/etc/cron.d/user-sync <<CRON
SHELL=/bin/bash
${SCHEDULE} root /code/scripts/run-user-sync.sh >/proc/1/fd/1 2>/proc/1/fd/2
CRON

# /etc/cron.d entries are ignored unless they are root-owned, mode 0644, and newline
# terminated. The heredoc handles the newline; these handle the rest.
chown root:root /etc/cron.d/user-sync
chmod 0644 /etc/cron.d/user-sync

>&2 echo "cron sidecar: user sync scheduled as '${SCHEDULE}'"
exec cron -f

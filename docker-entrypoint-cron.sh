#!/usr/bin/env bash
set -euo pipefail

# Entrypoint for the user-sync sidecar (the `cron` service in docker-compose.yml).
#
# Deliberately NOT docker-entrypoint.sh. That one calls run_server.sh, which applies
# migrations, loads fixtures, collects static files and execs uwsgi -- a second
# container doing any of that would race the django container at every boot. This one
# waits for its dependencies and then does nothing but run cron.

source .env

# The wait for /code/.venv is gone. It existed because both containers shared one
# virtualenv on the bind mount and the django container created it at boot; the
# virtualenv now ships in the image at /opt/venv, so this container has its own copy
# before it starts and there is nothing to race.

until pg_isready -h "${POSTGRES_HOST}" -q; do
  >&2 echo "cron sidecar: Postgres is unavailable - sleeping"
  sleep 1
done
>&2 echo "cron sidecar: Postgres is up"

SCHEDULE="${USER_SYNC_CRON_SCHEDULE:-0 3 * * *}"
# Author-claim scoring (#32). Default is half an hour after the user sync, and that
# ordering is the point: scoring's strongest signal is project co-membership read out of
# ApiUser.projects, so a run that goes first scores against yesterday's directory.
CLAIM_SCHEDULE="${CLAIM_SCORING_CRON_SCHEDULE:-30 3 * * *}"

# Output goes to /proc/1/fd/1 -- cron's own stdout, and therefore the container's, so
# the run shows up in `docker compose logs`. Cron's default is to mail its output,
# which in a container goes nowhere at all.
#
# The job runs a wrapper rather than `python manage.py ...` directly because cron gives
# a job almost no environment: the wrapper sources .env and names the interpreter by
# absolute path.
#
# The sync itself has no reason to run as root and does not (#31 finding 9), but the
# cron line's user field stays `root` and drops privilege with `su` rather than naming
# appuser directly. Those are not equivalent: /proc/1/fd/1 is root-owned, so an
# appuser-owned cron job cannot open it and the redirect fails with EACCES -- the run
# would happen and its entire output would disappear, cron mailing it into a container
# with no mailer. Redirecting in the root shell and then dropping means the unprivileged
# child inherits an already-open descriptor, which needs no permission on /proc at all.
# Verified: the job logs `uid=20049(appuser)` and its output still reaches
# `docker compose logs`.
cat >/etc/cron.d/user-sync <<CRON
SHELL=/bin/bash
${SCHEDULE} root su appuser -s /bin/bash -c "/code/scripts/run-user-sync.sh" >/proc/1/fd/1 2>/proc/1/fd/2
CRON

# Its own file rather than a second line in user-sync's, so that a broken schedule in one
# cannot take the other down with it: cron rejects a malformed /etc/cron.d file whole.
#
# Deliberately NOT --if-due. The command has that flag, and it looks like the right thing
# here until you notice that the cron schedule and the CLM_ cadence are both daily. The
# tracker is stamped when a run *finishes*, so the next night's run starts a few seconds
# short of 86400s later, `timed_out()` is false, and scoring is skipped -- every other
# night, silently. --if-due is for a sidecar firing more often than the cadence; this one
# fires exactly on it, so the schedule is the cadence and the tracker is a record of the
# last run.
cat >/etc/cron.d/claim-scoring <<CRON
SHELL=/bin/bash
${CLAIM_SCHEDULE} root su appuser -s /bin/bash -c "/code/scripts/run-claim-scoring.sh" >/proc/1/fd/1 2>/proc/1/fd/2
CRON

# /etc/cron.d entries are ignored unless they are root-owned, mode 0644, and newline
# terminated. The heredoc handles the newline; these handle the rest.
chown root:root /etc/cron.d/user-sync /etc/cron.d/claim-scoring
chmod 0644 /etc/cron.d/user-sync /etc/cron.d/claim-scoring

>&2 echo "cron sidecar: user sync scheduled as '${SCHEDULE}'"
>&2 echo "cron sidecar: claim scoring scheduled as '${CLAIM_SCHEDULE}'"
exec cron -f

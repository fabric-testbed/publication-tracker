#!/usr/bin/env bash
set -euo pipefail

# The container reads .env as uid/gid 20049, which it did not have to do when it ran as
# root. An unreadable .env otherwise fails as `line 4: .env: Permission denied` and,
# under `restart: unless-stopped`, becomes a restart loop rather than an error anyone
# reads. Say what is wrong once, plainly.
if [ ! -r .env ]; then
    >&2 echo "FATAL: /code/.env is not readable by $(id -un) (uid $(id -u), gid $(id -g))."
    >&2 echo "       This container runs unprivileged as of 1.13.1. On the production host"
    >&2 echo "       the checkout is owned by nrig-service (uid 20049) and nothing needs to"
    >&2 echo "       change; where the operator account differs, share it by group:"
    >&2 echo "         chown <operator>:20049 .env && chmod 0640 .env"
    >&2 echo "       Note that editors which replace the file rather than write in place"
    >&2 echo "       (sed -i, vim's default) reset that group -- re-check after editing."
    exit 1
fi
source .env

# No `uv sync` here any more, and no venv to activate. Dependencies are installed into
# /opt/venv at build time with `uv sync --frozen`, and the image puts /opt/venv/bin on
# PATH -- so `python` below is already the project interpreter. A restart can no longer
# re-resolve, and nothing writes a venv back into the host's checkout (#31 finding 9).
# The consequence for operators: a release that moves uv.lock needs `docker compose
# build`, not just a restart.

until [ "$(pg_isready -h database -q)"$? -eq 0 ]; do
  >&2 echo "Postgres is unavailable - sleeping"
  sleep 1
done

>&2 echo "Postgres is up - continuing"

# MAKE_MIGRATIONS is deliberately gone. Migrations are committed to the repo
# and applied by run_server.sh; they are never generated at container start.
if [ "${LOAD_FIXTURES:-0}" -eq 1 ]; then
    ./run_server.sh --run-mode docker --load-fixtures
else
    ./run_server.sh --run-mode docker
fi

exec "$@"

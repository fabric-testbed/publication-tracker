#!/usr/bin/env bash
set -euo pipefail

source .env
uv sync
source .venv/bin/activate

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

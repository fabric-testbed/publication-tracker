#!/usr/bin/env bash
set -e

source .env
uv sync
source .venv/bin/activate

until [ "$(pg_isready -h database -q)"$? -eq 0 ]; do
  >&2 echo "Postgres is unavailable - sleeping"
  sleep 1
done

>&2 echo "Postgres is up - continuing"

if [ "${LOAD_FIXTURES}" -eq 1 ] && [ "${MAKE_MIGRATIONS}" -eq 1 ]; then
    ./run_server.sh --run-mode docker --load-fixtures --make-migrations
elif [ "${LOAD_FIXTURES}" -eq 1 ]; then
    ./run_server.sh --run-mode docker --load-fixtures
elif [ "${MAKE_MIGRATIONS}" -eq 1 ]; then
    ./run_server.sh --run-mode docker --make-migrations
else
    ./run_server.sh --run-mode docker
fi

exec "$@"

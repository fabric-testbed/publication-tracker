#!/usr/bin/env bash

# Fail loudly. A failed migration must stop the boot, not fall through to
# uwsgi and serve traffic against a database missing the migration.
set -euo pipefail

PARAMS=""
while (("$#")); do
    case "$1" in
    -l | --load-fixtures)
        LOAD_FIXTURES=1
        shift
        ;;
    -r | --run-mode)
        if [ -n "$2" ] && [ "${2:0:1}" != "-" ]; then
            RUN_MODE=$2
            shift 2
            case "$RUN_MODE" in
                local-dev | local-ssl | docker)
                    ;;
                *)
                    echo "InvalidRunMode: -r | --run-mode <local-dev | local-ssl | docker>"
                    exit 1
                    ;;
            esac
        else
            echo "Error: Argument for $1 is missing" >&2
            exit 1
        fi
        ;;
    -* | --*=) # unsupported flags
        echo "Error: Unsupported flag $1" >&2
        exit 1
        ;;
    *) # preserve positional arguments
        PARAMS="$PARAMS $1"
        shift
        ;;
    esac
done
# set positional arguments in their proper place
eval set -- "$PARAMS"

# ensure a valid run-mode was set
case "${RUN_MODE:-}" in
    local-dev | local-ssl | docker)
        ;;
    *)
        echo "InvalidRunMode: -r | --run-mode <local-dev | local-ssl | docker>"
        exit 1
        ;;
esac

# load fixtures
if [[ "${LOAD_FIXTURES:-0}" -eq 1 ]]; then
    echo "### LOAD_FIXTURES = True ###"
    FIXTURES_LIST=(
        "apiuser"
        "publications"
        "pubsimple"
    )
else
    echo "### LOAD_FIXTURES = False ###"
    FIXTURES_LIST=()
fi

# migrations
#
# Migration files are committed to the repo and are NOT generated here. This
# script only applies what was reviewed and merged. If you changed a model,
# run `python manage.py makemigrations` on your workstation and commit the
# result -- see "Database Migrations" in README.md.
#
# The --check below asserts that the committed migrations still describe the
# current models. It exits non-zero if a model change was merged without its
# migration, which (with set -e) stops the boot rather than letting the app
# serve against a schema that does not match the code.
echo "### VERIFY committed migrations match models ###"
if ! python manage.py makemigrations --check --dry-run; then
    echo "" >&2
    echo "ERROR: models have changes with no corresponding committed migration." >&2
    echo "       Run 'python manage.py makemigrations' locally, review the" >&2
    echo "       generated file, and commit it. Refusing to start." >&2
    exit 1
fi

echo "### APPLY migrations ###"
python manage.py showmigrations
python manage.py migrate

# load fixtures
for fixture in "${FIXTURES_LIST[@]+"${FIXTURES_LIST[@]}"}"; do
    python manage.py loaddata $fixture
done

# static files
python manage.py collectstatic --noinput

# initialize task timeout tracker
echo "### INIT task timeout tracker ###"
python manage.py init_task_timeout_tracker

# initialize anonymous api user
echo "### INIT anonymous api_user ###"
python manage.py init_anon_api_user

# run mode
case "${RUN_MODE}" in
    local-dev)
        echo "local-dev"
        python manage.py runserver 0.0.0.0:8000
        ;;
    local-ssl)
        echo "local-ssl"
        uwsgi --uid "${UWSGI_UID:-1000}" --gid "${UWSGI_GID:-1000}" --virtualenv ./.venv --ini publicationtrkr.ini
        ;;
    docker)
        echo "docker"
        uwsgi --uid "${UWSGI_UID:-1000}" --gid "${UWSGI_GID:-1000}" --virtualenv ./.venv --ini publicationtrkr.ini
        ;;
    *)
        echo "ModeRequired: -r | --run-mode <local-dev | local-ssl | docker>"
        exit 1
        ;;
esac

exit 0

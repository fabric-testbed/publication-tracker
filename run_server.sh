#!/usr/bin/env bash

PARAMS=""
while (("$#")); do
    case "$1" in
    -m | --make-migrations)
        MAKE_MIGRATIONS=1
        shift
        ;;
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
case "$RUN_MODE" in
    local-dev | local-ssl | docker)
        ;;
    *)
        echo "InvalidRunMode: -r | --run-mode <local-dev | local-ssl | docker>"
        exit 1
        ;;
esac

# make app migrations
if [[ "${MAKE_MIGRATIONS}" -eq 1 ]]; then
    echo "### MAKE_MIGRATIONS = True ###"
    APPS_LIST=(
        "apiuser"
        "pubsimple"
    )
else
    echo "### MAKE_MIGRATIONS = False ###"
    APPS_LIST=()
fi

# load fixtures
if [[ "${LOAD_FIXTURES}" -eq 1 ]]; then
    echo "### LOAD_FIXTURES = True ###"
    FIXTURES_LIST=(
        "apiuser"
        "pubsimple"
    )
else
    echo "### LOAD_FIXTURES = False ###"
    FIXTURES_LIST=()
fi

# migrations files
for app in "${APPS_LIST[@]}"; do
    python manage.py makemigrations $app
done
python manage.py makemigrations
python manage.py showmigrations
python manage.py migrate

# load fixtures
for fixture in "${FIXTURES_LIST[@]}"; do
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
        uwsgi --uid "${UWSGI_UID:-1000}" --gid "${UWSGI_GID:-1000}" --virtualenv ./venv --ini publicationtrkr.ini
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
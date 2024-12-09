#!/usr/bin/env bash

APPS_LIST=(
    "apiuser"
    "pubsimple"
)

for app in "${APPS_LIST[@]}";do
    echo "python3 ./manage.py dumpdata ${app} --indent 2 --output ./dumpdata/${app}.json"
    python3 ./manage.py dumpdata "${app}" --indent 2 --output ./dumpdata/"${app}".json
done

exit 0;
#!/usr/bin/env bash
### Clean up the entire deployment and start from scratch

# cd to main level of repo
SCRIPT_DIR=$(pwd)
cd $(dirname ${SCRIPT_DIR})
source .env

# stop and remove all running docker containers, volumes and networks
echo "### ALERT: stopping and removing docker containers"
docker compose stop
docker compose rm -fv

# remove any altered migrations directories and re-check them out
echo "### ALERT: removing migrations files"
while read line; do
  echo "    - ${line}"
  rm -rf $line
done < <(find $(pwd) -type d \( -name venv -o -name .venv -o -path name \) -prune -false -o -name "migrations")

# TODO: force permissions of modified directories to be that of the local user
# remove any volume mounts for the database, static or media directories
echo "### ALERT: removing volume mounts"
rm -rf ${HOST_DB_DATA}

# TODO: force permissions of modified directories to be that of the local user
# create new volume mounts for the database, static or media directories with appropriate permissions
echo "### ALERT: replacing volume mounts"
mkdir -p ${HOST_DB_DATA}

cd $SCRIPT_DIR

exit 0;
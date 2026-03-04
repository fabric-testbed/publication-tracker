#!/usr/bin/env bash
#
# dumpdata.sh — Export all application data as Django fixture files.
#
# Produces one JSON fixture per app in ./dumpdata/ that can be reloaded
# with: python3 manage.py loaddata ./dumpdata/<app>.json
#
# Usage: ./dumpdata.sh            (from publication-tracker/publicationtrkr/)
#        ./dumpdata.sh --dry-run  (show commands without executing)

set -euo pipefail

# Resolve paths relative to this script's location
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MANAGE_PY="${SCRIPT_DIR}/manage.py"
DUMP_DIR="${SCRIPT_DIR}/dumpdata"

DRY_RUN=false
if [[ "${1:-}" == "--dry-run" ]]; then
    DRY_RUN=true
fi

# All apps that contain data models — order matters for foreign key dependencies
APPS_LIST=(
    "apiuser"
    "pubsimple"
    "publications"
)

# Common dumpdata flags
#   --natural-foreign : use natural keys for FK references (e.g., content types)
#                       so fixtures are portable across database instances
#   --natural-primary : use natural keys for primary keys where models define them
DUMP_FLAGS=(--indent 2 --natural-foreign --natural-primary)

# Ensure output directory exists
mkdir -p "${DUMP_DIR}"

ERRORS=0

for app in "${APPS_LIST[@]}"; do
    OUTPUT="${DUMP_DIR}/${app}.json"
    CMD="python3 ${MANAGE_PY} dumpdata ${app} ${DUMP_FLAGS[*]} --output ${OUTPUT}"

    echo ">>> ${CMD}"

    if [[ "${DRY_RUN}" == true ]]; then
        continue
    fi

    if ! python3 "${MANAGE_PY}" dumpdata "${app}" "${DUMP_FLAGS[@]}" --output "${OUTPUT}"; then
        echo "ERROR: dumpdata failed for app '${app}'" >&2
        ERRORS=$((ERRORS + 1))
        continue
    fi

    # Report file size as a quick sanity check
    if [[ -f "${OUTPUT}" ]]; then
        SIZE=$(wc -c < "${OUTPUT}" | tr -d ' ')
        echo "    -> ${OUTPUT} (${SIZE} bytes)"
    fi
done

# Write a metadata file with dump timestamp
if [[ "${DRY_RUN}" == false ]]; then
    cat > "${DUMP_DIR}/manifest.json" <<MANIFEST
{
  "dumped_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "apps": [$(printf '"%s", ' "${APPS_LIST[@]}" | sed 's/, $//')],
  "django_version": "$(python3 "${MANAGE_PY}" version 2>/dev/null || echo 'unknown')"
}
MANIFEST
    echo ">>> Manifest written to ${DUMP_DIR}/manifest.json"
fi

if [[ ${ERRORS} -gt 0 ]]; then
    echo ""
    echo "WARNING: ${ERRORS} app(s) failed to export." >&2
    exit 1
fi

echo ""
echo "Done. Fixtures saved to ${DUMP_DIR}/"
exit 0

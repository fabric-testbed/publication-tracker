#!/usr/bin/env bash
set -euo pipefail

# README.md and README-PUBLIC.md are maintained by hand and must carry the same edits.
# .publish-include renames README-PUBLIC.md into the public mirror as its README.md, so a
# release that updates one and not the other publishes a README describing the previous
# version -- a silent divergence nobody notices until someone follows the wrong
# instructions.
#
# The two are allowed to differ in exactly one line: the private tree ships the developer
# SSL certificates, the public one only describes generating them. That single exception
# is encoded below, so every *other* difference fails loudly.
#
# Run before cutting a release.

cd "$(dirname "$0")/.."

PRIVATE=README.md
PUBLIC=README-PUBLIC.md

# Substrings unique to the one line the two files may differ on. Deliberately narrow:
# "Development self-signed certificates" alone also matches the directory tree near the
# end of both files, which is identical in each and must stay compared.
PRIVATE_MARK='Development self-signed certificates are provided in'
PUBLIC_MARK='TODO: Generation of development self-signed certificates'
SENTINEL='@@KNOWN_README_EXCEPTION@@'

fail=0

check_one() {
    local file=$1 mark=$2 n
    n=$(grep -cF "${mark}" "${file}" || true)
    if [ "${n}" -ne 1 ]; then
        echo "ERROR: expected exactly one line matching" >&2
        echo "         '${mark}'" >&2
        echo "       in ${file}, found ${n}. The known exception has moved, been" >&2
        echo "       reworded, or been duplicated -- update this script to match." >&2
        return 1
    fi
}

check_one "${PRIVATE}" "${PRIVATE_MARK}" || fail=1
check_one "${PUBLIC}" "${PUBLIC_MARK}" || fail=1

if [ "${fail}" -eq 0 ]; then
    if ! diff -q <(sed "s|.*${PRIVATE_MARK}.*|${SENTINEL}|" "${PRIVATE}") \
                 <(sed "s|.*${PUBLIC_MARK}.*|${SENTINEL}|" "${PUBLIC}") >/dev/null; then
        echo "ERROR: ${PRIVATE} and ${PUBLIC} have diverged beyond the known exception." >&2
        echo "       Both need every release's edits. Difference (${PRIVATE} -> ${PUBLIC}):" >&2
        echo >&2
        diff -u <(sed "s|.*${PRIVATE_MARK}.*|${SENTINEL}|" "${PRIVATE}") \
                <(sed "s|.*${PUBLIC_MARK}.*|${SENTINEL}|" "${PUBLIC}") >&2 || true
        fail=1
    fi
fi

if [ "${fail}" -eq 0 ]; then
    echo "READMEs are in step (differing only on the known SSL-certificates line)."
fi

exit "${fail}"

#!/usr/bin/env bash
#
# publish-to-public.sh
#
# Manual, local equivalent of .github/workflows/publish-to-public.yml.
# Mirrors a released version of the private repo into the public repo by:
#   1. Cloning the private repo at the release tag (fresh checkout, like CI).
#   2. Staging the files listed in .publish-include (supports comments,
#      "dir/" entries, and "src:dest" renames).
#   3. Cloning the public repo, wiping its tracked content (except .git),
#      and copying the staged files in.
#   4. Committing + tagging, then pushing main + the tag (push is gated).
#
# Unlike the workflow, this uses YOUR git credentials (SSH/HTTPS) instead of
# the PUBLIC_REPO_PAT secret, and the release tag/name are passed as arguments.
#
# Prerequisite: the release tag must already exist on the private remote
# (push it before running, e.g. `git push origin v1.9.7`), because step 1
# clones the private repo at that tag — exactly as the workflow checks out
# the published release's commit.
#
# Usage:
#   scripts/publish-to-public.sh <release-tag> [release-name] [--push]
#
# Examples:
#   scripts/publish-to-public.sh v1.9.7
#   scripts/publish-to-public.sh v1.9.7 "Release 1.9.7"
#   scripts/publish-to-public.sh v1.9.7 "Release 1.9.7" --push   # push without prompting
#
# Without --push the script prepares and commits everything in a scratch
# clone, shows the diff, and stops before pushing so you can review.

set -euo pipefail

# ---- configuration --------------------------------------------------------
PRIVATE_REPO_URL="${PRIVATE_REPO_URL:-git@github.com:fabric-testbed/publication-tracker-dev.git}"
PUBLIC_REPO_URL="${PUBLIC_REPO_URL:-git@github.com:fabric-testbed/publication-tracker.git}"
SOURCE_SLUG="${SOURCE_SLUG:-fabric-testbed/publication-tracker-dev}"
# ---------------------------------------------------------------------------

usage() {
  echo "Usage: $0 <release-tag> [release-name] [--push]" >&2
  exit 1
}

[[ $# -lt 1 ]] && usage

RELEASE_TAG=""
RELEASE_NAME=""
AUTO_PUSH=0
for arg in "$@"; do
  case "$arg" in
    --push) AUTO_PUSH=1 ;;
    -h|--help) usage ;;
    *)
      if [[ -z "$RELEASE_TAG" ]]; then RELEASE_TAG="$arg"
      elif [[ -z "$RELEASE_NAME" ]]; then RELEASE_NAME="$arg"
      else echo "Unexpected argument: $arg" >&2; usage; fi
      ;;
  esac
done

[[ -z "$RELEASE_TAG" ]] && usage
RELEASE_NAME="${RELEASE_NAME:-Release ${RELEASE_TAG}}"

WORK="$(mktemp -d)"
PRIVATE="$WORK/private-repo"
PUBLIC="$WORK/public-repo"
STAGING="$WORK/staging"
trap 'echo; echo "Scratch dir left for inspection: $WORK"' EXIT

echo "=== Publishing ${RELEASE_TAG} (${RELEASE_NAME}) ==="
echo "Private: $PRIVATE_REPO_URL"
echo "Public:  $PUBLIC_REPO_URL"
echo "Scratch: $WORK"
echo

# 1. Fresh checkout of the private repo at the release tag (matches CI).
echo "--- Cloning private repo at ${RELEASE_TAG} ---"
git clone --quiet --depth 1 --branch "$RELEASE_TAG" "$PRIVATE_REPO_URL" "$PRIVATE"

# 2. Stage files per .publish-include (faithful port of the workflow's bash).
echo "--- Staging files from .publish-include ---"
mkdir -p "$STAGING"
while IFS= read -r line || [[ -n "$line" ]]; do
  line="$(echo "$line" | xargs)"
  [[ -z "$line" || "$line" == \#* ]] && continue

  if [[ "$line" == *":"* ]]; then                  # src:dest rename
    src="${line%%:*}"; dest="${line##*:}"
    if [[ -f "$PRIVATE/$src" ]]; then
      mkdir -p "$STAGING/$(dirname "$dest")"
      cp "$PRIVATE/$src" "$STAGING/$dest"
      echo "Copied (renamed): $src -> $dest"
    else
      echo "WARNING: Source file not found: $src"
    fi
  elif [[ "$line" == */ ]]; then                   # directory entry
    if [[ -d "$PRIVATE/$line" ]]; then
      mkdir -p "$STAGING/$line"
      cp -r "$PRIVATE/$line"* "$STAGING/$line" 2>/dev/null || true
      echo "Copied directory: $line"
    else
      echo "WARNING: Directory not found: $line"
    fi
  else                                             # single file
    if [[ -f "$PRIVATE/$line" ]]; then
      mkdir -p "$STAGING/$(dirname "$line")"
      cp "$PRIVATE/$line" "$STAGING/$line"
      echo "Copied file: $line"
    else
      echo "WARNING: File not found: $line"
    fi
  fi
done < "$PRIVATE/.publish-include"

echo
echo "=== Staged files ==="
find "$STAGING" -type f | sort

# 3. Clone the public repo.
echo
echo "--- Cloning public repo ---"
git clone --quiet "$PUBLIC_REPO_URL" "$PUBLIC"

# 4. Wipe public repo content (except .git) and copy staged files in.
echo "--- Syncing staged files into public repo ---"
find "$PUBLIC" -mindepth 1 -maxdepth 1 ! -name '.git' -exec rm -rf {} +
# `"$STAGING"/.` (NOT `/*`) — the glob does not match dotfiles.
cp -r "$STAGING"/. "$PUBLIC"/

echo
echo "=== Public repo contents ==="
find "$PUBLIC" -mindepth 1 -not -path "$PUBLIC/.git/*" -not -name '.git' | sort

# 5. Commit + tag.
cd "$PUBLIC"
# -f so the published .gitignore cannot suppress manifest-selected files.
git add -A -f
if git diff --cached --quiet; then
  echo
  echo "No changes to publish. Nothing to do."
  exit 0
fi

echo
echo "=== Change summary ==="
git diff --cached --stat

git commit --quiet -m "Release ${RELEASE_TAG}: ${RELEASE_NAME}

Published manually from private repo release.
Source: ${SOURCE_SLUG}@${RELEASE_TAG}"
git tag -a "$RELEASE_TAG" -m "Release ${RELEASE_TAG}"

# 6. Push (gated: requires --push or an interactive yes).
if [[ "$AUTO_PUSH" -ne 1 ]]; then
  echo
  read -r -p "Push 'main' and tag '${RELEASE_TAG}' to the public repo? [y/N] " reply
  [[ "$reply" =~ ^[Yy]$ ]] || { echo "Aborted before push. Review the clone at: $PUBLIC"; exit 0; }
fi

git push origin main
git push origin "$RELEASE_TAG"
echo
echo "Successfully published ${RELEASE_TAG} to the public repo."

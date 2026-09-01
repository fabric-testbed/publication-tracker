#!/usr/bin/env bash
#
# publish-to-public.sh
#
# Manual, local equivalent of .github/workflows/publish-to-public.yml.
# Mirrors a released version of the private repo into the public repo by:
#   1. Cloning the private repo at the release tag (fresh checkout, like CI).
#   2. Staging the files listed in .publish-include (supports comments,
#      "dir/" entries, and "src:dest" renames). Only git-tracked files are
#      staged, so untracked local secrets can never be published.
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
#    Every entry is expanded with `git ls-files` instead of a filesystem glob,
#    so ONLY git-tracked files can ever be staged. Untracked runtime secrets and
#    PII that live under published directories (.env, vouch/config, ssl/*.pem,
#    dumpdata/*.json) are therefore unpublishable by construction, not merely
#    absent because CI happens to check out tracked files only.
echo "--- Staging files from .publish-include ---"
mkdir -p "$STAGING"
STAGED_LIST="$WORK/staged-files.z"   # NUL-separated, staging-relative paths
: > "$STAGED_LIST"
UNMATCHED=0

# stage_tracked <src-path-in-private> <dest-path-in-staging>
stage_tracked() {
  mkdir -p "$STAGING/$(dirname "$2")"
  cp "$PRIVATE/$1" "$STAGING/$2"
  printf '%s\0' "$2" >> "$STAGED_LIST"
}

while IFS= read -r line || [[ -n "$line" ]]; do
  line="$(echo "$line" | xargs)"
  [[ -z "$line" || "$line" == \#* ]] && continue

  if [[ "$line" == *":"* ]]; then                  # src:dest rename
    src="${line%%:*}"; dest="${line##*:}"
    if git -C "$PRIVATE" ls-files --error-unmatch -- "$src" >/dev/null 2>&1; then
      stage_tracked "$src" "$dest"
      echo "Copied (renamed): $src -> $dest"
    else
      echo "WARNING: Source file is not tracked by git, skipped: $src" >&2
      UNMATCHED=$((UNMATCHED + 1))
    fi
  else                                             # directory or single file
    matched=0
    while IFS= read -r -d '' f; do
      stage_tracked "$f" "$f"
      matched=$((matched + 1))
    done < <(git -C "$PRIVATE" ls-files -z -- "$line")

    if [[ "$matched" -eq 0 ]]; then
      echo "WARNING: No tracked files match .publish-include entry: $line" >&2
      UNMATCHED=$((UNMATCHED + 1))
    elif [[ "$line" == */ ]]; then                 # directory entry
      echo "Copied directory: $line (${matched} tracked files)"
    else                                           # single file
      echo "Copied file: $line"
    fi
  fi
done < "$PRIVATE/.publish-include"

# Fail rather than shipping a partial mirror -- see the matching comment in
# .github/workflows/publish-to-public.yml.
if [[ "$UNMATCHED" -gt 0 ]]; then
  echo >&2
  echo "ERROR: ${UNMATCHED} .publish-include entries matched no tracked files (see above)." >&2
  echo "       Refusing to publish a partial mirror. Fix .publish-include and re-run." >&2
  exit 1
fi

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
git add -A
# Then force-add exactly the staged list — nothing else. That list came from
# `git ls-files` in the private repo, so this cannot pull in an untracked file;
# it only stops the published .gitignore from suppressing tracked placeholders
# under ignored runtime dirs (static/.gitkeep, media/.gitkeep,
# vouch/config.template). A blanket `git add -A -f` would also have force-added
# anything else that happened to be on disk.
if [[ -s "$STAGED_LIST" ]]; then
  xargs -0 git add -f -- < "$STAGED_LIST"
fi
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

#!/usr/bin/env bash
# Decide whether a push to `main` should trigger a deploy.
#
# Usage:
#   deploy_change_detection.sh <repo> <current-sha> <before-sha> <last-deployed-file>
#
# Prints "deploy=yes" or "deploy=no".
#
# The baseline is the last SHA that was **successfully deployed**, not the
# previous head of `main`. That distinction is the whole point: if a deploy
# failed, its changes were never applied, and a later push that only touches
# documentation must still deploy, because the failed deploy's changes are
# still undeployed. Diffing against the previous head would skip it.
#
# The rule is therefore: if the last successful deploy is unknown, or the
# range between it and this push contains anything that can change what runs,
# deploy. Otherwise, skip.
#
# `before` is the previous head of `main` for this push. It is used to spot a
# first push (no previous head) and a rewritten history (the previous head is
# no longer an ancestor of the push). The diff itself is taken over the whole
# range between the baseline and the push, in one go, so a multi-commit push
# is never half-deployed.
set -euo pipefail

repo="${1:?repository directory}"
current="${2:?current sha}"
before="${3:-}"
last_deployed_file="${4:?file holding the last deployed sha}"

cd "$repo"

answer() {
  echo "deploy=$1"
  exit 0
}

# 1. No record of a successful deploy: we do not know what is running, so we
#    cannot know what is missing. Deploy.
#
#    A file that cannot be read counts as no record: the point of the record
#    is to prove what is deployed, and an unreadable record proves nothing.
if [ ! -r "$last_deployed_file" ] || [ ! -s "$last_deployed_file" ]; then
  answer yes
fi
baseline="$(tr -d '[:space:]' < "$last_deployed_file" 2>/dev/null || true)"
if [ -z "$baseline" ]; then
  answer yes
fi

# 2. The recorded SHA is not in this checkout: the diff cannot be computed, so
#    deploy rather than guess.
if ! git cat-file -e "${baseline}^{commit}" 2>/dev/null; then
  answer yes
fi

# 3. A first push, or a history that was rewritten under us: deploy.
if [ -z "$before" ] || [ "$before" = "0000000000000000000000000000000000000000" ]; then
  answer yes
fi
if ! git merge-base --is-ancestor "$before" "$current" 2>/dev/null; then
  answer yes
fi
if ! git merge-base --is-ancestor "$baseline" "$current" 2>/dev/null; then
  answer yes
fi

# 4. Nothing new since the last successful deploy.
if [ "$baseline" = "$current" ]; then
  answer no
fi

# 5. The whole range since the last successful deploy, in one diff.
changed="$(git diff --name-only "$baseline" "$current")"
for file in $changed; do
  case "$file" in
    app/*|alembic/*|mcp_server/*|scripts/*|Dockerfile|docker-compose.yml|docker-compose.prod.yml|pyproject.toml|uv.lock|.github/workflows/ci.yml)
      answer yes
      ;;
  esac
done

answer no

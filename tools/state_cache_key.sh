#!/usr/bin/env bash
# Write the Actions cache key for ~/.checkowners.
# CHECKOWNERS_CONFIG selects the config file. CHECKOWNERS_BASE_SHA is the
# pull request base commit when the event has one.
set -euo pipefail

: "${GITHUB_OUTPUT:?GITHUB_OUTPUT is required}"
: "${RUNNER_OS:?RUNNER_OS is required}"
CHECKOWNERS_BASE_SHA="${CHECKOWNERS_BASE_SHA:-}"

if [ -n "${CHECKOWNERS_BASE_SHA}" ] && git cat-file -e "${CHECKOWNERS_BASE_SHA}^{commit}" 2>/dev/null; then
  merge_base="$(git merge-base HEAD "${CHECKOWNERS_BASE_SHA}")"
else
  merge_base="$(git rev-parse HEAD)"
fi

{
  IFS= read -r repo_identity
  IFS= read -r config_hash
  IFS= read -r model_versions
} < <(python - <<'PY'
from pathlib import Path

from checkowners.config import load_config
from checkowners.models import models_payload
from checkowners.state import config_hash, repository_identity

config = load_config()
models = models_payload()
print(repository_identity(Path.cwd()))
print(config_hash(config))
print("_".join(models[name] for name in ("ownership", "risk", "topology")))
PY
)

if [ -z "${repo_identity}" ] || [ -z "${config_hash}" ] || [ -z "${model_versions}" ]; then
  echo "::error::could not compute the state cache key"
  exit 1
fi

sanitize() { printf '%s' "$1" | tr -c 'A-Za-z0-9_-' '-'; }
prefix="checkowners-$(sanitize "${RUNNER_OS}")-$(sanitize "${repo_identity}")"
{
  echo "key=${prefix}-${merge_base}-${config_hash}-${model_versions}"
  echo "restore-keys<<EOF"
  echo "${prefix}-${merge_base}-${config_hash}-"
  echo "${prefix}-"
  echo "EOF"
} >> "${GITHUB_OUTPUT}"

#!/usr/bin/env bash
# Run the github-action command with the state directory the inputs select.
set -euo pipefail

if [ -n "${CHECKOWNERS_FAIL_ON_DRIFT_INPUT:-}" ]; then
  export CHECKOWNERS_FAIL_ON_DRIFT="${CHECKOWNERS_FAIL_ON_DRIFT_INPUT}"
fi
if [ "${CHECKOWNERS_USE_CACHE:-false}" = "true" ]; then
  export CHECKOWNERS_STATE_DIR="${HOME}/.checkowners"
else
  export CHECKOWNERS_STATE_DIR="${CHECKOWNERS_EPHEMERAL_STATE}"
fi
exec checkowners github-action

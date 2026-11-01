#!/usr/bin/env bash
# Add the ownership-critical label to the pull request under review.
set -euo pipefail

: "${GH_TOKEN:?GH_TOKEN is required}"
: "${PR_NUMBER:?PR_NUMBER is required}"

gh pr edit "${PR_NUMBER}" --add-label ownership-critical

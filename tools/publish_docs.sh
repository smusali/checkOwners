#!/usr/bin/env bash
# Deploy the documentation site. EVENT_NAME selects a release or a dev build.
set -euo pipefail

: "${GH_TOKEN:?GH_TOKEN is required}"
: "${EVENT_NAME:?EVENT_NAME is required}"

git config user.name "github-actions[bot]"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
git config --global url."https://x-access-token:${GH_TOKEN}@github.com/".insteadOf "https://github.com/"
git fetch origin gh-pages || true

if [ "${EVENT_NAME}" = "release" ]; then
  mike deploy "${GITHUB_REF_NAME#v}" --push --update-aliases latest
  mike set-default latest --push
  exit 0
fi

mike deploy dev --push
if ! mike list latest; then
  mike alias dev latest --push
  mike set-default latest --push
fi

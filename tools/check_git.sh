#!/usr/bin/env bash
# Fail when Git is older than 2.23.0 or the checkout is shallow.
set -uo pipefail

raw=$(git version 2>/dev/null | sed -n 's/.* \([0-9][0-9]*\.[0-9][0-9]*\).*/\1/p' | head -n 1)
major=${raw%%.*}
rest=${raw#*.}
minor=${rest%%.*}
if ! printf '%s' "${major}" | grep -Eq '^[0-9]+$' || ! printf '%s' "${minor}" | grep -Eq '^[0-9]+$'; then
  major=0
  minor=0
fi
if [ "${major}" -lt 2 ] || { [ "${major}" -eq 2 ] && [ "${minor}" -lt 23 ]; }; then
  echo "::error title=checkOwners needs Git 2.23.0::checkOwners requires Git 2.23.0 or newer; found ${raw:-none}. Install a newer Git and rerun."
  exit 1
fi

shallow=$(git rev-parse --is-shallow-repository 2>/dev/null | tr -d '[:space:]')
if [ "${shallow}" != "true" ] && [ "${shallow}" != "false" ]; then
  marker=$(git rev-parse --git-path shallow 2>/dev/null | tr -d '[:space:]')
  shallow=false
  if [ -n "${marker}" ] && [ -f "${marker}" ]; then
    shallow=true
  fi
fi
if [ "${shallow}" = "true" ]; then
  echo "::error title=checkOwners needs full history::Shallow clone detected. Add fetch-depth: 0 to actions/checkout so git log and git blame see history."
  exit 1
fi

#!/usr/bin/env bash
# Fail when a previous organization's name or address is still in the tree.
set -euo pipefail

status=0
for needle in "$(printf '%s%s' 'forty' 'onetech')" "$(printf '%s%s' 'fortyone.' 'technologies')"; do
  if git grep -i -n -- "$needle"; then
    echo "::error::A previous organization identifier must not appear in the tree"
    status=1
  fi
done
exit "${status}"

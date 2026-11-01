#!/usr/bin/env bash
# Build the wheel, install it the way the Action does, and run the CLI.
set -euo pipefail

uv build
uv run python tools/check_wheel.py
rm -rf .smoke
uv venv --quiet .smoke
export VIRTUAL_ENV=.smoke
uv pip install --quiet --require-hashes -r requirements.lock
uv pip install --quiet --no-deps dist/checkowners-*-py3-none-any.whl
unset VIRTUAL_ENV

smoke=.smoke/bin/python
"${smoke}" -c "import networkx"
"${smoke}" -m checkowners --version
"${smoke}" -m checkowners analyze --json > analyze.json
"${smoke}" -m checkowners drift --json > drift.json
"${smoke}" -m checkowners qualified-owners --all --json > qualified-owners.json
"${smoke}" -m checkowners topology --json > topology.json
"${smoke}" -m checkowners risk --json > risk.json
uv run python tools/check_json_contract.py \
  analyze=analyze.json \
  drift=drift.json \
  qualified-owners=qualified-owners.json \
  topology=topology.json \
  risk=risk.json

#!/usr/bin/env bash
# Install the pinned checkowners wheel, or a local extras spec for this repo.
# Inputs arrive as environment variables from action.yml. Do not interpolate
# workflow data into this file: install_spec is remote code execution.
set -euo pipefail

: "${GITHUB_ACTION_PATH:?GITHUB_ACTION_PATH is required}"
: "${CHECKOWNERS_PINNED_VERSION:?CHECKOWNERS_PINNED_VERSION is required}"
CHECKOWNERS_VERSION="${CHECKOWNERS_VERSION:-}"
CHECKOWNERS_INSTALL_SPEC="${CHECKOWNERS_INSTALL_SPEC:-}"
CHECKOWNERS_INDEX_URL="${CHECKOWNERS_INDEX_URL:-}"
CHECKOWNERS_OFFLINE="${CHECKOWNERS_OFFLINE:-false}"

ACTION_ROOT="${GITHUB_ACTION_PATH}"
LOCK="${ACTION_ROOT}/requirements.lock"
VERSION="${CHECKOWNERS_VERSION:-$CHECKOWNERS_PINNED_VERSION}"
INDEX_ARGS=()
if [ -n "${CHECKOWNERS_INDEX_URL}" ]; then
  INDEX_ARGS+=(--index-url "${CHECKOWNERS_INDEX_URL}")
fi

install_lockfile() {
  python -m pip install --no-deps --require-hashes --only-binary=:all: \
    "$@" -r "${LOCK}"
}

if [ -n "${CHECKOWNERS_INSTALL_SPEC}" ]; then
  if [ "${CHECKOWNERS_INSTALL_SPEC}" != "." ] \
    && [ "${CHECKOWNERS_INSTALL_SPEC}" != ".[graph]" ] \
    && [ "${CHECKOWNERS_INSTALL_SPEC}" != ".[github]" ] \
    && [ "${CHECKOWNERS_INSTALL_SPEC}" != ".[graph,github]" ] \
    && [ "${CHECKOWNERS_INSTALL_SPEC}" != ".[github,graph]" ] \
    && [ "${CHECKOWNERS_INSTALL_SPEC}" != ".[all]" ]; then
    echo "::error::install_spec must be a local extras spec: . .[graph] .[github] .[graph,github] .[github,graph] .[all]"
    exit 1
  fi
  install_lockfile "${INDEX_ARGS[@]+"${INDEX_ARGS[@]}"}"
  python -m pip install --no-deps "${CHECKOWNERS_INSTALL_SPEC}"
elif [ "${CHECKOWNERS_OFFLINE}" = "true" ]; then
  if [ -n "${CHECKOWNERS_VERSION}" ] && [ "${CHECKOWNERS_VERSION}" != "${CHECKOWNERS_PINNED_VERSION}" ]; then
    echo "::error::offline install requires checkowners_version to match the Action pin (${CHECKOWNERS_PINNED_VERSION})"
    exit 1
  fi
  WHEEL="${ACTION_ROOT}/checkowners-${CHECKOWNERS_PINNED_VERSION}-py3-none-any.whl"
  if [ ! -f "${WHEEL}" ]; then
    echo "::error::offline install requires ${WHEEL}"
    exit 1
  fi
  install_lockfile --no-index --find-links "${ACTION_ROOT}"
  python -m pip install --no-deps --no-index --find-links "${ACTION_ROOT}" \
    "checkowners==${CHECKOWNERS_PINNED_VERSION}"
elif [ -n "${CHECKOWNERS_VERSION}" ] && [ "${CHECKOWNERS_VERSION}" != "${CHECKOWNERS_PINNED_VERSION}" ]; then
  install_lockfile "${INDEX_ARGS[@]+"${INDEX_ARGS[@]}"}"
  python -m pip install --no-deps --only-binary=:all: \
    "${INDEX_ARGS[@]+"${INDEX_ARGS[@]}"}" "checkowners==${VERSION}"
else
  WHEEL="${ACTION_ROOT}/checkowners-${CHECKOWNERS_PINNED_VERSION}-py3-none-any.whl"
  if [ ! -f "${WHEEL}" ]; then
    echo "::error::missing pinned wheel ${WHEEL}"
    exit 1
  fi
  install_lockfile "${INDEX_ARGS[@]+"${INDEX_ARGS[@]}"}"
  python -m pip install --no-deps --no-index --find-links "${ACTION_ROOT}" \
    "checkowners==${CHECKOWNERS_PINNED_VERSION}"
fi

got="$(checkowners --version)"
if [ -n "${CHECKOWNERS_INSTALL_SPEC}" ]; then
  if [[ ! "${got}" =~ ^checkowners\ [0-9]+\.[0-9]+\.[0-9]+ ]]; then
    echo "::error::expected 'checkowners <semver>', got: ${got}"
    exit 1
  fi
elif [ "${got}" != "checkowners ${VERSION}" ]; then
  echo "::error::installed '${got}', expected 'checkowners ${VERSION}'"
  exit 1
fi

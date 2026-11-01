#!/usr/bin/env bash
# Write the CycloneDX SBOM for the runtime lockfile into dist/.
set -euo pipefail

: "${RELEASE_TAG:?RELEASE_TAG is required}"

sbom="dist/checkowners-${RELEASE_TAG#v}.cdx.json"
cyclonedx-py requirements -o "${sbom}" requirements.lock
test -s "${sbom}"

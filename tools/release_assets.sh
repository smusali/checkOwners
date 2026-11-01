#!/usr/bin/env bash
# Attach the SBOM and the build provenance to the published release.
set -euo pipefail

: "${GH_TOKEN:?GH_TOKEN is required}"
: "${RELEASE_TAG:?RELEASE_TAG is required}"
: "${BUNDLE:?BUNDLE is required}"

version="${RELEASE_TAG#v}"
sbom="dist/checkowners-${version}.cdx.json"
provenance="checkowners-${version}.intoto.jsonl"
test -s "${sbom}"
cp "${BUNDLE}" "${provenance}"
test -s "${provenance}"
gh release upload "${RELEASE_TAG}" "${sbom}" "${provenance}" --clobber

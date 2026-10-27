#!/usr/bin/env bash
# Promote the currently running version to stable after a healthy verification window.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STATE_DIR="${ROOT_DIR}/state"

if [[ ! -f "${STATE_DIR}/current_image" ]]; then
  echo "[promote] ERROR: no current_image in ${STATE_DIR}" >&2
  exit 1
fi

VERSION="$(cat "${STATE_DIR}/current_version")"
IMAGE="$(cat "${STATE_DIR}/current_image")"
FAULT="$(cat "${STATE_DIR}/current_fault_mode" 2>/dev/null || echo none)"

echo "${VERSION}" > "${STATE_DIR}/stable_version"
echo "${IMAGE}" > "${STATE_DIR}/stable_image"
echo "${FAULT}" > "${STATE_DIR}/stable_fault_mode"

TS="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
echo "${TS} PROMOTE version=${VERSION} image=${IMAGE}" >> "${STATE_DIR}/deploy_history.log"
echo "[promote] ${VERSION} is now stable"

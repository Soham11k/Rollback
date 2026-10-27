#!/usr/bin/env bash
# Roll back the app container to the last known-stable image.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ -n "${STATE_DIR:-}" ]]; then
  :
elif [[ -d /state && -w /state ]]; then
  STATE_DIR="/state"
else
  STATE_DIR="${ROOT_DIR}/state"
fi
DEPLOY_SCRIPT="${ROOT_DIR}/scripts/deploy.sh"

if [[ ! -f "${STATE_DIR}/stable_image" ]]; then
  echo "[rollback] ERROR: no stable_image recorded in ${STATE_DIR}" >&2
  exit 1
fi

STABLE_IMAGE="$(cat "${STATE_DIR}/stable_image")"
STABLE_VERSION="$(cat "${STATE_DIR}/stable_version")"
STABLE_FAULT="$(cat "${STATE_DIR}/stable_fault_mode" 2>/dev/null || echo none)"
CURRENT="$(cat "${STATE_DIR}/current_version" 2>/dev/null || echo unknown)"

echo "[rollback] restoring ${STABLE_VERSION} (from ${CURRENT}) image=${STABLE_IMAGE}"

TS="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
hist_tmp="$(mktemp /tmp/rollbackai-hist.XXXXXX)"
if [[ -f "${STATE_DIR}/deploy_history.log" ]]; then
  cat "${STATE_DIR}/deploy_history.log" > "${hist_tmp}" 2>/dev/null || true
fi
printf '%s\n' "${TS} ROLLBACK from=${CURRENT} to=${STABLE_VERSION} image=${STABLE_IMAGE}" >> "${hist_tmp}"
cp -f "${hist_tmp}" "${STATE_DIR}/deploy_history.log" 2>/dev/null \
  || cat "${hist_tmp}" > "${STATE_DIR}/deploy_history.log"
rm -f "${hist_tmp}"

STATE_DIR="${STATE_DIR}" "${DEPLOY_SCRIPT}" "${STABLE_IMAGE}" "${STABLE_VERSION}" "${STABLE_FAULT}"

echo "[rollback] restored ${STABLE_VERSION}"

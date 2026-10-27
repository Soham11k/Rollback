#!/usr/bin/env bash
# Build a local app image with optional fault injection (no GHCR required).
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VERSION="${1:-1.0.0}"
FAULT_MODE="${2:-none}"
TAG="${3:-rollbackai-app:local-${VERSION}}"

export COPYFILE_DISABLE=1

# Docker Desktop cannot read macOS AppleDouble (._*) xattrs on USB volumes.
# Stage a clean build context on the local disk.
STAGE="$(mktemp -d /tmp/rollbackai-app-build.XXXXXX)"
cleanup() { rm -rf "${STAGE}"; }
trap cleanup EXIT

mkdir -p "${STAGE}"
# ditto without AppleDouble resource forks
ditto --norsrc --noextattr --noqtn "${ROOT_DIR}/app/app.py" "${STAGE}/app.py"
ditto --norsrc --noextattr --noqtn "${ROOT_DIR}/app/catalog.py" "${STAGE}/catalog.py"
ditto --norsrc --noextattr --noqtn "${ROOT_DIR}/app/seed.py" "${STAGE}/seed.py"
ditto --norsrc --noextattr --noqtn "${ROOT_DIR}/app/requirements.txt" "${STAGE}/requirements.txt"
ditto --norsrc --noextattr --noqtn "${ROOT_DIR}/app/Dockerfile" "${STAGE}/Dockerfile"
ditto --norsrc --noextattr --noqtn "${ROOT_DIR}/app/templates" "${STAGE}/templates"
ditto --norsrc --noextattr --noqtn "${ROOT_DIR}/app/static" "${STAGE}/static"
ditto --norsrc --noextattr --noqtn "${ROOT_DIR}/app/data" "${STAGE}/data"
printf 'tests/\n__pycache__/\n*.pyc\n.pytest_cache/\n._*\n**/._*\n' > "${STAGE}/.dockerignore"
find "${STAGE}" -name '._*' -delete 2>/dev/null || true

echo "[build-local] version=${VERSION} fault_mode=${FAULT_MODE} tag=${TAG}"
docker build -t "${TAG}" "${STAGE}"
echo "${TAG}"

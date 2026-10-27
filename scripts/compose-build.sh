#!/usr/bin/env bash
# Build compose images from a clean local-disk context (USB-safe).
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export COPYFILE_DISABLE=1

STAGE="$(mktemp -d /tmp/rollbackai-compose-build.XXXXXX)"
cleanup() { rm -rf "${STAGE}"; }
trap cleanup EXIT

mkdir -p "${STAGE}/loadgen" "${STAGE}/detector" "${STAGE}/rewind" \
  "${STAGE}/grafana/provisioning/datasources" \
  "${STAGE}/grafana/provisioning/dashboards/json"

for f in loadgen.py requirements.txt Dockerfile; do
  ditto --norsrc --noextattr --noqtn "${ROOT_DIR}/loadgen/${f}" "${STAGE}/loadgen/${f}"
done
for f in collect.py train.py monitor.py evaluate.py snapshot.py requirements.txt Dockerfile; do
  if [[ -f "${ROOT_DIR}/detector/${f}" ]]; then
    ditto --norsrc --noextattr --noqtn "${ROOT_DIR}/detector/${f}" "${STAGE}/detector/${f}"
  fi
done

# Rewind service
for f in "${ROOT_DIR}/rewind/"*.py "${ROOT_DIR}/rewind/requirements.txt" "${ROOT_DIR}/rewind/Dockerfile"; do
  ditto --norsrc --noextattr --noqtn "${f}" "${STAGE}/rewind/$(basename "${f}")"
done
ditto --norsrc --noextattr --noqtn "${ROOT_DIR}/rewind/templates" "${STAGE}/rewind/templates"
ditto --norsrc --noextattr --noqtn "${ROOT_DIR}/rewind/static" "${STAGE}/rewind/static"

# Grafana with baked provisioning (no AppleDouble)
ditto --norsrc --noextattr --noqtn \
  "${ROOT_DIR}/monitoring/grafana/image/Dockerfile" "${STAGE}/grafana/Dockerfile"
ditto --norsrc --noextattr --noqtn \
  "${ROOT_DIR}/monitoring/grafana/image/provisioning" "${STAGE}/grafana/provisioning"

printf '._*\n**/._*\n__pycache__/\n' > "${STAGE}/loadgen/.dockerignore"
printf '._*\n**/._*\n__pycache__/\ndata/\nmodels/*.joblib\n' > "${STAGE}/detector/.dockerignore"
printf '._*\n**/._*\n__pycache__/\ntests/\n' > "${STAGE}/rewind/.dockerignore"
printf '._*\n**/._*\n' > "${STAGE}/grafana/.dockerignore"
find "${STAGE}" -name '._*' -delete 2>/dev/null || true

docker build -t rollbackai-loadgen:latest "${STAGE}/loadgen"
docker build -t rollbackai-detector:latest "${STAGE}/detector"
docker build -t rollbackai-rewind:latest "${STAGE}/rewind"
docker build -t rollbackai-grafana:latest "${STAGE}/grafana"

echo "[compose-build] loadgen + detector + rewind + grafana images ready"

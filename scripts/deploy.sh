#!/usr/bin/env bash
# Deploy (or swap) the RewindAI / GatePass app container to a given image tag.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# Prefer /state when mounted in the detector container (USB-safe separate mount).
if [[ -n "${STATE_DIR:-}" ]]; then
  :
elif [[ -d /state && -w /state ]]; then
  STATE_DIR="/state"
else
  STATE_DIR="${ROOT_DIR}/state"
fi
NETWORK="${NETWORK:-rollbackai}"
CONTAINER_NAME="${CONTAINER_NAME:-rollbackai-app}"
APP_PORT="${APP_PORT:-8080}"
CPUS="${APP_CPUS:-1.0}"
MEMORY="${APP_MEMORY:-256m}"
FORCE_DEPLOY="${FORCE_DEPLOY:-0}"

IMAGE_REF="${1:-}"
VERSION_LABEL="${2:-}"
FAULT_MODE="${3:-none}"

if [[ -z "${IMAGE_REF}" ]]; then
  echo "Usage: $0 <image_ref> [version_label] [fault_mode]" >&2
  echo "Set FORCE_DEPLOY=1 to override a quarantined image/version." >&2
  exit 1
fi

if [[ -z "${VERSION_LABEL}" ]]; then
  VERSION_LABEL="${IMAGE_REF##*:}"
fi

mkdir -p "${STATE_DIR}" 2>/dev/null || true
touch "${STATE_DIR}/deploy_history.log" 2>/dev/null || true

# Refuse redeploy of a quarantined bad release unless FORCE_DEPLOY=1.
if [[ "${FORCE_DEPLOY}" != "1" && -f "${STATE_DIR}/quarantine.json" ]]; then
  q_version="$(python3 -c "import json,sys; print(json.load(open(sys.argv[1])).get('version',''))" "${STATE_DIR}/quarantine.json" 2>/dev/null || true)"
  q_image="$(python3 -c "import json,sys; print(json.load(open(sys.argv[1])).get('image',''))" "${STATE_DIR}/quarantine.json" 2>/dev/null || true)"
  if [[ -n "${q_version}" && "${VERSION_LABEL}" == "${q_version}" ]]; then
    echo "[deploy] REFUSED: version ${VERSION_LABEL} is quarantined. Set FORCE_DEPLOY=1 to override." >&2
    exit 2
  fi
  if [[ -n "${q_image}" && "${IMAGE_REF}" == "${q_image}" ]]; then
    echo "[deploy] REFUSED: image ${IMAGE_REF} is quarantined. Set FORCE_DEPLOY=1 to override." >&2
    exit 2
  fi
fi

write_state() {
  # USB bind-mounts sometimes reject shell redirects; write via /tmp then copy.
  local path="$1"
  local value="$2"
  local tmp
  tmp="$(mktemp /tmp/rollbackai-state.XXXXXX)"
  printf '%s\n' "${value}" > "${tmp}"
  if ! cp -f "${tmp}" "${path}" 2>/dev/null; then
    rm -f "${path}" 2>/dev/null || true
    cat "${tmp}" > "${path}" 2>/dev/null || cp "${tmp}" "${path}"
  fi
  rm -f "${tmp}"
}

append_history() {
  local line="$1"
  local tmp
  tmp="$(mktemp /tmp/rollbackai-hist.XXXXXX)"
  if [[ -f "${STATE_DIR}/deploy_history.log" ]]; then
    cat "${STATE_DIR}/deploy_history.log" > "${tmp}" 2>/dev/null || true
  fi
  printf '%s\n' "${line}" >> "${tmp}"
  # USB bind mounts can flake; never fail the deploy over history alone.
  if ! cp -f "${tmp}" "${STATE_DIR}/deploy_history.log" 2>/dev/null; then
    cat "${tmp}" > "${STATE_DIR}/deploy_history.log" 2>/dev/null || true
  fi
  # Also try the host path when deploy runs from the detector container.
  if [[ -n "${HOST_STATE_DIR:-}" && "${HOST_STATE_DIR}" != "${STATE_DIR}" ]]; then
    cp -f "${tmp}" "${HOST_STATE_DIR}/deploy_history.log" 2>/dev/null || true
  fi
  rm -f "${tmp}"
}

PREVIOUS=""
if [[ -f "${STATE_DIR}/current_version" ]]; then
  PREVIOUS="$(cat "${STATE_DIR}/current_version")"
fi

echo "[deploy] image=${IMAGE_REF} version=${VERSION_LABEL} fault_mode=${FAULT_MODE}"

HAS_COMPOSE=0
if docker compose version >/dev/null 2>&1; then
  HAS_COMPOSE=1
fi

if [[ "${HAS_COMPOSE}" -eq 1 && -f "${ROOT_DIR}/docker-compose.yml" ]]; then
  (cd "${ROOT_DIR}" && docker compose up -d prometheus grafana loadgen rewind) || true
elif ! docker network inspect "${NETWORK}" >/dev/null 2>&1; then
  echo "[deploy] creating docker network ${NETWORK}"
  docker network create "${NETWORK}" >/dev/null
fi

if docker image inspect "${IMAGE_REF}" >/dev/null 2>&1; then
  echo "[deploy] using local image ${IMAGE_REF}"
else
  echo "[deploy] pulling ${IMAGE_REF}"
  docker pull "${IMAGE_REF}"
fi

if docker ps -a --format '{{.Names}}' | grep -qx "${CONTAINER_NAME}"; then
  echo "[deploy] removing existing container ${CONTAINER_NAME}"
  docker rm -f "${CONTAINER_NAME}" >/dev/null
fi

# When deploy.sh runs inside the detector (docker.sock), STATE_DIR is often
# /state — a path that only exists in that container. Docker Desktop needs the
# host path for -v; prefer HOST_STATE_DIR when set by compose.
MOUNT_STATE_DIR="${HOST_STATE_DIR:-${STATE_DIR}}"

echo "[deploy] starting ${CONTAINER_NAME}"
docker run -d \
  --name "${CONTAINER_NAME}" \
  --network "${NETWORK}" \
  --network-alias app \
  -p "${APP_PORT}:8080" \
  --cpus "${CPUS}" \
  --memory "${MEMORY}" \
  -e "APP_VERSION=${VERSION_LABEL}" \
  -e "FAULT_MODE=${FAULT_MODE}" \
  -e "STATE_DIR=/state" \
  -e "PROMETHEUS_URL=${APP_PROMETHEUS_URL:-http://prometheus:9090}" \
  -e "DETECTOR_URL=${APP_DETECTOR_URL:-http://detector:9091}" \
  -v "${MOUNT_STATE_DIR}:/state:ro" \
  --restart unless-stopped \
  "${IMAGE_REF}" >/dev/null

echo "[deploy] waiting for container health"
for i in $(seq 1 30); do
  status="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "${CONTAINER_NAME}" 2>/dev/null || echo missing)"
  if [[ "${status}" == "healthy" ]]; then
    echo "[deploy] healthy"
    break
  fi
  if [[ "${status}" == "running" ]]; then
    if docker exec "${CONTAINER_NAME}" python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health')" >/dev/null 2>&1; then
      echo "[deploy] healthy"
      break
    fi
  fi
  if [[ "${i}" -eq 30 ]]; then
    echo "[deploy] ERROR: app did not become healthy (status=${status})" >&2
    docker logs "${CONTAINER_NAME}" --tail 50 || true
    exit 1
  fi
  sleep 2
done

write_state "${STATE_DIR}/current_image" "${IMAGE_REF}"
write_state "${STATE_DIR}/current_version" "${VERSION_LABEL}"
write_state "${STATE_DIR}/current_fault_mode" "${FAULT_MODE}"
if [[ -n "${PREVIOUS}" ]]; then
  write_state "${STATE_DIR}/previous_version" "${PREVIOUS}"
fi

if [[ ! -f "${STATE_DIR}/stable_version" ]]; then
  write_state "${STATE_DIR}/stable_version" "${VERSION_LABEL}"
  write_state "${STATE_DIR}/stable_image" "${IMAGE_REF}"
  write_state "${STATE_DIR}/stable_fault_mode" "none"
  echo "[deploy] marked ${VERSION_LABEL} as initial stable version"
fi

TS="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
append_history "${TS} DEPLOY version=${VERSION_LABEL} image=${IMAGE_REF} fault_mode=${FAULT_MODE}"

echo "[deploy] done: current=${VERSION_LABEL}"

#!/usr/bin/env bash
# One command to bring RewindAI up: Docker, images, stack, Ollama, demo apps.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT_DIR}"
export COPYFILE_DISABLE=1

REWIND_URL="${REWIND_URL:-http://127.0.0.1:8090}"
VOLUME="rollbackai_rewind-data"
FRESH=0
BUILD=0

usage() {
  cat <<'EOF'
Usage: ./scripts/start.sh [--fresh] [--build]

  --fresh   wipe RewindAI's registry (apps, releases, incidents) and start clean
  --build   rebuild the RewindAI, loadgen and Grafana images first
EOF
}

for arg in "$@"; do
  case "${arg}" in
    --fresh) FRESH=1 ;;
    --build) BUILD=1 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown option: ${arg}" >&2; usage; exit 1 ;;
  esac
done

say() { printf '\n=== %s ===\n' "$*"; }
info() { printf '  -> %s\n' "$*"; }

wake_docker() {
  if docker info >/dev/null 2>&1; then
    return
  fi
  info "Docker is not running, opening Docker Desktop..."
  open -a Docker 2>/dev/null || { echo "Start Docker Desktop and re-run." >&2; exit 1; }
  for _ in $(seq 1 90); do
    if docker info >/dev/null 2>&1; then
      info "Docker is up"
      return
    fi
    sleep 2
  done
  echo "Docker did not start within 3 minutes." >&2
  exit 1
}

images_missing() {
  local image
  for image in rollbackai-rewind rollbackai-loadgen rollbackai-grafana; do
    docker image inspect "${image}:latest" >/dev/null 2>&1 || return 0
  done
  return 1
}

ollama_bin() {
  if [[ -x /opt/homebrew/bin/ollama ]]; then
    echo /opt/homebrew/bin/ollama
  elif command -v ollama >/dev/null 2>&1; then
    command -v ollama
  fi
}

start_ollama() {
  if curl -fsS http://127.0.0.1:11434/api/tags >/dev/null 2>&1; then
    info "Ollama is running"
    return
  fi
  local bin
  bin="$(ollama_bin)"
  if [[ -z "${bin}" ]]; then
    info "Ollama is not installed, so Fix with AI stays off. Run ./scripts/ai-setup.sh to enable it."
    return
  fi
  if [[ -x /opt/homebrew/bin/brew ]]; then
    /opt/homebrew/bin/brew services start ollama >/dev/null 2>&1 || true
  fi
  if ! curl -fsS http://127.0.0.1:11434/api/tags >/dev/null 2>&1; then
    nohup "${bin}" serve >/tmp/ollama-serve.log 2>&1 &
  fi
  for _ in $(seq 1 20); do
    if curl -fsS http://127.0.0.1:11434/api/tags >/dev/null 2>&1; then
      info "Ollama is running"
      return
    fi
    sleep 1
  done
  info "Ollama did not answer on :11434. Check /tmp/ollama-serve.log"
}

wipe_registry() {
  info "Wiping the RewindAI registry (${VOLUME})..."
  docker compose stop rewind >/dev/null 2>&1 || true
  docker compose rm -f rewind >/dev/null 2>&1 || true
  local apps
  apps="$(docker ps -aq --filter 'name=^rewind-' || true)"
  if [[ -n "${apps}" ]]; then
    # shellcheck disable=SC2086
    docker rm -f ${apps} >/dev/null
  fi
  docker volume rm "${VOLUME}" >/dev/null 2>&1 || true
}

wait_http() {
  local url="$1" tries="${2:-60}"
  for _ in $(seq 1 "${tries}"); do
    if curl -fsS "${url}" >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  echo "Timed out waiting for ${url}" >&2
  return 1
}

registry_empty() {
  curl -fsS "${REWIND_URL}/api/apps" | python3 -c \
    "import json,sys; raise SystemExit(0 if not json.load(sys.stdin).get('apps') else 1)"
}

seed_apps() {
  info "Registering GatePass and Notes..."
  ./scripts/rewind.sh register --id gatepass --name GatePass --host-port 8080 \
    --probes "/api/events,/api/checkout?event_id=EVT-01&tier=GA,/api/checkout?event_id=EVT-02&tier=FRT" \
    --on-anomaly rollback >/dev/null
  ./scripts/rewind.sh register --id notes --name Notes --host-port 8081 \
    --probes "/notes,/notes/1" --on-anomaly rollback >/dev/null

  info "Publishing GatePass 1.0.0 and verifying it (about a minute)..."
  ./scripts/rewind.sh publish gatepass --version 1.0.0 --dir "${ROOT_DIR}/app" --wait
  info "Publishing Notes 1.0.0..."
  ./scripts/rewind.sh publish notes --version 1.0.0 --dir "${ROOT_DIR}/demo/apps/notes"
}

say "Starting RewindAI"
chmod +x scripts/*.sh
wake_docker

if [[ "${FRESH}" == "1" ]]; then
  wipe_registry
fi

if [[ "${BUILD}" == "1" ]] || images_missing; then
  info "Building images..."
  ./scripts/compose-build.sh
fi

info "Starting the stack..."
docker compose up -d --no-build --remove-orphans rewind prometheus grafana loadgen

stopped="$(docker ps -aq --filter 'name=^rewind-' --filter 'status=exited' || true)"
if [[ -n "${stopped}" ]]; then
  info "Restarting app containers stopped by stop.sh..."
  # shellcheck disable=SC2086
  docker start ${stopped} >/dev/null
fi

start_ollama

info "Waiting for RewindAI..."
wait_http "${REWIND_URL}/health" 90

if registry_empty; then
  seed_apps
fi

say "RewindAI is up"
./scripts/rewind.sh status
cat <<EOF

  RewindAI console  ${REWIND_URL}/
  GatePass buyers   http://127.0.0.1:8080/
  Grafana           http://127.0.0.1:3000/d/rewindai-overview  (admin/admin)
  Prometheus        http://127.0.0.1:9090/

  Ship a bad release:  ./scripts/rewind.sh publish gatepass --version 2.0.0 --base 1.0.0 --edits demo/gatepass/v2-bugs.json --wait
  Stop everything:     ./scripts/stop.sh
EOF

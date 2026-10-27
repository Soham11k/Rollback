#!/usr/bin/env bash
# Stop the RewindAI stack and the apps it deployed. Data stays on the rewind-data volume.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT_DIR}"

if ! docker info >/dev/null 2>&1; then
  echo "Docker is not running, nothing to stop."
  exit 0
fi

apps="$(docker ps -q --filter 'name=^rewind-' || true)"
if [[ -n "${apps}" ]]; then
  echo "  -> stopping app containers"
  # shellcheck disable=SC2086
  docker stop ${apps} >/dev/null
fi

echo "  -> stopping RewindAI, Prometheus, Grafana and loadgen"
docker compose stop >/dev/null

echo "Stopped. Your apps, releases and incidents are kept; ./scripts/start.sh brings them back."
echo "Ollama keeps running as a background service (brew services stop ollama to stop it)."

#!/usr/bin/env bash
# RewindAI multi-app demo: prepare | live | all
set -euo pipefail

# Prefer caller-provided ROOT_DIR (needed when re-exec'd from /tmp).
if [[ -z "${ROOT_DIR:-}" ]]; then
  ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
fi
# Re-exec from /tmp when launched from a USB path (AppleDouble / ditto safety).
if [[ "${ROOT_DIR}" == /Volumes/* ]] && [[ "${REWIND_SCENARIO_LOCAL:-}" != "1" ]]; then
  STAGE="$(mktemp -d /tmp/rewind-scenario.XXXXXX)"
  export COPYFILE_DISABLE=1
  ditto --norsrc --noextattr --noqtn "${ROOT_DIR}/scripts/scenario.sh" "${STAGE}/scenario.sh"
  chmod +x "${STAGE}/scenario.sh"
  REWIND_SCENARIO_LOCAL=1 ROOT_DIR="${ROOT_DIR}" exec bash "${STAGE}/scenario.sh" "$@"
fi

cd "${ROOT_DIR}"
export COPYFILE_DISABLE=1

REWIND_URL="${REWIND_URL:-http://127.0.0.1:8090}"
APP_URL="${APP_URL:-http://127.0.0.1:8080}"
GRAFANA_URL="${GRAFANA_URL:-http://127.0.0.1:3000}"
PAUSE="${PAUSE:-0}"
VERIFY_WINDOW_SECONDS="${VERIFY_WINDOW_SECONDS:-60}"

say() { printf '\n=== %s ===\n' "$*"; }
info() { printf '  -> %s\n' "$*"; }
pause_if_needed() {
  if [[ "${PAUSE}" == "1" ]]; then
    read -r -p "  [PAUSE] press Enter to continue… " _
  fi
}

require_docker() {
  docker info >/dev/null 2>&1 || { echo "Docker is not running" >&2; exit 1; }
}

wait_http() {
  local url="$1" tries="${2:-60}"
  for _ in $(seq 1 "${tries}"); do
    if curl -fsS "${url}" >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  echo "timeout waiting for ${url}" >&2
  return 1
}

wait_json_field() {
  local url="$1" field="$2" expect="$3" tries="${4:-90}"
  python3 - "${url}" "${field}" "${expect}" "${tries}" <<'PY'
import json, sys, time, urllib.request
url, field, expect, tries = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4])
for _ in range(tries):
    try:
        data = json.load(urllib.request.urlopen(url, timeout=3))
    except Exception:
        time.sleep(2)
        continue
    cur = data
    for part in field.split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        else:
            cur = None
            break
    if str(cur) == expect:
        print(json.dumps(data, indent=2)[:1500])
        raise SystemExit(0)
    time.sleep(2)
raise SystemExit(f"timeout waiting for {field}={expect} at {url}")
PY
}

cmd_prepare() {
  require_docker
  say "PREPARE — build RewindAI stack, register apps, publish healthy v1"
  chmod +x scripts/*.sh

  info "Stopping legacy single-app containers if present..."
  for name in rollbackai-app rollbackai-detector rewind-gatepass rewind-notes; do
    docker rm -f "${name}" >/dev/null 2>&1 || true
  done

  info "Building images..."
  ./scripts/compose-build.sh
  docker compose up -d rewind prometheus grafana loadgen
  wait_http "${REWIND_URL}/health" 90

  info "Ollama (optional for prepare; required for AI fix chapter)..."
  if command -v ollama >/dev/null 2>&1; then
    ./scripts/ai-setup.sh || info "ai-setup warned — Fix with AI may be offline"
  else
    info "Ollama not installed yet. Run ./scripts/ai-setup.sh before the AI chapter."
  fi

  info "Register GatePass + notes..."
  ./scripts/rewind.sh register --id gatepass --name GatePass --host-port 8080 \
    --probes "/api/events,/api/checkout?event_id=EVT-01&tier=GA,/api/checkout?event_id=EVT-02&tier=FRT" \
    --on-anomaly rollback || true
  ./scripts/rewind.sh register --id notes --name Notes --host-port 8081 \
    --probes "/notes,/notes/1" --on-anomaly rollback || true

  info "Publish GatePass 1.0.0 from app/..."
  VERIFY_WINDOW_SECONDS="${VERIFY_WINDOW_SECONDS}" \
    ./scripts/rewind.sh publish gatepass --version 1.0.0 --dir "${ROOT_DIR}/app"
  info "Waiting for GatePass 1.0.0 to verify healthy..."
  wait_json_field "${REWIND_URL}/api/apps/gatepass" "app.last_decision" "healthy" 120 || true
  wait_json_field "${REWIND_URL}/api/apps/gatepass" "app.live_version" "1.0.0" 60 || true

  info "Publish notes 1.0.0..."
  ./scripts/rewind.sh publish notes --version 1.0.0 --dir "${ROOT_DIR}/demo/apps/notes"
  wait_json_field "${REWIND_URL}/api/apps/notes" "app.live_version" "1.0.0" 90 || true

  say "PREPARE complete"
  info "RewindAI console: ${REWIND_URL}/"
  info "GatePass buyers:  ${APP_URL}/"
  info "Grafana:          ${GRAFANA_URL}/d/rewindai-overview (admin/admin)"
  info "Next: PAUSE=1 ./scripts/scenario.sh live"
}

cmd_live() {
  require_docker
  wait_http "${REWIND_URL}/health" 30

  say "Chapter 1 — GatePass v1 is live"
  curl -fsS "${APP_URL}/health"; echo
  curl -fsS "${APP_URL}/api/checkout?event_id=EVT-01&tier=GA" | python3 -m json.tool | head -20 || true
  pause_if_needed

  say "Chapter 2 — Publish buggy GatePass v2 (real code bugs)"
  ./scripts/rewind.sh publish gatepass --version 2.0.0 --base 1.0.0 \
    --edits "${ROOT_DIR}/demo/gatepass/v2-bugs.json"
  info "Watching verification / automatic rollback..."
  # Wait until v2 is quarantined or live returns to 1.0.0
  python3 - "${REWIND_URL}" <<'PY'
import json, time, urllib.request, sys
url = sys.argv[1].rstrip("/")
deadline = time.time() + 180
while time.time() < deadline:
    data = json.load(urllib.request.urlopen(url + "/api/apps/gatepass", timeout=5))
    app = data.get("app") or {}
    releases = {r["version"]: r for r in data.get("releases") or []}
    v2 = releases.get("2.0.0") or {}
    print(
        f"live={app.get('live_version')} decision={app.get('last_decision')} "
        f"v2_quarantined={v2.get('quarantined')} v2_decision={v2.get('decision')}",
        flush=True,
    )
    if v2.get("quarantined") and app.get("live_version") == "1.0.0":
        report = data.get("report") or {}
        print(json.dumps({"app": app, "report_summary": report.get("summary"), "suspects": (report.get("suspects") or [])[:3]}, indent=2))
        raise SystemExit(0)
    time.sleep(5)
raise SystemExit("timed out waiting for anomaly/rollback")
PY
  pause_if_needed

  say "Chapter 3 — Where the bugs are"
  curl -fsS "${REWIND_URL}/api/apps/gatepass/report" | python3 -m json.tool | head -80
  info "Open ${REWIND_URL}/apps/gatepass"
  pause_if_needed

  say "Chapter 4 — Fix with AI (Ollama)"
  if ! curl -fsS "${REWIND_URL}/api/ollama" | python3 -c "import sys,json; d=json.load(sys.stdin); raise SystemExit(0 if d.get('ok') else 1)"; then
    info "Ollama offline — run ./scripts/ai-setup.sh then re-run fix manually:"
    info "  ./scripts/rewind.sh fix gatepass && ./scripts/rewind.sh ship gatepass --version 3.0.0"
  else
    ./scripts/rewind.sh fix gatepass
    info "Waiting for AI proposal..."
    python3 - "${REWIND_URL}" <<'PY'
import json, time, urllib.request, sys
url = sys.argv[1].rstrip("/")
deadline = time.time() + 420
while time.time() < deadline:
    data = json.load(urllib.request.urlopen(url + "/api/apps/gatepass/fix", timeout=5))
    status = data.get("status")
    print(f"fix status={status}", flush=True)
    if status == "ready":
        print((data.get("summary") or "")[:500])
        print((data.get("diff") or "")[:1500])
        raise SystemExit(0)
    if status in {"failed", "discarded"}:
        print(json.dumps(data, indent=2)[:2000])
        raise SystemExit(1)
    time.sleep(5)
raise SystemExit("timed out waiting for AI fix")
PY
    pause_if_needed
    say "Chapter 5 — Ship fix as 3.0.0 and re-verify"
    ./scripts/rewind.sh ship gatepass --version 3.0.0
    wait_json_field "${REWIND_URL}/api/apps/gatepass" "app.live_version" "3.0.0" 120 || true
    python3 - "${REWIND_URL}" <<'PY'
import json, time, urllib.request, sys
url = sys.argv[1].rstrip("/")
deadline = time.time() + 150
while time.time() < deadline:
    data = json.load(urllib.request.urlopen(url + "/api/apps/gatepass", timeout=5))
    app = data.get("app") or {}
    print(f"live={app.get('live_version')} best={app.get('best_version')} decision={app.get('last_decision')}", flush=True)
    if app.get("last_decision") in {"healthy", "anomaly"}:
        print(json.dumps({"releases": data.get("releases")}, indent=2)[:2000])
        raise SystemExit(0)
    time.sleep(5)
PY
  fi

  say "Demo complete"
  info "Console: ${REWIND_URL}/apps/gatepass"
}

cmd_all() {
  cmd_prepare
  cmd_live
}

main() {
  local cmd="${1:-all}"
  case "${cmd}" in
    prepare) cmd_prepare ;;
    live) cmd_live ;;
    all) cmd_all ;;
    *) echo "Usage: $0 prepare|live|all" >&2; exit 1 ;;
  esac
}

main "${1:-all}"

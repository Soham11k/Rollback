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
PAUSE="${PAUSE:-0}"

say() { printf '\n=== %s ===\n' "$*"; }
info() { printf '  -> %s\n' "$*"; }
pause_if_needed() {
  if [[ "${PAUSE}" == "1" ]]; then
    read -r -p "  [PAUSE] press Enter to continue… " _
  fi
}

cmd_prepare() {
  say "PREPARE: fresh RewindAI with GatePass 1.0.0 verified healthy"
  ./scripts/start.sh --fresh --build
}

cmd_live() {
  say "Chapter 1: GatePass 1.0.0 is live"
  curl -fsS "${APP_URL}/api/checkout?event_id=EVT-02&tier=FRT" | python3 -m json.tool | head -12 || true
  pause_if_needed

  say "Chapter 2: publish GatePass 2.0.0 (surge pricing, seat audit, receipt cache)"
  ./scripts/rewind.sh publish gatepass --version 2.0.0 --base 1.0.0 \
    --edits "${ROOT_DIR}/demo/gatepass/v2-bugs.json" --wait || true
  pause_if_needed

  say "Chapter 3: where the bugs are"
  curl -fsS "${REWIND_URL}/api/apps/gatepass/report" | python3 -c "$(cat <<'PY'
import json, sys
report = json.load(sys.stdin).get("report") or {}
for bug in (report.get("diagnosis") or {}).get("bugs") or []:
    print(f"  {bug['signal']:<12} {bug['value']:,.0f}{bug['unit']} (limit {bug['limit']:,.0f}{bug['unit']}) at {bug['location']}")
print("  suspects:")
for s in (report.get("suspects") or [])[:4]:
    print(f"    {s['file']}:{s['line']}  {s['reason']}")
PY
)"
  info "Console: ${REWIND_URL}/apps/gatepass"
  pause_if_needed

  say "Chapter 4: stay on 2.0.0's code and fix it with local AI"
  if ./scripts/rewind.sh fix gatepass; then
    pause_if_needed
    say "Chapter 5: ship the fix and verify it"
    ./scripts/rewind.sh ship gatepass --wait || true
  else
    info "Fix with AI did not produce a proposal. Run ./scripts/ai-setup.sh, then: ./scripts/rewind.sh fix gatepass"
  fi

  say "Demo complete"
  ./scripts/rewind.sh status gatepass
}

main() {
  case "${1:-all}" in
    prepare) cmd_prepare ;;
    live) cmd_live ;;
    all) cmd_prepare; cmd_live ;;
    *) echo "Usage: $0 prepare|live|all" >&2; exit 1 ;;
  esac
}

main "${1:-all}"

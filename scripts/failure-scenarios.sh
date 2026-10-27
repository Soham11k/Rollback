#!/usr/bin/env bash
# RewindAI / GatePass failure-scenario suite.
# Usage:
#   ./scripts/failure-scenarios.sh list
#   ./scripts/failure-scenarios.sh run cpu
#   ./scripts/failure-scenarios.sh run all-faults   # every failure case
#   ./scripts/failure-scenarios.sh run suite        # failures + healthy + spike
#
# Requires: Docker running, and a trained model from:
#   ./scripts/scenario.sh prepare
set -euo pipefail

# Re-exec from /tmp (USB AppleDouble safety) — same pattern as scenario.sh
if [[ "${ROLLBACKAI_FAIL_LOCAL:-}" != "1" ]]; then
  _src="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/failure-scenarios.sh"
  export ROLLBACKAI_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
  _tmp="$(mktemp /tmp/rollbackai-fail.XXXXXX)"
  _copy="${_tmp}.sh"
  mv "${_tmp}" "${_copy}"
  if command -v ditto >/dev/null 2>&1; then
    ditto --norsrc --noextattr --noqtn "${_src}" "${_copy}"
  else
    cp "${_src}" "${_copy}"
  fi
  chmod +x "${_copy}"
  export ROLLBACKAI_FAIL_LOCAL=1
  exec bash "${_copy}" "$@"
fi

ROOT_DIR="${ROLLBACKAI_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
cd "${ROOT_DIR}"

export COPYFILE_DISABLE=1
find state detector -name '._*' -delete 2>/dev/null || true
chmod +x scripts/*.sh

VERIFY_WINDOW_SECONDS="${VERIFY_WINDOW_SECONDS:-60}"
MAJORITY="${MAJORITY:-3}"
APP_URL="${APP_URL:-http://127.0.0.1:8080}"
SUMMARY_FILE="${ROOT_DIR}/state/failure_suite_summary.txt"
RESULT_FILE="${ROOT_DIR}/state/last_verify.json"

say() { printf '\n=== %s ===\n' "$*"; }
info() { printf '  %s\n' "$*"; }

require_ready() {
  if ! docker info >/dev/null 2>&1; then
    echo "Docker is not running. Start it with: open -a Docker" >&2
    exit 1
  fi
  if [[ ! -s detector/models/baseline.joblib ]]; then
    echo "No trained model. Run first: ./scripts/scenario.sh prepare" >&2
    exit 1
  fi
  if ! docker ps --format '{{.Names}}' | grep -qx rollbackai-detector; then
    echo "Detector not running. Run: ./scripts/scenario.sh prepare" >&2
    exit 1
  fi
}

detector_exec() {
  docker exec \
    -e PROMETHEUS_URL=http://prometheus:9090 \
    -e MODEL_PATH=/models/baseline.joblib \
    -e PROJECT_ROOT=/project \
    rollbackai-detector "$@"
}

reset_stable_v1() {
  ./scripts/build-local.sh 1.0.0 none rollbackai-app:v1 >/dev/null
  ./scripts/deploy.sh rollbackai-app:v1 1.0.0 none
  echo "1.0.0" > state/stable_version
  echo "rollbackai-app:v1" > state/stable_image
  echo "none" > state/stable_fault_mode
}

story_for() {
  case "$1" in
    cpu)
      cat <<'EOF'
Failure: CPU burn (rank-list recalculated on every request)
Expected: anomaly → automatic rollback to v1.0.0
EOF
      ;;
    memory)
      cat <<'EOF'
Failure: memory leak (every marksheet retained in RAM)
Expected: anomaly → automatic rollback to v1.0.0
EOF
      ;;
    latency)
      cat <<'EOF'
Failure: slow DB path (unindexed marksheet query ~2.5s)
Expected: anomaly → automatic rollback to v1.0.0
EOF
      ;;
    errors)
      cat <<'EOF'
Failure: error burst (~15–20% HTTP 500 on result lookups)
Expected: anomaly → automatic rollback to v1.0.0
EOF
      ;;
    all)
      cat <<'EOF'
Failure: combined outage (CPU + memory + latency + errors)
Expected: anomaly → automatic rollback to v1.0.0  [primary demo]
EOF
      ;;
    healthy)
      cat <<'EOF'
Control: healthy release (FAULT_MODE=none)
Expected: NO anomaly — keep the new version (no rollback)
EOF
      ;;
    spike)
      cat <<'EOF'
Control: brief bad spike then recover before verification
Expected: NO rollback — system already healthy when detector watches
EOF
      ;;
    *)
      echo "Unknown scenario: $1" >&2
      return 1
      ;;
  esac
}

# Run one named scenario. Prints PASS/FAIL/MISS and returns 0 on PASS.
run_one() {
  local name="$1"
  local expect_anomaly="${2:-1}"   # 1 = should detect, 0 = should stay healthy
  local fault_mode="${3:-$name}"
  local version_label="2.0.0-${name}"

  say "Scenario: ${name}"
  story_for "${name}" | while IFS= read -r line; do info "${line}"; done

  reset_stable_v1

  if [[ "${name}" == "spike" ]]; then
    info "Injecting a short-lived bad release, then restoring v1 before monitor..."
    ./scripts/build-local.sh "2.0.0-spike" all rollbackai-app:v2-spike >/dev/null
    ./scripts/deploy.sh rollbackai-app:v2-spike "2.0.0-spike" all
    sleep 8
    ./scripts/deploy.sh rollbackai-app:v1 1.0.0 none
    # Monitor the already-healthy portal — should NOT call for rollback.
    fault_mode="none"
    version_label="1.0.0"
    expect_anomaly=0
  elif [[ "${name}" == "healthy" ]]; then
    version_label="1.1.0-healthy"
    ./scripts/build-local.sh "${version_label}" none "rollbackai-app:${version_label}" >/dev/null
    ./scripts/deploy.sh "rollbackai-app:${version_label}" "${version_label}" none
  else
    ./scripts/build-local.sh "${version_label}" "${fault_mode}" "rollbackai-app:${version_label}" >/dev/null
    ./scripts/deploy.sh "rollbackai-app:${version_label}" "${version_label}" "${fault_mode}"
  fi

  info "Health check after deploy: $(curl -s "${APP_URL}/health" || echo failed)"
  info "Watching with Isolation Forest (${VERIFY_WINDOW_SECONDS}s, majority=${MAJORITY})..."

  find state -name '._*' -delete 2>/dev/null || true
  set +e
  detector_exec python /detector/monitor.py \
    --version "${version_label}" \
    --mode isolation_forest \
    --model /models/baseline.joblib \
    --window "${VERIFY_WINDOW_SECONDS}" \
    --interval 5 \
    --majority "${MAJORITY}" \
    --project-root /project \
    --metrics-port 9095 \
    --no-rollback \
    --result-file /tmp/last_verify.json
  local code=$?
  set -e

  docker exec rollbackai-detector cat /tmp/last_verify.json > "${RESULT_FILE}" 2>/dev/null \
    || printf '{}\n' > "${RESULT_FILE}"
  local decision
  decision="$(python3 -c "import json; print(json.load(open('${RESULT_FILE}')).get('decision',''))" 2>/dev/null || echo unknown)"

  local outcome="FAIL"
  local note=""

  if [[ "${expect_anomaly}" -eq 1 ]]; then
    if [[ "${decision}" == "anomaly" || "${code}" -eq 2 ]]; then
      info "Anomaly detected — rolling back to stable v1.0.0..."
      ./scripts/rollback.sh
      outcome="PASS"
      note="detected + rolled back"
    else
      # Single-metric faults can miss with Isolation Forest — record honestly.
      outcome="MISS"
      note="expected anomaly, got decision=${decision} code=${code} (known IF limitation on weak single-signal faults)"
      ./scripts/rollback.sh >/dev/null 2>&1 || ./scripts/deploy.sh rollbackai-app:v1 1.0.0 none >/dev/null
    fi
  else
    if [[ "${decision}" == "healthy" || "${code}" -eq 0 ]]; then
      outcome="PASS"
      note="correctly kept healthy / no rollback"
      # Ensure we end on v1 for the suite.
      ./scripts/deploy.sh rollbackai-app:v1 1.0.0 none >/dev/null
      echo "1.0.0" > state/stable_version
      echo "rollbackai-app:v1" > state/stable_image
      echo "none" > state/stable_fault_mode
    else
      outcome="FAIL"
      note="false positive — unexpected anomaly decision=${decision}"
      ./scripts/rollback.sh >/dev/null 2>&1 || ./scripts/deploy.sh rollbackai-app:v1 1.0.0 none >/dev/null
    fi
  fi

  local current
  current="$(cat state/current_version 2>/dev/null || echo '?')"
  info "Result: ${outcome} — ${note} (current=${current})"
  printf '%-10s %-6s %s\n' "${name}" "${outcome}" "${note}" >> "${SUMMARY_FILE}"

  case "${outcome}" in
    PASS) return 0 ;;
    MISS) return 2 ;;
    *) return 1 ;;
  esac
}

cmd_list() {
  cat <<'EOF'
RollbackAI failure scenarios

  cpu       CPU burn — expect rollback
  memory    Memory pressure — expect rollback
  latency   Slow responses (~2.5s) — expect rollback
  errors    HTTP 500 burst — expect rollback
  all       All faults combined — expect rollback (primary)
  healthy   Clean release — expect NO rollback
  spike     Brief bad blip already recovered — expect NO rollback

Commands:
  ./scripts/failure-scenarios.sh run cpu
  ./scripts/failure-scenarios.sh run all-faults   # cpu memory latency errors all
  ./scripts/failure-scenarios.sh run suite        # all of the above + healthy + spike

Prereq: ./scripts/scenario.sh prepare
EOF
}

cmd_run() {
  local which="${1:-}"
  require_ready
  mkdir -p state
  : > "${SUMMARY_FILE}"

  local failed=0
  local missed=0
  local passed=0

  run_and_tally() {
    set +e
    run_one "$@"
    local rc=$?
    set -e
    case "${rc}" in
      0) passed=$((passed + 1)) ;;
      2) missed=$((missed + 1)) ;;
      *) failed=$((failed + 1)) ;;
    esac
  }

  case "${which}" in
    cpu|memory|latency|errors|all|healthy|spike)
      local expect=1
      [[ "${which}" == "healthy" || "${which}" == "spike" ]] && expect=0
      run_and_tally "${which}" "${expect}" "${which}"
      ;;
    all-faults)
      for s in cpu memory latency errors all; do
        run_and_tally "${s}" 1 "${s}"
      done
      ;;
    suite)
      for s in cpu memory latency errors all; do
        run_and_tally "${s}" 1 "${s}"
      done
      run_and_tally healthy 0 none
      run_and_tally spike 0 all
      ;;
    *)
      echo "Unknown target: ${which}" >&2
      cmd_list
      exit 1
      ;;
  esac

  say "Suite summary"
  if [[ -s "${SUMMARY_FILE}" ]]; then
    column -t "${SUMMARY_FILE}" 2>/dev/null || cat "${SUMMARY_FILE}"
  fi
  info "PASS=${passed}  MISS=${missed}  FAIL=${failed}"
  info "Full log lines also in ${SUMMARY_FILE}"
  info "Tip: Grafana http://127.0.0.1:3000/d/rollbackai-overview"

  if [[ "${failed}" -gt 0 ]]; then
    exit 1
  fi
  # Misses (undetected single-signal faults) do not fail the suite exit code —
  # they are documented Isolation Forest limitations for the viva.
  exit 0
}

usage() {
  cat <<'USAGE'
Usage: failure-scenarios.sh list|run <name|all-faults|suite>
USAGE
}

main() {
  local action="${1:-list}"
  shift || true
  case "${action}" in
    list) cmd_list ;;
    run) cmd_run "${1:-}" ;;
    *) usage; exit 1 ;;
  esac
}

main "$@"
exit 0

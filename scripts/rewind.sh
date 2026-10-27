#!/usr/bin/env bash
# CLI for the RewindAI service.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REWIND_URL="${REWIND_URL:-http://127.0.0.1:8090}"
REWIND_URL="${REWIND_URL%/}"

usage() {
  cat <<'EOF'
Usage:
  rewind.sh status [ID] [--json]
  rewind.sh register --id ID [--name NAME] [--host-port N] [--probes PATHS] [--on-anomaly rollback|ask]
  rewind.sh publish ID --version V --dir PATH [--wait]
  rewind.sh publish ID --version V --image IMAGE [--wait]
  rewind.sh publish ID --version V --base BASE --edits FILE.json [--wait]
  rewind.sh watch ID VERSION
  rewind.sh pick ID VERSION [--force]
  rewind.sh fix ID [--no-wait]
  rewind.sh ship ID [--version V] [--wait]
  rewind.sh connect-github ID --repo OWNER/NAME [--branch release] [--path app]
  rewind.sh disconnect-github ID
EOF
}

PY_FMT="$(cat <<'PY'
import json
import sys

mode = sys.argv[1]
args = sys.argv[2:]
raw = sys.stdin.read()
try:
    data = json.loads(raw) if raw.strip() else {}
except ValueError:
    data = {"message": raw.strip()[:300]}


def dig(obj, dotted):
    for part in dotted.split("."):
        if not isinstance(obj, dict):
            return None
        obj = obj.get(part)
    return obj


def num(value, unit="", digits=0):
    if value is None:
        return "-"
    return f"{float(value):,.{digits}f}{unit}"


def progress_line(p):
    last = p.get("last") or {}
    line = (
        f"  verifying {p.get('version')}  {num(p.get('elapsed_s'))}/{num(p.get('window_s'))} s"
        f"  votes {p.get('votes', 0)}/{p.get('majority', 0)}"
    )
    if last:
        line += (
            f"  cpu {num(last.get('cpu'), '%')}  mem {num(last.get('memory'), '%')}"
            f"  p95 {num(last.get('latency_p95'), ' ms')}  errors {num(last.get('error_rate'), '%', 1)}"
        )
    return line


def release_line(row):
    flags = []
    if row.get("live"):
        flags.append("LIVE")
    if row.get("best"):
        flags.append("BEST")
    if row.get("quarantined"):
        flags.append("QUARANTINED")
    score = "-" if row.get("score") is None else str(row.get("score"))
    decision = row.get("decision") or row.get("status") or "-"
    commit = (row.get("commit") or {}).get("sha", "")[:7]
    tail = f"  commit {commit}" if commit else ""
    return f"    {row.get('version', '?'):<16} score {score:<5} {decision:<10} {' '.join(flags)}{tail}"


if mode == "message":
    print(
        data.get("message")
        or data.get("error")
        or data.get("status")
        or raw.strip()[:300]
        or "no details"
    )
elif mode == "field":
    value = dig(data, args[0])
    if isinstance(value, (dict, list)):
        print(json.dumps(value))
    elif value is not None:
        print(value)
elif mode == "apps":
    apps = data.get("apps") or []
    if not apps:
        print("  No apps registered yet. ./scripts/start.sh seeds GatePass and Notes.")
    for app in apps:
        print(
            f"  {app.get('id', '?'):<12} live {app.get('live_version') or '-':<12}"
            f" best {app.get('best_version') or '-':<12} {app.get('last_decision') or 'idle'}"
        )
elif mode == "app":
    base = args[0]
    app = data.get("app") or {}
    print(f"{app.get('name')} ({app.get('id')})  http://127.0.0.1:{app.get('host_port')}/")
    print(
        f"  live {app.get('live_version') or '-'}   best {app.get('best_version') or '-'}"
        f"   state {app.get('last_decision') or 'idle'}"
    )
    incident = data.get("last_incident") or {}
    if incident:
        line = (
            f"  last incident  {incident.get('version')} anomaly ({incident.get('confidence') or '-'}),"
            f" detected in {num(incident.get('time_to_detect_s'), ' s', 1)}"
        )
        if incident.get("restored_version"):
            line += (
                f", rolled back to {incident['restored_version']}"
                f" in {num(incident.get('time_to_rollback_s'), ' s', 1)}"
            )
        print(line)
    if data.get("verifying"):
        print(progress_line(data["verifying"]))
    github = app.get("github") or {}
    if github.get("repo"):
        print(
            f"  github  {github['repo']}@{github.get('branch', 'release')}:{github.get('path', '.')}"
            f"  last {str(github.get('last_sha') or '-')[:7]}  {github.get('state') or ''}"
        )
    releases = data.get("releases") or []
    if releases:
        print("  releases")
        for row in releases:
            print(release_line(row))
    fix = data.get("fix") or {}
    if fix.get("status"):
        print(f"  ai fix  {fix.get('status')} (job {fix.get('id') or fix.get('job_id')})")
    print(f"  console {base}/apps/{app.get('id')}")
elif mode == "verdict":
    version = args[0]
    app = data.get("app") or {}
    row = next((r for r in data.get("releases") or [] if r.get("version") == version), {})
    print(
        row.get("decision") or row.get("status") or "pending",
        app.get("last_decision") or "idle",
        app.get("live_version") or "-",
        row.get("verify_error") or "",
    )
elif mode == "progress":
    p = data.get("verifying")
    print(progress_line(p) if p else "  waiting for the first sample...")
elif mode == "fix":
    print(f"AI fix {data.get('id') or data.get('job_id') or ''}: {data.get('status')}")
    attempts = data.get("attempts") or []
    if len(attempts) > 1:
        print(f"  attempts: {len(attempts)}")
    if data.get("error"):
        print(f"  error: {data['error']}")
    if data.get("summary"):
        print(f"\nSummary\n  {data['summary']}")
    bugs = data.get("bugs") or []
    if bugs:
        print("\nBugs")
        for bug in bugs:
            where = bug.get("file") or "?"
            if bug.get("line"):
                where += f":{bug['line']}"
            print(f"  {where}  {bug.get('reason')}")
    if data.get("diff"):
        print("\nDiff")
        print(data["diff"].rstrip())
else:
    raise SystemExit(f"unknown format mode {mode}")
PY
)"

BODY=""
HTTP_CODE=""

service_down() {
  echo "RewindAI is not running at ${REWIND_URL}." >&2
  echo "Start it with ./scripts/start.sh" >&2
  exit 1
}

# request METHOD PATH [curl args...] sets BODY and HTTP_CODE.
request() {
  local method="$1" path="$2"
  shift 2
  local out
  out="$(mktemp /tmp/rewind-cli.XXXXXX)"
  if ! HTTP_CODE="$(curl -sS -o "${out}" -w '%{http_code}' -X "${method}" "${REWIND_URL}${path}" "$@" 2>/dev/null)"; then
    rm -f "${out}"
    service_down
  fi
  BODY="$(cat "${out}")"
  rm -f "${out}"
}

fmt() {
  python3 -c "${PY_FMT}" "$@" <<<"${BODY}"
}

# check exits with the server's message when the last request failed.
check() {
  if [[ "${HTTP_CODE}" -ge 400 ]]; then
    echo "RewindAI said (HTTP ${HTTP_CODE}): $(fmt message)" >&2
    exit 1
  fi
}

json_body() {
  python3 -c 'import json, sys; print(json.dumps(dict(zip(sys.argv[1::2], sys.argv[2::2]))))' "$@"
}

cmd_status() {
  local app_id="" as_json=0
  for arg in "$@"; do
    case "${arg}" in
      --json) as_json=1 ;;
      *) app_id="${arg}" ;;
    esac
  done
  if [[ -z "${app_id}" ]]; then
    request GET /api/apps
    check
    if [[ "${as_json}" == "1" ]]; then echo "${BODY}"; else fmt apps; fi
    return
  fi
  request GET "/api/apps/${app_id}"
  check
  if [[ "${as_json}" == "1" ]]; then echo "${BODY}"; else fmt app "${REWIND_URL}"; fi
}

cmd_register() {
  local id="" name="" host_port=8080 container_port=8080 health="/health" probes="/" on_anomaly="rollback"
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --id) id="$2"; shift 2 ;;
      --name) name="$2"; shift 2 ;;
      --host-port) host_port="$2"; shift 2 ;;
      --container-port) container_port="$2"; shift 2 ;;
      --health) health="$2"; shift 2 ;;
      --probes) probes="$2"; shift 2 ;;
      --on-anomaly) on_anomaly="$2"; shift 2 ;;
      *) echo "unknown arg: $1" >&2; exit 1 ;;
    esac
  done
  [[ -n "${id}" ]] || { echo "--id is required" >&2; exit 1; }
  local payload
  payload="$(python3 - "${id}" "${name:-${id}}" "${host_port}" "${container_port}" "${health}" "${probes}" "${on_anomaly}" <<'PY'
import json, sys
app_id, name, host_port, container_port, health, probes, on_anomaly = sys.argv[1:]
print(json.dumps({
    "id": app_id,
    "name": name,
    "host_port": int(host_port),
    "container_port": int(container_port),
    "health_path": health,
    "probe_paths": [p.strip() for p in probes.split(",") if p.strip()],
    "on_anomaly": on_anomaly,
}))
PY
)"
  request POST /api/apps -H 'Content-Type: application/json' -d "${payload}"
  check
  echo "Registered ${id}."
}

# watch_verify ID VERSION follows verification until RewindAI decides.
watch_verify() {
  local app_id="$1" version="$2" started=${SECONDS} decision state live problem
  while true; do
    request GET "/api/apps/${app_id}"
    check
    read -r decision state live problem <<<"$(fmt verdict "${version}")"
    if [[ "${decision}" == "error" ]]; then
      printf '\r\033[K'
      echo "Verification of ${version} stopped with an error: ${problem:-see docker logs rewindai}" >&2
      echo "Live: ${live}. Fix the cause, then run: ./scripts/rewind.sh pick ${app_id} <version>" >&2
      return 1
    fi
    if [[ "${state}" != "verifying" && ( "${decision}" == "healthy" || "${decision}" == "anomaly" ) ]]; then
      printf '\r\033[K'
      if [[ "${decision}" == "healthy" ]]; then
        echo "${version} verified healthy. Live: ${live}."
        return 0
      fi
      echo "${version} failed verification. Live: ${live}."
      fmt app "${REWIND_URL}"
      return 2
    fi
    if (( SECONDS - started > 600 )); then
      echo
      echo "Still verifying after 10 minutes; check the console." >&2
      return 1
    fi
    printf '\r\033[K%s' "$(fmt progress)"
    sleep 3
  done
}

cmd_watch() {
  local app_id="${1:-}" version="${2:-}"
  [[ -n "${app_id}" && -n "${version}" ]] || { echo "Usage: rewind.sh watch ID VERSION" >&2; exit 1; }
  watch_verify "${app_id}" "${version}"
}

cmd_publish() {
  local app_id="${1:-}"
  shift || true
  [[ -n "${app_id}" ]] || { echo "app id required" >&2; exit 1; }
  local version="" dir="" image="" base="" edits="" skip="" wait=0
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --version) version="$2"; shift 2 ;;
      --dir) dir="$2"; shift 2 ;;
      --image) image="$2"; shift 2 ;;
      --base) base="$2"; shift 2 ;;
      --edits) edits="$2"; shift 2 ;;
      --skip-verify) skip=1; shift ;;
      --wait) wait=1; shift ;;
      *) echo "unknown arg: $1" >&2; exit 1 ;;
    esac
  done
  [[ -n "${version}" ]] || { echo "--version is required" >&2; exit 1; }

  echo "Publishing ${app_id} ${version} (build + deploy)..."
  if [[ -n "${dir}" ]]; then
    local stage archive
    stage="$(mktemp -d /tmp/rewind-pub.XXXXXX)"
    archive="${stage}/source.tgz"
    if command -v ditto >/dev/null 2>&1; then
      ditto --norsrc --noextattr --noqtn "${dir}" "${stage}/src"
    else
      cp -R "${dir}" "${stage}/src"
    fi
    find "${stage}/src" -name '._*' -delete 2>/dev/null || true
    find "${stage}/src" -name '__pycache__' -prune -exec rm -rf {} + 2>/dev/null || true
    COPYFILE_DISABLE=1 tar -C "${stage}/src" -czf "${archive}" .
    local args=(-F "version=${version}" -F "source=@${archive}")
    [[ -n "${skip}" ]] && args+=(-F "skip_verify=1")
    request POST "/api/apps/${app_id}/publish" "${args[@]}"
    rm -rf "${stage}"
  else
    local payload
    payload="$(python3 - "${version}" "${image}" "${base}" "${edits}" "${skip}" <<'PY'
import json, sys
version, image, base, edits, skip = sys.argv[1:]
payload = {"version": version}
if image:
    payload["image"] = image
if base:
    payload["base"] = base
if edits:
    payload["edits"] = json.loads(open(edits).read())
if skip:
    payload["skip_verify"] = True
print(json.dumps(payload))
PY
)"
    request POST "/api/apps/${app_id}/publish" -H 'Content-Type: application/json' -d "${payload}"
  fi
  check
  fmt message
  if [[ "${wait}" == "1" && -z "${skip}" ]]; then
    watch_verify "${app_id}" "${version}"
  fi
}

cmd_pick() {
  local app_id="${1:-}" version="${2:-}" force=false
  shift 2 || true
  [[ -n "${app_id}" && -n "${version}" ]] || { echo "Usage: rewind.sh pick ID VERSION [--force]" >&2; exit 1; }
  if [[ "${1:-}" == "--force" ]]; then force=true; fi
  request POST "/api/apps/${app_id}/pick" -H 'Content-Type: application/json' \
    -d "{\"version\":\"${version}\",\"force\":${force}}"
  if [[ "${HTTP_CODE}" == "409" ]]; then
    echo "${version} is quarantined. Re-run with --force to restore it anyway." >&2
    exit 1
  fi
  check
  fmt message
}

cmd_fix() {
  local app_id="${1:-}"
  shift || true
  local wait=1
  [[ "${1:-}" == "--no-wait" ]] && wait=0
  [[ -n "${app_id}" ]] || { echo "Usage: rewind.sh fix ID [--no-wait]" >&2; exit 1; }
  request POST "/api/apps/${app_id}/fix" -H 'Content-Type: application/json' -d '{}'
  if [[ "${HTTP_CODE}" == "503" ]]; then
    echo "Fix with AI is off: $(fmt field error)" >&2
    echo "Run ./scripts/ai-setup.sh to install and start Ollama." >&2
    exit 1
  fi
  check
  local job_id model
  job_id="$(fmt field id)"
  model="$(fmt field ollama.model)"
  echo "Asking ${model:-the local model} for a fix (job ${job_id})..."
  [[ "${wait}" == "1" ]] || return 0

  local started=${SECONDS} status
  while true; do
    request GET "/api/apps/${app_id}/fix?job_id=${job_id}"
    check
    status="$(fmt field status)"
    [[ "${status}" != "running" ]] && break
    if (( SECONDS - started > 900 )); then
      echo
      echo "Still running after 15 minutes; check the console." >&2
      exit 1
    fi
    printf '\r  waiting on Ollama... %ss' "$((SECONDS - started))"
    sleep 3
  done
  printf '\r\033[K'
  fmt fix
  if [[ "${status}" != "ready" ]]; then
    exit 1
  fi
  echo
  echo "Ship it:  ./scripts/rewind.sh ship ${app_id} --version $(fmt field suggested_version) --wait"
}

cmd_ship() {
  local app_id="${1:-}"
  shift || true
  local version="" wait=0
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --version) version="$2"; shift 2 ;;
      --wait) wait=1; shift ;;
      *) echo "unknown arg: $1" >&2; exit 1 ;;
    esac
  done
  [[ -n "${app_id}" ]] || { echo "Usage: rewind.sh ship ID [--version V] [--wait]" >&2; exit 1; }

  request GET "/api/apps/${app_id}/fix"
  check
  local status
  status="$(fmt field status)"
  case "${status}" in
    ready) ;;
    running)
      echo "The AI fix for ${app_id} is still running. Check again with ./scripts/rewind.sh status ${app_id}." >&2
      exit 1 ;;
    failed)
      echo "The last AI fix failed: $(fmt field error)" >&2
      echo "Run ./scripts/rewind.sh fix ${app_id} to try again." >&2
      exit 1 ;;
    shipped)
      echo "That fix already shipped as $(fmt field shipped_version)." >&2
      exit 1 ;;
    *)
      echo "There is no AI fix ready for ${app_id}. Run ./scripts/rewind.sh fix ${app_id} first." >&2
      exit 1 ;;
  esac
  version="${version:-$(fmt field suggested_version)}"
  request POST "/api/apps/${app_id}/fix/ship" -H 'Content-Type: application/json' \
    -d "$(json_body version "${version}")"
  check
  echo "Shipped the AI fix as ${version}, verification started."
  if [[ "${wait}" == "1" ]]; then
    watch_verify "${app_id}" "${version}"
  fi
}

cmd_connect_github() {
  local app_id="${1:-}"
  shift || true
  local repo="" branch="release" path="."
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --repo) repo="$2"; shift 2 ;;
      --branch) branch="$2"; shift 2 ;;
      --path) path="$2"; shift 2 ;;
      *) echo "unknown arg: $1" >&2; exit 1 ;;
    esac
  done
  [[ -n "${app_id}" && -n "${repo}" ]] || {
    echo "Usage: rewind.sh connect-github ID --repo OWNER/NAME [--branch release] [--path app]" >&2
    exit 1
  }
  request POST "/api/apps/${app_id}/github" -H 'Content-Type: application/json' \
    -d "$(json_body repo "${repo}" branch "${branch}" path "${path}")"
  check
  fmt message
}

cmd_disconnect_github() {
  local app_id="${1:-}"
  [[ -n "${app_id}" ]] || { echo "Usage: rewind.sh disconnect-github ID" >&2; exit 1; }
  request DELETE "/api/apps/${app_id}/github"
  check
  fmt message
}

main() {
  local cmd="${1:-}"
  shift || true
  case "${cmd}" in
    status) cmd_status "$@" ;;
    register) cmd_register "$@" ;;
    publish) cmd_publish "$@" ;;
    watch) cmd_watch "$@" ;;
    pick) cmd_pick "$@" ;;
    fix) cmd_fix "$@" ;;
    ship) cmd_ship "$@" ;;
    connect-github) cmd_connect_github "$@" ;;
    disconnect-github) cmd_disconnect_github "$@" ;;
    -h|--help|help|"") usage ;;
    *) echo "unknown command: ${cmd}" >&2; usage; exit 1 ;;
  esac
}

main "$@"

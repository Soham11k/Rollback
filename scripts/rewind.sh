#!/usr/bin/env bash
# CLI for the RewindAI service.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REWIND_URL="${REWIND_URL:-http://127.0.0.1:8090}"

usage() {
  cat <<'EOF'
Usage:
  rewind.sh register --id ID [--name NAME] [--host-port N] [--probes PATHS] [--on-anomaly rollback|ask]
  rewind.sh publish ID --version V --dir PATH
  rewind.sh publish ID --version V --image IMAGE
  rewind.sh publish ID --version V --base BASE --edits FILE.json
  rewind.sh status [ID]
  rewind.sh pick ID VERSION [--force]
  rewind.sh fix ID
  rewind.sh ship ID --version V
EOF
}

api() {
  local method="$1"; shift
  local path="$1"; shift
  curl -fsS -X "${method}" "${REWIND_URL}${path}" "$@"
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
  [[ -n "${id}" ]] || { echo "--id required" >&2; exit 1; }
  name="${name:-${id}}"
  python3 - "${REWIND_URL}" "${id}" "${name}" "${host_port}" "${container_port}" "${health}" "${probes}" "${on_anomaly}" <<'PY'
import json, sys, urllib.request
url, app_id, name, host_port, container_port, health, probes, on_anomaly = sys.argv[1:]
payload = {
  "id": app_id,
  "name": name,
  "host_port": int(host_port),
  "container_port": int(container_port),
  "health_path": health,
  "probe_paths": [p.strip() for p in probes.split(",") if p.strip()],
  "on_anomaly": on_anomaly,
}
req = urllib.request.Request(
  url.rstrip("/") + "/api/apps",
  data=json.dumps(payload).encode(),
  headers={"Content-Type": "application/json"},
  method="POST",
)
print(urllib.request.urlopen(req).read().decode())
PY
}

cmd_publish() {
  local app_id="${1:-}"; shift || true
  [[ -n "${app_id}" ]] || { echo "app id required" >&2; exit 1; }
  local version="" dir="" image="" base="" edits="" skip=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --version) version="$2"; shift 2 ;;
      --dir) dir="$2"; shift 2 ;;
      --image) image="$2"; shift 2 ;;
      --base) base="$2"; shift 2 ;;
      --edits) edits="$2"; shift 2 ;;
      --skip-verify) skip=1; shift ;;
      *) echo "unknown arg: $1" >&2; exit 1 ;;
    esac
  done
  [[ -n "${version}" ]] || { echo "--version required" >&2; exit 1; }

  if [[ -n "${dir}" ]]; then
    local stage archive
    stage="$(mktemp -d /tmp/rewind-pub.XXXXXX)"
    archive="${stage}/source.tgz"
    # Clean copy without AppleDouble
    ditto --norsrc --noextattr --noqtn "${dir}" "${stage}/src"
    find "${stage}/src" -name '._*' -delete 2>/dev/null || true
    tar -C "${stage}/src" -czf "${archive}" .
    local args=(-F "version=${version}" -F "source=@${archive}")
    [[ -n "${skip}" ]] && args+=(-F "skip_verify=1")
    curl -fsS -X POST "${REWIND_URL}/api/apps/${app_id}/publish" "${args[@]}"
    echo
    rm -rf "${stage}"
    return
  fi

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
  curl -fsS -X POST "${REWIND_URL}/api/apps/${app_id}/publish" \
    -H 'Content-Type: application/json' \
    -d "${payload}"
  echo
}

cmd_status() {
  local app_id="${1:-}"
  if [[ -z "${app_id}" ]]; then
    api GET /api/apps
    echo
  else
    api GET "/api/apps/${app_id}"
    echo
  fi
}

cmd_pick() {
  local app_id="${1:-}" version="${2:-}" force=false
  shift 2 || true
  [[ -n "${app_id}" && -n "${version}" ]] || { echo "pick ID VERSION" >&2; exit 1; }
  if [[ "${1:-}" == "--force" ]]; then force=true; fi
  curl -fsS -X POST "${REWIND_URL}/api/apps/${app_id}/pick" \
    -H 'Content-Type: application/json' \
    -d "{\"version\":\"${version}\",\"force\":${force}}"
  echo
}

cmd_fix() {
  local app_id="${1:-}"
  [[ -n "${app_id}" ]] || { echo "fix ID" >&2; exit 1; }
  curl -fsS -X POST "${REWIND_URL}/api/apps/${app_id}/fix" \
    -H 'Content-Type: application/json' -d '{}'
  echo
}

cmd_ship() {
  local app_id="${1:-}"; shift || true
  local version=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --version) version="$2"; shift 2 ;;
      *) shift ;;
    esac
  done
  [[ -n "${app_id}" && -n "${version}" ]] || { echo "ship ID --version V" >&2; exit 1; }
  curl -fsS -X POST "${REWIND_URL}/api/apps/${app_id}/fix/ship" \
    -H 'Content-Type: application/json' \
    -d "{\"version\":\"${version}\"}"
  echo
}

main() {
  local cmd="${1:-}"; shift || true
  case "${cmd}" in
    register) cmd_register "$@" ;;
    publish) cmd_publish "$@" ;;
    status) cmd_status "$@" ;;
    pick) cmd_pick "$@" ;;
    fix) cmd_fix "$@" ;;
    ship) cmd_ship "$@" ;;
    -h|--help|help|"") usage ;;
    *) echo "unknown command: ${cmd}" >&2; usage; exit 1 ;;
  esac
}

main "$@"

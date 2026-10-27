#!/usr/bin/env bash
# Verify deploy.sh blocks quarantined versions and FORCE_DEPLOY overrides.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STATE_DIR="$(mktemp -d /tmp/rewindai-quarantine-test.XXXXXX)"
cleanup() { rm -rf "${STATE_DIR}"; }
trap cleanup EXIT

mkdir -p "${STATE_DIR}"
cat > "${STATE_DIR}/quarantine.json" <<'EOF'
{
  "version": "2.0.0",
  "image": "rollbackai-app:v2",
  "reason": "anomaly (HIGH)"
}
EOF

echo "[test] blocked redeploy of quarantined version..."
set +e
STATE_DIR="${STATE_DIR}" FORCE_DEPLOY=0 bash "${ROOT_DIR}/scripts/deploy.sh" \
  rollbackai-app:v2 2.0.0 all >/tmp/rewindai-q-out.txt 2>/tmp/rewindai-q-err.txt
code=$?
set -e
if [[ "${code}" -ne 2 ]]; then
  echo "EXPECTED exit 2 for quarantined deploy, got ${code}" >&2
  cat /tmp/rewindai-q-err.txt >&2 || true
  exit 1
fi
grep -q "REFUSED" /tmp/rewindai-q-err.txt

echo "[test] blocked redeploy of quarantined image tag with different label..."
set +e
STATE_DIR="${STATE_DIR}" FORCE_DEPLOY=0 bash "${ROOT_DIR}/scripts/deploy.sh" \
  rollbackai-app:v2 spooky-label none >/tmp/rewindai-q-out.txt 2>/tmp/rewindai-q-err.txt
code=$?
set -e
if [[ "${code}" -ne 2 ]]; then
  echo "EXPECTED exit 2 for quarantined image, got ${code}" >&2
  exit 1
fi

echo "[test] FORCE_DEPLOY=1 reaches docker (may fail later without daemon/image)..."
set +e
STATE_DIR="${STATE_DIR}" FORCE_DEPLOY=1 bash "${ROOT_DIR}/scripts/deploy.sh" \
  rollbackai-app:v2 2.0.0 all >/tmp/rewindai-q-force.txt 2>&1
force_code=$?
set -e
# Override must get past the quarantine gate (exit 2). Anything else means Docker ran or failed later.
if [[ "${force_code}" -eq 2 ]]; then
  echo "FORCE_DEPLOY still hit quarantine gate" >&2
  cat /tmp/rewindai-q-force.txt >&2
  exit 1
fi
if grep -q "REFUSED" /tmp/rewindai-q-force.txt; then
  echo "FORCE_DEPLOY still printed REFUSED" >&2
  exit 1
fi

echo "[test] healthy version not blocked..."
# Use a fake image name that isn't quarantined; expect failure from docker, not quarantine.
set +e
STATE_DIR="${STATE_DIR}" FORCE_DEPLOY=0 bash "${ROOT_DIR}/scripts/deploy.sh" \
  rollbackai-app:v1 1.0.0 none >/tmp/rewindai-q-v1.txt 2>&1
v1_code=$?
set -e
if [[ "${v1_code}" -eq 2 ]] || grep -q "REFUSED" /tmp/rewindai-q-v1.txt; then
  echo "v1 should not be quarantined" >&2
  cat /tmp/rewindai-q-v1.txt >&2
  exit 1
fi

echo "PASS: quarantine blocking and FORCE_DEPLOY override"

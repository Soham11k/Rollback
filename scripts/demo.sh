#!/usr/bin/env bash
# Back-compat entrypoint: full distribution-morning scenario (prepare + live).
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export ROLLBACKAI_ROOT="${ROOT_DIR}"
exec bash "${ROOT_DIR}/scripts/scenario.sh" all

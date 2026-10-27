#!/usr/bin/env bash
# Install Ollama natively, keep it running as a service, and pull the RewindAI coder model.
set -euo pipefail

MODEL="${OLLAMA_MODEL:-qwen2.5-coder:7b}"

# Apple silicon needs the arm64 Homebrew in /opt/homebrew: an Intel (/usr/local)
# Ollama runs under Rosetta without the GPU and is far too slow for a 7B model.
if [[ "$(uname -m)" == "arm64" && -x /opt/homebrew/bin/brew ]]; then
  BREW=/opt/homebrew/bin/brew
elif command -v brew >/dev/null 2>&1; then
  BREW="$(command -v brew)"
else
  echo "Homebrew is required to install Ollama (https://brew.sh)." >&2
  exit 1
fi
PREFIX="$("${BREW}" --prefix)"
OLLAMA="${PREFIX}/bin/ollama"

if [[ ! -x "${OLLAMA}" ]]; then
  echo "[ai-setup] installing ollama with ${BREW}..."
  HOMEBREW_NO_AUTO_UPDATE=1 "${BREW}" install ollama
fi

if ! curl -fsS http://127.0.0.1:11434/api/tags >/dev/null 2>&1; then
  echo "[ai-setup] starting the ollama service (survives reboots)..."
  "${BREW}" services start ollama
  for _ in $(seq 1 20); do
    curl -fsS http://127.0.0.1:11434/api/tags >/dev/null 2>&1 && break
    sleep 1
  done
fi

echo "[ai-setup] pulling ${MODEL} (one-time, about 4.7 GB)..."
for attempt in 1 2 3; do
  if "${OLLAMA}" pull "${MODEL}"; then
    break
  fi
  if [[ "${attempt}" == "3" ]]; then
    echo "[ai-setup] could not pull ${MODEL}; re-run this script to resume." >&2
    exit 1
  fi
  echo "[ai-setup] pull failed, retrying in 10 s..."
  sleep 10
done

curl -fsS "http://127.0.0.1:11434/api/tags" >/dev/null
echo "[ai-setup] Ollama is ready on :11434 with ${MODEL}"

if docker ps --format '{{.Names}}' 2>/dev/null | grep -qx rewindai; then
  echo "[ai-setup] checking that the rewindai container reaches Ollama..."
  if docker exec rewindai python -c "import urllib.request; urllib.request.urlopen('http://host.docker.internal:11434/api/tags', timeout=3)" >/dev/null 2>&1; then
    echo "[ai-setup] rewindai can reach Ollama. Fix with AI is on."
  else
    echo "[ai-setup] warn: rewindai cannot reach host.docker.internal:11434 yet (start the stack and retry)"
  fi
fi

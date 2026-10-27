#!/usr/bin/env bash
# Install and start Ollama on the Mac, pull the RewindAI coder model.
set -euo pipefail

MODEL="${OLLAMA_MODEL:-qwen2.5-coder:7b}"

if ! command -v brew >/dev/null 2>&1; then
  echo "Homebrew is required to install Ollama." >&2
  exit 1
fi

if ! command -v ollama >/dev/null 2>&1; then
  echo "[ai-setup] installing ollama via Homebrew..."
  brew install ollama
fi

if ! pgrep -x ollama >/dev/null 2>&1; then
  echo "[ai-setup] starting ollama serve..."
  nohup ollama serve >/tmp/ollama-serve.log 2>&1 &
  sleep 2
fi

echo "[ai-setup] pulling ${MODEL} (one-time ~5GB download)..."
ollama pull "${MODEL}"

echo "[ai-setup] checking host API..."
curl -fsS "http://127.0.0.1:11434/api/tags" >/dev/null
echo "[ai-setup] Ollama is ready on :11434 with ${MODEL}"

if docker ps --format '{{.Names}}' 2>/dev/null | grep -qx rewindai; then
  echo "[ai-setup] checking container → host reachability..."
  if docker exec rewindai python -c "import urllib.request; urllib.request.urlopen('http://host.docker.internal:11434/api/tags', timeout=3)" >/dev/null 2>&1; then
    echo "[ai-setup] rewindai container can reach Ollama"
  else
    echo "[ai-setup] warn: rewindai cannot reach host.docker.internal:11434 yet (start the stack and retry)"
  fi
fi

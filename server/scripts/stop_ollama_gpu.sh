#!/usr/bin/env bash
set -euo pipefail
INSTALL_DIR="${OLLAMA_GPU_HOME:-$HOME/.local/ollama-gpu}"
PID_FILE="${INSTALL_DIR}/ollama-gpu.pid"
if [[ -f "${PID_FILE}" ]]; then
  kill "$(cat "${PID_FILE}")" 2>/dev/null || true
  rm -f "${PID_FILE}"
  echo "Stopped GPU Ollama"
else
  echo "GPU Ollama pid file not found"
fi

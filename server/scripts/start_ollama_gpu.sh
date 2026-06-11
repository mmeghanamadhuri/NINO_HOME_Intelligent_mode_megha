#!/usr/bin/env bash
# Start user-local GPU Ollama. Default port 11435 avoids the CPU-only snap on 11434.
set -euo pipefail

INSTALL_DIR="${OLLAMA_GPU_HOME:-$HOME/.local/ollama-gpu}"
OLLAMA_BIN="${INSTALL_DIR}/bin/ollama"
HOST="${OLLAMA_GPU_HOST:-127.0.0.1:11435}"
MODEL="${OLLAMA_MODEL:-qwen2.5:1.5b}"
PID_FILE="${INSTALL_DIR}/ollama-gpu.pid"
LOG_FILE="${INSTALL_DIR}/ollama-gpu.log"

if [[ ! -x "${OLLAMA_BIN}" ]]; then
  echo "GPU Ollama not installed. Run: bash server/scripts/install_ollama_gpu_user.sh"
  exit 1
fi

export OLLAMA_HOST="${HOST}"
export OLLAMA_MODELS="${OLLAMA_MODELS:-$HOME/.ollama/models}"
export OLLAMA_KEEP_ALIVE="${OLLAMA_KEEP_ALIVE:--1}"
export OLLAMA_NUM_PARALLEL="${OLLAMA_NUM_PARALLEL:-1}"
export OLLAMA_FLASH_ATTENTION="${OLLAMA_FLASH_ATTENTION:-1}"
export CUDA_VISIBLE_DEVICES="${CUDA_VISIBLE_DEVICES:-0}"
export LD_LIBRARY_PATH="${INSTALL_DIR}/lib/ollama:${LD_LIBRARY_PATH:-}"
export PATH="${INSTALL_DIR}/bin:${PATH}"

if [[ -f "${PID_FILE}" ]] && kill -0 "$(cat "${PID_FILE}")" 2>/dev/null; then
  echo "GPU Ollama already running (pid $(cat "${PID_FILE}")) on ${HOST}"
  exit 0
fi

echo ">>> Starting GPU Ollama on ${HOST}"
nohup "${OLLAMA_BIN}" serve >>"${LOG_FILE}" 2>&1 &
echo $! >"${PID_FILE}"
sleep 2

for _ in $(seq 1 30); do
  if curl -sf "http://${HOST}/api/tags" >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

if ! "${OLLAMA_BIN}" list 2>/dev/null | awk 'NR>1 {print $1}' | grep -qx "${MODEL}"; then
  echo ">>> Pulling ${MODEL} (first run only)..."
  "${OLLAMA_BIN}" pull "${MODEL}"
fi

echo ">>> Warming ${MODEL} on GPU"
curl -sf "http://${HOST}/api/generate" \
  -d "{\"model\":\"${MODEL}\",\"prompt\":\"ready\",\"stream\":false,\"keep_alive\":\"-1\",\"options\":{\"num_predict\":4,\"num_gpu\":-1}}" \
  >/dev/null || true

echo ">>> Status:"
"${OLLAMA_BIN}" ps || true
PROC="$("${OLLAMA_BIN}" ps 2>/dev/null | rg -o '\d+%\s*(?:GPU|CPU)(?:\s*/\s*\d+%\s*(?:GPU|CPU))?' | head -1 || true)"
VRAM="$(curl -sf "http://${HOST}/api/ps" | python3 -c "import sys,json; m=json.load(sys.stdin).get('models',[{}])[0]; print(m.get('size_vram',0))" 2>/dev/null || echo 0)"
echo "processor=${PROC:-unknown} size_vram=${VRAM}"
echo ">>> Log: ${LOG_FILE}"
echo ">>> Server auto-detects this endpoint (OLLAMA_URL=http://${HOST}/api/generate)"

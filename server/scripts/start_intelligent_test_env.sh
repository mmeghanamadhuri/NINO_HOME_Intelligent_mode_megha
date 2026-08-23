#!/usr/bin/env bash
# Start PostgreSQL, Ollama GPU, and NiNO server for Intelligent Mode live testing.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVER_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${SERVER_DIR}"

log() { echo "[intelligent-test] $*"; }

LAN_IP="$(python3 - <<'PY'
import socket
try:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.connect(("8.8.8.8", 80))
    print(s.getsockname()[0])
    s.close()
except OSError:
    print("")
PY
)"

if [[ -z "${LAN_IP}" ]]; then
  log "WARNING: Could not guess LAN IP — set NINO_SERVER_LAN_HOST in .env"
else
  export NINO_SERVER_LAN_HOST="${NINO_SERVER_LAN_HOST:-${LAN_IP}}"
  log "LAN IP for bot voice WebSocket: ${NINO_SERVER_LAN_HOST}"
fi

log "Starting PostgreSQL + memory DB..."
bash "${SCRIPT_DIR}/start_postgres.sh"

log "Starting GPU Ollama..."
bash "${SCRIPT_DIR}/start_ollama_gpu.sh" || {
  log "GPU Ollama failed — trying system ollama on :11434"
  if command -v ollama >/dev/null 2>&1; then
    nohup ollama serve >/tmp/ollama-serve.log 2>&1 &
    sleep 3
  fi
}

if [[ -f "${SERVER_DIR}/.venv/bin/activate" ]]; then
  # shellcheck disable=SC1091
  source "${SERVER_DIR}/.venv/bin/activate"
fi

if [[ -f "${SERVER_DIR}/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "${SERVER_DIR}/.env"
  set +a
fi

export SOAK_TEST_ENABLED="${SOAK_TEST_ENABLED:-0}"
export SOAK_LIVE_ESP="${SOAK_LIVE_ESP:-0}"

PID_FILE="${SERVER_DIR}/data/nino_server.pid"
LOG_FILE="${SERVER_DIR}/data/nino_server.log"
mkdir -p "${SERVER_DIR}/data"

if [[ -f "${PID_FILE}" ]] && kill -0 "$(cat "${PID_FILE}")" 2>/dev/null; then
  log "NiNO server already running (pid $(cat "${PID_FILE}"))"
else
  log "Starting NiNO server on 0.0.0.0:8000 (Intelligent Mode from .env, soak off)..."
  nohup env SOAK_TEST_ENABLED="${SOAK_TEST_ENABLED}" SOAK_LIVE_ESP="${SOAK_LIVE_ESP}" \
    python3 app.py --host 0.0.0.0 --port 8000 >>"${LOG_FILE}" 2>&1 &
  echo $! >"${PID_FILE}"
fi

log "Waiting for server health..."
for _ in $(seq 1 60); do
  if curl -sf "http://127.0.0.1:8000/api/intelligent-mode/status" >/dev/null 2>&1; then
    break
  fi
  sleep 2
done

if ! curl -sf "http://127.0.0.1:8000/api/intelligent-mode/status" >/dev/null 2>&1; then
  log "ERROR: Server did not become ready — tail ${LOG_FILE}"
  tail -n 40 "${LOG_FILE}" || true
  exit 1
fi

log "Running LAN bot discovery..."
DISCOVERY="$(curl -sf -X POST "http://127.0.0.1:8000/api/devices/discover" 2>/dev/null || echo '{}')"
echo "${DISCOVERY}" | python3 -m json.tool 2>/dev/null || echo "${DISCOVERY}"

log "Triggering one Intelligent Mode cycle..."
curl -sf -X POST "http://127.0.0.1:8000/api/intelligent-mode/run" | python3 -m json.tool || true

cat <<EOF

================================================================================
NiNO Intelligent Mode test environment is up.

  Ops dashboard:  http://${LAN_IP:-127.0.0.1}:8000/ops
  Server status:  http://${LAN_IP:-127.0.0.1}:8000/api/status
  Agent status:   http://${LAN_IP:-127.0.0.1}:8000/api/intelligent-mode/status

Connect your ESP32 bot (same Wi-Fi):
  1. Power the board and confirm it joins your Wi-Fi
  2. On ESP serial:  voice connect ${LAN_IP:-<PC_IP>} 8000
  3. Then:           voice wake on

Discovery rescans every ~8s, or: curl -X POST http://${LAN_IP:-127.0.0.1}:8000/api/devices/discover

Server log: ${LOG_FILE}
Stop server: kill \$(cat ${PID_FILE})
================================================================================
EOF

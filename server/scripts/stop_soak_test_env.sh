#!/usr/bin/env bash
# Stop continuous soak test monitor and NiNO server started by run_soak_test_env.sh.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVER_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
PID_FILE="${SERVER_DIR}/data/nino_server.pid"
SOAK_PID_FILE="${SERVER_DIR}/data/soak_test.pid"

log() { echo "[soak-stop] $*"; }

curl -sf -X POST "http://127.0.0.1:8000/api/intelligent-mode/soak/stop" >/dev/null 2>&1 || true

if [[ -f "${SOAK_PID_FILE}" ]]; then
  monitor_pid="$(cat "${SOAK_PID_FILE}")"
  if kill -0 "${monitor_pid}" 2>/dev/null; then
    log "Stopping soak monitor (pid ${monitor_pid})..."
    kill "${monitor_pid}" 2>/dev/null || true
  fi
  rm -f "${SOAK_PID_FILE}"
fi

if [[ -f "${PID_FILE}" ]]; then
  server_pid="$(cat "${PID_FILE}")"
  if kill -0 "${server_pid}" 2>/dev/null; then
    log "Stopping NiNO server (pid ${server_pid})..."
    kill "${server_pid}" 2>/dev/null || true
    sleep 2
    kill -9 "${server_pid}" 2>/dev/null || true
  fi
  rm -f "${PID_FILE}"
fi

log "Soak test environment stopped."

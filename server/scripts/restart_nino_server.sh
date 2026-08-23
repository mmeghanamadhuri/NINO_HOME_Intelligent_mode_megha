#!/usr/bin/env bash
# Restart NiNO server after coding-agent applies a server-side fix.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVER_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
PID_FILE="${SERVER_DIR}/data/nino_server.pid"
LOG_FILE="${SERVER_DIR}/data/nino_server.log"

log() { echo "[restart-nino] $*"; }

if [[ -f "${PID_FILE}" ]]; then
  OLD_PID="$(cat "${PID_FILE}")"
  if kill -0 "${OLD_PID}" 2>/dev/null; then
    log "Stopping NiNO server (pid ${OLD_PID})..."
    kill "${OLD_PID}" || true
    for _ in $(seq 1 15); do
      kill -0 "${OLD_PID}" 2>/dev/null || break
      sleep 1
    done
    kill -9 "${OLD_PID}" 2>/dev/null || true
  fi
  rm -f "${PID_FILE}"
fi

if [[ -f "${SERVER_DIR}/.venv/bin/activate" ]]; then
  # shellcheck disable=SC1091
  source "${SERVER_DIR}/.venv/bin/activate"
fi

mkdir -p "${SERVER_DIR}/data"
log "Starting NiNO server..."
cd "${SERVER_DIR}"
nohup python3 app.py --host 0.0.0.0 --port "${NINO_SERVER_PORT:-8000}" >>"${LOG_FILE}" 2>&1 &
echo $! >"${PID_FILE}"
log "NiNO server restarted (pid $(cat "${PID_FILE}"))"

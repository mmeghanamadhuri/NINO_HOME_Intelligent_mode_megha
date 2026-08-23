#!/usr/bin/env bash
# Continuous E2E soak test environment for NiNO + Intelligent Mode.
#
# Starts PostgreSQL, Ollama, NiNO server, then runs soak tests in a loop until
# you stop this script (Ctrl+C) or run stop_soak_test_env.sh.
#
# Each cycle tests: voice Q&A, face/person stack, memory, TTS, smoke probes,
# bot /status, and hands failures to Intelligent Mode (auto-fix + email).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVER_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${SERVER_DIR}"

log() { echo "[soak-env] $*"; }

if [[ -f "${SERVER_DIR}/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "${SERVER_DIR}/.env"
  set +a
fi

export INTELLIGENT_MODE="${INTELLIGENT_MODE:-1}"
export SOAK_TEST_ENABLED="${SOAK_TEST_ENABLED:-1}"
export SOAK_TEST_INTERVAL_SECONDS="${SOAK_TEST_INTERVAL_SECONDS:-90}"
export INTELLIGENT_E2E_TESTS="${INTELLIGENT_E2E_TESTS:-1}"
export INTELLIGENT_SMOKE_TESTS="${INTELLIGENT_SMOKE_TESTS:-1}"
export INTELLIGENT_SELF_DEBUG="${INTELLIGENT_SELF_DEBUG:-1}"
export SOAK_LIVE_ESP="${SOAK_LIVE_ESP:-0}"
export SOAK_VOICE_RANDOM="${SOAK_VOICE_RANDOM:-1}"
export SOAK_VOICE_QUESTIONS_PER_CYCLE="${SOAK_VOICE_QUESTIONS_PER_CYCLE:-8}"
export SOAK_VOICE_QUESTIONS_CSV="${SOAK_VOICE_QUESTIONS_CSV:-${SERVER_DIR}/data/voice_assistant_test_questions.csv}"
export SOAK_VOICE_ALL_AGES="${SOAK_VOICE_ALL_AGES:-0}"
export SOAK_START_VOLUME="${SOAK_START_VOLUME:-30}"

BOT_IP="${BOT_IP:-}"
if [[ -z "${BOT_IP}" && -f "${SERVER_DIR}/data/devices.json" ]]; then
  BOT_IP="$(python3 - <<'PY'
import json
from pathlib import Path
path = Path("data/devices.json")
if not path.is_file():
    raise SystemExit
data = json.loads(path.read_text())
devices = data.get("devices") or []
if devices:
    base = (devices[0].get("base_url") or devices[0].get("camera_url") or "").strip()
    if base.startswith("http"):
        print(base.split("//", 1)[1].split("/", 1)[0])
PY
)" || true
fi
BOT_IP="${BOT_IP:-192.168.1.148}"

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
export NINO_SERVER_LAN_HOST="${NINO_SERVER_LAN_HOST:-${LAN_IP}}"

log "Starting base Intelligent Mode stack..."
bash "${SCRIPT_DIR}/start_intelligent_test_env.sh"

if [[ -f "${SERVER_DIR}/.venv/bin/activate" ]]; then
  # shellcheck disable=SC1091
  source "${SERVER_DIR}/.venv/bin/activate"
fi

PID_FILE="${SERVER_DIR}/data/nino_server.pid"
SOAK_PID_FILE="${SERVER_DIR}/data/soak_test.pid"
mkdir -p "${SERVER_DIR}/data"

# Restart server with SOAK_TEST_ENABLED so the in-process runner starts.
if [[ -f "${PID_FILE}" ]]; then
  old_pid="$(cat "${PID_FILE}")"
  if kill -0 "${old_pid}" 2>/dev/null; then
    log "Restarting NiNO server with SOAK_TEST_ENABLED=1..."
    kill "${old_pid}" 2>/dev/null || true
    sleep 2
  fi
fi

LOG_FILE="${SERVER_DIR}/data/nino_server.log"
log "Starting NiNO server (soak mode)..."
nohup env SOAK_TEST_ENABLED=1 SOAK_TEST_INTERVAL_SECONDS="${SOAK_TEST_INTERVAL_SECONDS}" \
  SOAK_LIVE_ESP="${SOAK_LIVE_ESP}" SOAK_VOICE_RANDOM="${SOAK_VOICE_RANDOM}" \
  SOAK_VOICE_QUESTIONS_PER_CYCLE="${SOAK_VOICE_QUESTIONS_PER_CYCLE}" \
  SOAK_VOICE_QUESTIONS_CSV="${SOAK_VOICE_QUESTIONS_CSV}" \
  SOAK_VOICE_ALL_AGES="${SOAK_VOICE_ALL_AGES}" \
  python3 app.py --host 0.0.0.0 --port 8000 >>"${LOG_FILE}" 2>&1 &
echo $! >"${PID_FILE}"

log "Waiting for server + soak runner..."
for _ in $(seq 1 60); do
  if curl -sf "http://127.0.0.1:8000/api/intelligent-mode/soak/status" >/dev/null 2>&1; then
    running="$(curl -sf "http://127.0.0.1:8000/api/intelligent-mode/soak/status" | python3 -c "import sys,json; print(json.load(sys.stdin).get('runner_alive', False))" 2>/dev/null || echo False)"
    if [[ "${running}" == "True" ]]; then
      break
    fi
  fi
  sleep 2
done

if [[ -n "${BOT_IP}" && "${SOAK_START_VOLUME}" != "skip" ]]; then
  log "Setting bot speaker volume to ${SOAK_START_VOLUME}% on ${BOT_IP}..."
  if curl -sf --max-time 15 -X POST "http://${BOT_IP}/speaker/volume" \
    -H "Content-Type: application/json" \
    -d "{\"volume\": ${SOAK_START_VOLUME}}" >/dev/null; then
    log "Bot volume set to ${SOAK_START_VOLUME}%."
  else
    log "WARNING: could not set volume on http://${BOT_IP}/speaker/volume (bot offline?)"
  fi
fi

# Foreground monitor loop — keeps script alive until Ctrl+C; polls status.
echo $$ >"${SOAK_PID_FILE}"
trap 'log "Stopping soak monitor..."; rm -f "${SOAK_PID_FILE}"; curl -sf -X POST http://127.0.0.1:8000/api/intelligent-mode/soak/stop >/dev/null 2>&1 || true; exit 0' INT TERM

cat <<EOF

================================================================================
NiNO continuous soak test is RUNNING until you press Ctrl+C.

  Ops dashboard:     http://${LAN_IP:-127.0.0.1}:8000/ops
  Soak status:       http://${LAN_IP:-127.0.0.1}:8000/api/intelligent-mode/soak/status
  Run one cycle:     curl -X POST http://127.0.0.1:8000/api/intelligent-mode/soak/cycle
  Stop soak only:    curl -X POST http://127.0.0.1:8000/api/intelligent-mode/soak/stop
  Stop everything:   bash server/scripts/stop_soak_test_env.sh

Connect ESP32 bot (same Wi-Fi):
  voice connect ${LAN_IP:-<PC_IP>} 8000
  voice wake on

Each cycle (~${SOAK_TEST_INTERVAL_SECONDS}s) tests:
  • Voice Q&A — ${SOAK_VOICE_QUESTIONS_PER_CYCLE} questions/cycle from CSV bank (${SOAK_VOICE_QUESTIONS_CSV})
  • Person/face stack readiness
  • Memory, TTS, smoke probes, bot /status
  • Intelligent Mode auto-fix + email on failure

SOAK_LIVE_ESP=${SOAK_LIVE_ESP} — replies play on the bot via POST /play_wav
(Voice connect is optional; soak injects question text and drives ESP playback.)

Server log: ${LOG_FILE}
================================================================================
EOF

while true; do
  status_json="$(curl -sf "http://127.0.0.1:8000/api/intelligent-mode/soak/status" 2>/dev/null || echo '{}')"
  cycles="$(echo "${status_json}" | python3 -c "import sys,json; print(json.load(sys.stdin).get('cycles_completed', 0))" 2>/dev/null || echo 0)"
  last_ok="$(echo "${status_json}" | python3 -c "
import sys, json
d = json.load(sys.stdin)
lc = d.get('last_cycle') or {}
print('ok' if lc.get('ok') else 'FAIL' if lc else '—')
" 2>/dev/null || echo "—")"
  log "Soak monitor: cycles=${cycles} last=${last_ok} (Ctrl+C to stop)"
  sleep 30
done

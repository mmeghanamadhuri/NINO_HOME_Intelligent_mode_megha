#!/usr/bin/env bash
# Set bot volume to 30%, then run Intelligent Mode soak for 2 hours (or until stop).
#
# Usage:
#   bash server/scripts/start_soak_2h.sh
#   BOT_IP=192.168.1.148 bash server/scripts/start_soak_2h.sh
#
# Stop early:
#   curl -X POST http://127.0.0.1:8000/api/intelligent-mode/soak/stop
#   bash server/scripts/stop_soak_test_env.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVER_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${SERVER_DIR}"

log() { echo "[soak-2h] $*"; }

if [[ -f "${SERVER_DIR}/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "${SERVER_DIR}/.env"
  set +a
fi

BOT_IP="${BOT_IP:-}"
if [[ -z "${BOT_IP}" && -f "${SERVER_DIR}/data/devices.json" ]]; then
  BOT_IP="$(python3 - <<'PY'
import json
from pathlib import Path
path = Path("data/devices.json")
data = json.loads(path.read_text())
devices = data.get("devices") or []
if devices:
    base = (devices[0].get("base_url") or devices[0].get("camera_url") or "").strip()
    if base.startswith("http"):
        print(base.split("//", 1)[1].split("/", 1)[0])
PY
)"
fi
BOT_IP="${BOT_IP:-192.168.1.148}"
SERVER_URL="${SERVER_URL:-http://127.0.0.1:8000}"
VOLUME="${SOAK_START_VOLUME:-30}"
SOAK_HOURS="${SOAK_TEST_HOURS:-2}"
MAX_SECONDS=$((SOAK_HOURS * 3600))

export SOAK_TEST_ENABLED=1
export SOAK_TEST_MAX_DURATION_SECONDS="${SOAK_TEST_MAX_DURATION_SECONDS:-${MAX_SECONDS}}"
export SOAK_VOICE_ALL_AGES=1
export SOAK_VOICE_QUESTIONS_PER_CYCLE="${SOAK_VOICE_QUESTIONS_PER_CYCLE:-6}"
export SOAK_TEST_INTERVAL_SECONDS="${SOAK_TEST_INTERVAL_SECONDS:-90}"
export SOAK_LIVE_ESP="${SOAK_LIVE_ESP:-1}"
export INTELLIGENT_MODE="${INTELLIGENT_MODE:-1}"

log "Setting speaker volume to ${VOLUME}% on bot ${BOT_IP}..."
if curl -sf -X POST "http://${BOT_IP}/speaker/volume" \
  -H "Content-Type: application/json" \
  -d "{\"volume\": ${VOLUME}}" >/dev/null; then
  log "Volume set to ${VOLUME}%."
else
  log "WARNING: could not set volume on http://${BOT_IP}/speaker/volume — continuing anyway."
fi

log "Checking NiNO server at ${SERVER_URL}..."
if ! curl -sf "${SERVER_URL}/api/intelligent-mode/status" >/dev/null 2>&1; then
  log "Server not up — starting soak environment..."
  bash "${SCRIPT_DIR}/run_soak_test_env.sh" &
  ENV_PID=$!
  for _ in $(seq 1 90); do
    if curl -sf "${SERVER_URL}/api/intelligent-mode/status" >/dev/null 2>&1; then
      break
    fi
    sleep 2
  done
  if ! curl -sf "${SERVER_URL}/api/intelligent-mode/status" >/dev/null 2>&1; then
    kill "${ENV_PID}" 2>/dev/null || true
    log "ERROR: server did not start. Check ${SERVER_DIR}/data/nino_server.log"
    exit 1
  fi
fi

log "Starting soak test for ${SOAK_HOURS} hour(s) (${SOAK_TEST_MAX_DURATION_SECONDS}s max)..."
curl -sf -X POST "${SERVER_URL}/api/intelligent-mode/soak/stop" >/dev/null 2>&1 || true
START_JSON="$(curl -sf -X POST "${SERVER_URL}/api/intelligent-mode/soak/start?max_duration_seconds=${SOAK_TEST_MAX_DURATION_SECONDS}")"
echo "${START_JSON}" | python3 -m json.tool 2>/dev/null || echo "${START_JSON}"

cat <<EOF

================================================================================
Soak test RUNNING — all age-group voice Q&A + timers/reminders + Intelligent Mode

  Ops dashboard:  ${SERVER_URL}/ops
  Soak status:    ${SERVER_URL}/api/intelligent-mode/soak/status
  Stop soak:      curl -X POST ${SERVER_URL}/api/intelligent-mode/soak/stop

  Bot volume:     ${VOLUME}% (${BOT_IP})
  Max duration:   ${SOAK_TEST_MAX_DURATION_SECONDS}s (~${SOAK_HOURS}h)
  Cycle interval: ${SOAK_TEST_INTERVAL_SECONDS}s

Each cycle tests kids / tweens / teens / adults / seniors questions,
alarms, timers, reminders, face, memory, TTS, and auto-fix via Intelligent Mode.
================================================================================
EOF

MONITOR_PID_FILE="${SERVER_DIR}/data/soak_2h_monitor.pid"
echo $$ >"${MONITOR_PID_FILE}"
trap 'rm -f "${MONITOR_PID_FILE}"; exit 0' INT TERM

while true; do
  status_json="$(curl -sf "${SERVER_URL}/api/intelligent-mode/soak/status" 2>/dev/null || echo '{}')"
  running="$(echo "${status_json}" | python3 -c "import sys,json; print(json.load(sys.stdin).get('runner_alive', False))" 2>/dev/null || echo False)"
  cycles="$(echo "${status_json}" | python3 -c "import sys,json; print(json.load(sys.stdin).get('cycles_completed', 0))" 2>/dev/null || echo 0)"
  if [[ "${running}" != "True" ]]; then
    log "Soak runner stopped (cycles=${cycles}). Done."
    break
  fi
  log "Soak running: cycles=${cycles} (Ctrl+C to stop monitor; soak stops after ${SOAK_HOURS}h or soak/stop API)"
  sleep 60
done

rm -f "${MONITOR_PID_FILE}"

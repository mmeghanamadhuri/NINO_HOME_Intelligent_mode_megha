# NiNO Intelligent Mode — Verified Working (2026-08-23)

This document records what was **cross-checked and confirmed working** on the Megha home environment before pushing to [NINO_HOME_Intelligent_mode_megha](https://github.com/mmeghanamadhuri/NINO_HOME_Intelligent_mode_megha.git).

**Environment:** Server `192.168.1.156:8000` · Bot `b0a6048addd4` (NINO - HOME Vaseekaran) · Ollama GPU `127.0.0.1:11435` · Soak **off** (live mode)

---

## Verification summary

| Check | Result |
|-------|--------|
| Unit tests `test_intelligent*.py` | **151 passed** |
| `GET /api/intelligent-mode/status` | **200** — enabled, running, 45s poll |
| `GET /api/intelligent-mode/dashboard` | **200** — ops data loads |
| `GET /ops` | **200** — dashboard UI |
| Device discovery | **1 bot live** on LAN |
| Soak runner | **Off** (`SOAK_TEST_ENABLED=0`, `SOAK_LIVE_ESP=0`) |

---

## What works

### 1. Master orchestrator (background loop)

- Runs every **~45 seconds** when `INTELLIGENT_MODE=1`.
- Collects a full system snapshot (devices, camera, TTS, STT, LLM, memory, bot runtime).
- Skips auto-fix while live voice is active (`INTELLIGENT_SKIP_FIX_DURING_VOICE=1`).
- Sends email digests (not per-incident spam) when configured.

### 2. Detection layer

| Source | Purpose |
|--------|---------|
| **L1 smoke tests** (~10 checks) | Ollama, Whisper, TTS, bot HTTP, camera, playback routes |
| **L2 e2e voice tests** (4 in-process) | Mock voice pipeline without ESP hardware |
| **Baseline anomalies** | 3σ drift on latency / smoke pass ratio |
| **Live voice incidents** | From `latency_log.json` with wake-reject / stale filters |
| **Discovery health** | LAN bot online/offline |

False-positive filters suppress noise from wake rejects, stale GPU port 11434 when 11435 is OK, and soak skip ≠ fail.

### 3. Agent remediation (runtime fixes, not developer escalation)

| Pattern | Auto-handled |
|---------|--------------|
| WAV too large | Auto-split via `esp_playback.py` + recovery chain |
| STT empty / no speech | Whisper GPU/CPU reload → voice state reset |
| Valid soak reply (wording differs) | Auto-resolve without fix chain |
| Unexpected soak reply | Voice pipeline recovery when needed |

True **code bugs** still escalate; ops restarts do not falsely mark them resolved.

### 4. Fix selection and recovery

- **Fix history** reorders recovery chains from past verified outcomes.
- **LLM fix selector** picks actions when confidence ≥ medium (configurable).
- **Autonomous recovery** up to tier 2 with max 2 chain steps.
- **Verification agent** re-probes after fix; incident stays open until probes pass.

### 5. Experience learning loop

- **`experience_playbook.json`** stores verified fix outcomes (not just attempt counts).
- Playbook hints feed LLM fix selection and recovery ordering.
- Learning runs **after verification succeeds**, not on unverified attempts.

### 6. Ops dashboard (`/ops`)

- Fleet / per-device views with health badges.
- Open incidents, agent activity, smoke/e2e last run, soak status.
- Developer issues tab, OTA pending, coding-agent proposals (when enabled).
- API: `/api/intelligent-mode/dashboard`, `/api/intelligent-mode/incidents`.

### 7. Soak test infrastructure (available, currently disabled for live use)

- CSV question bank: `server/data/voice_assistant_test_questions.csv` (502 questions).
- Rotating voice scenarios via `soak_voice_questions.py`.
- `SOAK_LIVE_ESP=0` — no automated TTS on physical bot during tests.
- Re-enable with `SOAK_TEST_ENABLED=1` and `run_soak_test_env.sh` when needed.

### 8. Voice / TTS on Vaseekaran bot (`b0a6048addd4`)

- **Double-audio fix:** HTTP `/play_wav` only (WS sends metadata, no duplicate WAV bytes).
- Config: `VOICE_HTTP_PLAY_WAV_DEVICES=b0a6048addd4` (set `0` for WS-only on all bots).
- Live voice sessions, wake word, continue_listen, and eye expressions work.

### 9. Firmware — WiFi chime dedup (in repo, needs flash)

- `WIFI.wav` (“connected to WiFi”) no longer races on boot — critical section prevents double queue.
- Flash/OTA required on the physical bot for this fix to take effect.

### 10. Email reporting

- Plain-English summary at top of digest emails.
- Sections: **Handled by Intelligent Mode** vs **Developer action needed**.
- Subject lines: `Auto-fixed` / `Auto-fix in progress` / escalation as appropriate.

---

## How to start (live home mode)

```bash
cd ESP-P4-UK-Demo
bash server/scripts/stop_soak_test_env.sh
SOAK_TEST_ENABLED=0 SOAK_LIVE_ESP=0 bash server/scripts/start_intelligent_test_env.sh
```

- Main UI: http://192.168.1.156:8000
- Ops: http://192.168.1.156:8000/ops
- Status: http://192.168.1.156:8000/api/intelligent-mode/status

---

## How to re-verify

```bash
cd server
.venv/bin/python -m unittest discover -s . -p 'test_intelligent*.py' -q
curl -s http://127.0.0.1:8000/api/intelligent-mode/status | python3 -m json.tool
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8000/api/intelligent-mode/dashboard
```

Expected: all tests OK, status `enabled: true`, dashboard HTTP 200.

---

## Known open items (not blocking)

- **5 open incidents** in live environment (mostly discovery/voice verification retries from earlier sessions).
- **Firmware WiFi chime fix** — in git; bot needs reflash for boot-time dedup.
- **Coding agent** — disabled by default (`CODING_AGENT_ENABLED=0`).

---

## Key files

| Area | Path |
|------|------|
| Orchestrator | `server/intelligent_mode/orchestrator.py` |
| Config | `server/intelligent_mode/config.py`, `server/.env.example` |
| Experience playbook | `server/intelligent_mode/experience_playbook.py` |
| Ops dashboard | `server/intelligent_mode/dashboard.py`, `server/templates/ops.html` |
| Soak + CSV bank | `server/intelligent_mode/soak_test.py`, `soak_voice_questions.py` |
| Double-TTS fix | `server/app.py` (`VOICE_HTTP_PLAY_WAV_DEVICES`) |
| WiFi chime fix | `main/main.c` |
| Changelog | `docs/INTELLIGENT_MODE_CHANGELOG.md` |

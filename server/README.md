# NiNO Python Server

FastAPI server for the NiNO ESP32-P4 demo. It pulls camera frames from the board (or a local webcam), runs face recognition, handles voice queries over WebSocket, supports **automatic voice face registration** for unknown visitors, manages alarms and PostgreSQL memory, and sends TTS audio plus eye-expression tags back to the ESP.

---

## Table of contents

- [Architecture](#architecture)
- [Quick start](#quick-start)
- [Configuration](#configuration)
- [Modules](#modules)
- [Voice pipeline](#voice-pipeline)
- [Face recognition](#face-recognition)
- [Automatic face registration](#automatic-face-registration)
- [Memory (PostgreSQL)](#memory-postgresql)
- [Startup summary greeting](#startup-summary-greeting)
- [ESP TTS chunking](#esp-tts-chunking)
- [Alarms](#alarms)
- [HTTP & WebSocket API](#http--websocket-api)
- [Data files](#data-files)
- [Tests](#tests)
- [Scripts](#scripts)
- [Troubleshooting](#troubleshooting)

---

## Architecture

```text
app.py
  ├── camera.py              ESP snapshot / MJPEG / local webcam
  ├── face_service.py        YuNet + SFace → face_embeddings.json
  ├── face_registration_service.py  Unknown face → prompt → name → capture
  ├── face_registration_voice.py    Parse “my name is …” from STT
  ├── voice_service.py       WebSocket STT → route → TTS
  ├── llm_service.py         Ollama voice replies + greeting prompts
  ├── tts_service.py         ElevenLabs / SAPI / espeak → esp_playback.py
  ├── esp_playback.py        POST /play_wav + eye / prompt_ack headers
  ├── esp_wav_chunking.py    Split long TTS into ESP-sized WAV clips
  ├── network_util.py        LAN IP + VOICE_WS_URL for ESP pairing
  ├── alarm_service.py       Voice + scheduler + medical ack
  └── memory_service.py      PostgreSQL conversations + recall + daily summaries
```

---

## Quick start

### 1. Virtual environment

```bash
cd server
python3 -m venv .venv
source .venv/bin/activate          # Windows: .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

**Dependencies:** FastAPI, OpenCV, NumPy, faster-whisper, psycopg2, python-dotenv.

### 2. Environment file

```bash
cp .env.example .env
```

Edit `.env` for your machine:

```bash
DATABASE_URL=postgresql://nino:nino@127.0.0.1:5432/nino_memory
OLLAMA_URL=http://127.0.0.1:11435/api/generate
OLLAMA_MODEL=qwen2.5:1.5b
MEMORY_SUMMARY_CRON=1
MEMORY_SUMMARY_CRON_TIME=00:05
# ESP_PLAY_WAV_URL=http://192.168.0.96/play_wav
# VOICE_WS_URL=ws://192.168.0.100:8000/voice-query
# FACE_REG_ENABLED=1
# ELEVENLABS_API_KEY=sk_...
```

`.env` is loaded automatically on startup when `python-dotenv` is installed.

### 3. PostgreSQL (optional)

```bash
bash scripts/init_memory_db.sh
```

### 4. GPU Ollama on Linux (recommended)

```bash
bash scripts/install_ollama_gpu_user.sh   # one-time
bash scripts/start_ollama_gpu.sh          # each boot
```

The server resolves `--ollama-url auto` to GPU port **11435** first.

### 5. Run

```bash
python app.py --host 0.0.0.0 --port 8000 \
  --camera-source http://<ESP_IP>/stream \
  --esp-play-wav-url http://<ESP_IP>/play_wav \
  --database-url "$DATABASE_URL"
```

Open **[http://localhost:8000](http://localhost:8000)** for the web UI.

### 6. Connect ESP voice

On the ESP serial console:

```text
voice connect <PC_LAN_IP> 8000
voice wake on
```

---

## Configuration

### Precedence

1. CLI flags (`python app.py --…`)
2. Environment variables / `.env`
3. `server_config.json` (optional JSON — do not commit secrets)

### Common CLI flags

| Flag | Purpose |
| ---- | ------- |
| `--camera-source` | ESP stream URL, `auto`, or device index |
| `--esp-play-wav-url` | ESP `POST /play_wav` endpoint |
| `--database-url` | PostgreSQL connection string |
| `--ollama-url` | Ollama API URL (`auto` = GPU first on Linux) |
| `--ollama-model` | Model name (default `qwen2.5:1.5b`) |
| `--stt-provider` | `elevenlabs` or `whisper` |
| `--tts-provider` | `elevenlabs`, `sapi`, or `local` |
| `--face-threshold` | SFace match threshold (default 0.42) |


| Variable | Default | Purpose |
| -------- | ------- | ------- |
| `DATABASE_URL` | — | PostgreSQL connection (required for memory) |
| `MEMORY_EXTRACTION` | on when DB set | Phase B — LLM long-term fact extraction |
| `MEMORY_RECENT_TURNS` | `10` | Recent conversation lines in voice prompt |
| `MEMORY_TOP_MEMORIES` | `10` | Max stored facts injected per turn |
| `MEMORY_MIN_IMPORTANCE` | `5` | Min importance score to store a fact |
| `MEMORY_SUMMARY_CRON` | `0` | Phase C — enable daily summaries |
| `MEMORY_SUMMARY_CRON_TIME` | `00:05` | Local time (HH:MM) to summarize yesterday |
| `STARTUP_GREETING_TEMPERATURE` | `VOICE_REPLY_TEMPERATURE` | LLM variety for startup counter-question |

### ESP playback variables

| Variable | Default | Purpose |
| -------- | ------- | ------- |
| `ESP_PLAY_WAV_URL` | derived from `CAMERA_SOURCE` | ESP `POST /play_wav` endpoint |
| `ESP_MAX_PLAY_WAV_BYTES` | `389120` | Max WAV size (~384 KiB firmware limit) |
| `TTS_PROVIDER` | auto | `elevenlabs`, `sapi`, or `local` (espeak) |
| `LOCAL_TTS_RATE` | derived | espeak rate tweak (see `tts_service.py`) |
| `VOICE_WS_URL` | — | Full WebSocket URL pushed to ESP on `prompt_ack` |
| `NINO_SERVER_LAN_HOST` | auto-detect | PC LAN IP when building `VOICE_WS_URL` |
| `NINO_SERVER_PORT` | `8000` | Port in auto-built `VOICE_WS_URL` |

### Face registration variables

| Variable | Default | Purpose |
| -------- | ------- | ------- |
| `FACE_REG_ENABLED` | `1` | Automatic voice registration for unknown faces |
| `FACE_REG_UNKNOWN_SECONDS` | `3` | Seconds unknown face must stay in frame before prompt |
| `FACE_REG_PROMPT_COOLDOWN_SECONDS` | `600` | Min seconds between registration prompts |
| `FACE_REG_SAMPLES` | `15` | Face crops per registration (same as web UI) |
| `FACE_REG_INTERVAL_MS` | `150` | Delay between sample captures |
| `FACE_REG_AWAIT_NAME_SECONDS` | `45` | Drop `awaiting_name` if no voice response |
| `FACE_REG_NO_SPEECH_RETRY_SECONDS` | `6` | Wait after prompt playback before no-speech retry |
| `FACE_REG_LISTEN_OPEN_DELAY_SECONDS` | `3` | Extra delay before treating listen window as silent |
| `FACE_REG_MAX_NO_SPEECH_RETRIES` | `2` | Max automatic re-prompts when mic stays silent |

Requires `ESP_PLAY_WAV_URL` (or `CAMERA_SOURCE=http://<ESP_IP>/stream`) for proactive prompts.

---

## Modules

| File | Role |
| ---- | ---- |
| `app.py` | FastAPI routes, MJPEG generator, voice WebSocket, wiring |
| `camera.py` | HTTP snapshot polling, local USB fallback |
| `face_service.py` | YuNet detection, SFace embeddings, registration |
| `face_registration_service.py` | Unknown-face timer, proactive prompt, capture + train |
| `face_registration_voice.py` | Parse name phrases from STT (“my name is …”, “call me …”) |
| `network_util.py` | LAN IP guess + `VOICE_WS_URL` for ESP voice pairing |
| `llm_service.py` | Ollama voice replies, startup greeting prompts |
| `eye_expression.py` | Reply-text → eye tag scoring (voice path) |
| `tts_service.py` | TTS synthesis, startup greeting queue, ESP multi-clip playback |
| `esp_playback.py` | `POST /play_wav` + `X-Nino-Eye-Expression` header |
| `esp_wav_chunking.py` | Measure WAV size → split text at sentence/word boundaries |
| `voice_service.py` | STT, routing (alarm/servo/recap/identity/llm) |
| `alarm_service.py` | Scheduler, persistence, medical repeat |
| `alarm_voice.py` | Voice alarm parse + fire |
| `memory_service.py` | PostgreSQL users, conversations, recall, daily summaries |

---

## Voice pipeline

1. ESP sends 16 kHz WAV over WebSocket (`/ws/voice` or `/voice-query`).
2. STT: ElevenLabs Scribe (if key set) or faster-whisper.
3. Live face identity attached from camera (with TTL fallback).
4. Route (after volume + STT echo rejection):
   - **Face registration** (if `awaiting_name`)
   - alarm / servo_360 / recap / identity / general LLM
5. TTS → WAV streamed back; JSON metadata includes `eye_expression` from reply text.
6. Latency logged to `data/latency_log.json`; conversation queued to PostgreSQL.

Voice eyes come from **`eye_expression.py`** (text analysis on the LLM reply).

When face registration is waiting for a name, STT echo/garbage rejection is skipped so short name utterances are not dropped.

---

## Face recognition

- **Detector:** YuNet (`data/models/face_detection_yunet_2023mar.onnx`)
- **Recognizer:** SFace (`data/models/face_recognition_sface_2021dec.onnx`)
- **Embeddings:** `data/face_embeddings.json`
- **Samples:** `data/faces/<person_id>/sample_*.jpg`

Tune via environment:

```bash
export FACE_MATCH_THRESHOLD=0.42
export FACE_MATCH_SOFT_THRESHOLD=0.39
export FACE_CONFIRM_FRAMES=3
export FACE_SESSION_PRIMARY_HOLD_SECONDS=90
```

Register via web UI (`POST /api/register`), the home page form, or **automatic voice registration** (below). Both paths call the same `capture_face_samples()` helper.

---

## Automatic face registration

When `FACE_REG_ENABLED=1` (default), an **unknown** primary face held in frame for ~3 s triggers proactive registration — no “register my face” command required.

### Flow

```text
Unknown face stable (FACE_REG_UNKNOWN_SECONDS)
  → TTS prompt → ESP POST /play_wav  (X-Nino-Prompt-Ack: 1)
  → ESP plays prompt → beep → USB mic VAD (same pattern as medical alarm ack)
  → User: “My name is Sirena”
  → ESP WAV → WebSocket /voice-query
  → STT → parse name → capture_face_samples × N → train()
  → TTS: “All set, Sirena. I've registered your face.”
```

Camera (face samples) and USB mic (name) are separate hardware paths; the server joins them in software.

### States

| State | Meaning |
| ----- | ------- |
| `idle` | Normal operation |
| `awaiting_name` | Prompt played; next voice utterance should contain a name |
| `capturing` | Saving face samples during the voice turn |

If the name is not understood, the server re-opens the mic (`relisten_after_missed_name`). If the ESP sends no audio after the listen window, automatic no-speech retries run up to `FACE_REG_MAX_NO_SPEECH_RETRIES`.

### Status

```bash
curl -s http://localhost:8000/api/status | python3 -c "
import sys, json
print(json.dumps(json.load(sys.stdin)['face_registration'], indent=2))
"
```

Full design doc: [../docs/VOICE_FACE_REGISTRATION.md](../docs/VOICE_FACE_REGISTRATION.md)

---

## Memory (PostgreSQL)

Requires `DATABASE_URL`. Schema in `scripts/memory_schema.sql`.

| Phase | Env | Behavior |
| ----- | --- | -------- |
| A | `DATABASE_URL` | Log conversations per user |
| B | `MEMORY_EXTRACTION=1` (default when DB set) | LLM extracts long-term facts after each turn |
| C | `MEMORY_SUMMARY_CRON=1` | Daily rollup of prior-day conversations into `summaries` |

### Phase C — daily summaries

When `MEMORY_SUMMARY_CRON=1`:

1. **On server startup** — catch-up summarizes **calendar yesterday** for every user who had conversations that day (if not already summarized).
2. **Every night at `MEMORY_SUMMARY_CRON_TIME`** (default `00:05` local PC time) — same catch-up runs automatically **without restarting** the server.
3. One row per user per day in `summaries` (`summary_date`, `summary_text`).

Greetings and voice context load **yesterday's** summary only (`summary_date = today - 1`), not the oldest or newest row in the table.

Check scheduler state:

```bash
curl -s http://localhost:8000/api/status | python3 -c "
import sys, json
m = json.load(sys.stdin)['memory']
print('cron:', m.get('summary_cron_enabled'))
print('time:', m.get('summary_scheduler_time'))
print('yesterday:', m.get('summary_yesterday_date'))
print('next run in (s):', m.get('summary_next_run_in_seconds'))
"
```

Row counts:

```bash
curl -s http://localhost:8000/api/memory/stats | python3 -m json.tool
```

More detail: [../phase_c_detailed.md](../phase_c_detailed.md)

---

## Startup summary greeting

When a registered face is seen **once per server boot**, NiNO can speak a **Phase C startup greeting** (if daily summaries are enabled).

### Flow

```text
Face recognized (primary)
  → memory_service.get_latest_summary_text(name)   # calendar yesterday
  → llm_service.startup_greeting_parts_from_summary()
       1) Hi {name}, good to see you!              (fixed)
       2) Yesterday we discussed {topic}.          (fixed from summary)
       3) Counter-question                         (LLM — continue or topic follow-up)
  → tts_service._play_esp_text()                   (auto-chunked WAV clips)
```

### Notes

- Fires **once per boot** per person (`_startup_greeted` in `tts_service.py`).
- Requires `MEMORY_SUMMARY_CRON=1` and a summary row for **yesterday**; otherwise falls back to a short generic hello.

---

## ESP TTS chunking

The ESP32 `/play_wav` endpoint accepts WAV payloads up to **~384 KiB** (`ESP_MAX_PLAY_WAV_BYTES`). Long spoken replies exceed that at normal TTS speed.

`esp_wav_chunking.py` handles this generically for **any user and any text**:

1. Synthesize at normal rate and **measure** WAV bytes.
2. If it fits → one `POST /play_wav`.
3. If not → split at **sentence boundaries**, then **word boundaries** if needed.
4. Queue **N clips** on the ESP audio FIFO — they play back-to-back.

Used by startup greetings and any path through `tts_service._play_esp_text()`.

Logs show clip progress:

```text
ESP play_wav 1/2 (332644 bytes, rate=135): Hi Chakri, good to see you! Yesterday we discussed ...
ESP play_wav 2/2 (200730 bytes, rate=135): Can you tell me the different types of microcontrollers?
```

---

## Alarms

- Stored in `data/alarms.json`
- Voice set/list/cancel via `alarm_voice.py`
- Medical alarms: TOS only, repeat every 3 min, ESP auto-listens for yes/no
- Web ack: `POST /api/alarms/{id}/ack` with `{"response": "yes"}`

See [../docs/ALARM.md](../docs/ALARM.md).

---

## HTTP & WebSocket API

| Method | Path | Description |
| ------ | ---- | ----------- |
| GET | `/` | Web UI |
| GET | `/video_feed` | MJPEG with face boxes |
| GET | `/snapshot.jpg` | Latest annotated JPEG |
| GET | `/api/status` | Full system status |
| GET | `/api/latency-log?limit=N` | Voice timing events |
| GET | `/api/memory/stats` | DB row counts |
| GET | `/api/alarms` | Alarm list + scheduler state |
| POST | `/api/register` | Register face samples |
| POST | `/api/retrain` | Re-encode all stored crops |
| POST | `/api/camera` | Switch camera source JSON body |
| POST | `/api/alarms/{id}/ack` | Medical alarm confirmation |
| DELETE | `/api/alarms` | Cancel all |
| DELETE | `/api/alarms/{id}` | Cancel one |
| WS | `/ws/voice` | Voice assistant (primary) |
| WS | `/voice-query` | Alias for voice WebSocket |

### ESP playback headers

| Header | Purpose |
| ------ | ------- |
| `X-Nino-Eye-Expression` | `happy`, `sad`, `surprised`, … |
| `X-Nino-Prompt-Ack` | `1` — ESP opens mic after TTS (medical ack, face registration) |
| `X-Nino-Prompt-Ack-Chime` | `0` = listen immediately after prompt (no extra beep) |
| `X-Nino-Voice-Ws-Url` | PC WebSocket URL auto-configured on ESP (`VOICE_WS_URL` or LAN detect) |

---

## Data files

```text
data/
├── models/
│   ├── face_detection_yunet_2023mar.onnx
│   └── face_recognition_sface_2021dec.onnx
├── faces/                         Registered face crops
├── face_embeddings.json           SFace embedding store
├── alarms.json                    Pending alarms
└── latency_log.json               Voice pipeline timing (thread-safe append)
```

Do not commit `.env`, `server_config.json`, or API keys.

---

## Tests

```bash
cd server
source .venv/bin/activate
python -m unittest discover -v -p 'test_*.py'
```

| Test file | Covers |
| --------- | ------ |
| `test_eye_expression.py` | Reply → eye tag scoring |
| `test_alarm_voice.py` | Alarm phrase parsing |
| `test_memory_filters.py` | Recap context filtering |
| `test_memory_routing.py` | Voice memory routing |
| `test_memory_summary_scheduler.py` | Phase C midnight scheduler helpers |
| `test_greeting_summary.py` | Startup greeting prompts and 3-part structure |
| `test_esp_wav_chunking.py` | Generic ESP WAV text splitting |
| `test_face_registration.py` | Name parsing, unknown-face prompt, no-speech retry |
| `test_llm_memory_turn.py` | Memory prompt assembly |
| `test_volume_command.py` | Volume voice command |

---

## Scripts

| Script | Purpose |
| ------ | ------- |
| `scripts/init_memory_db.sh` | Create `nino_memory` DB + schema |
| `scripts/memory_schema.sql` | PostgreSQL tables |
| `scripts/start_ollama_gpu.sh` | Start user-local CUDA Ollama on :11435 |
| `scripts/stop_ollama_gpu.sh` | Stop GPU Ollama |
| `scripts/install_ollama_gpu_user.sh` | One-time GPU Ollama install |

---

## Troubleshooting

### Camera disconnected

- Verify `http://<ESP_IP>/snapshot.jpg` in a browser
- Check `camera.connected` and `camera.last_error` in `/api/status`

### Voice slow

- Set `ELEVENLABS_API_KEY` for fast STT
- Confirm `llm.url` points to GPU Ollama (`127.0.0.1:11435`)

### PostgreSQL / memory

- `"memory": { "ready": false }` → run `init_memory_db.sh`, set `DATABASE_URL`, restart
- No startup greeting context → enable `MEMORY_SUMMARY_CRON=1`, chat yesterday, wait for `00:05` or restart server for catch-up
- Stale greeting topic → confirm `summary_yesterday_date` in `/api/status` matches the day you expect

### ESP audio cut off or too fast

- Check logs for `ESP play_wav N/M` — multiple clips at normal `rate=` is expected for long greetings
- Single clip over ~389120 bytes will fail — chunking should split automatically; report if `last_error` appears in `/api/status` → `tts`

### Face registration not prompting

- Confirm `face_registration.enabled` is `true` and `state` is `idle` in `/api/status`
- Set `ESP_PLAY_WAV_URL` or use `CAMERA_SOURCE=http://<ESP_IP>/stream` (URL is derived automatically)
- Unknown face must stay primary in frame for `FACE_REG_UNKNOWN_SECONDS` (default 3 s)
- Check `prompt_cooldown_seconds` — a recent prompt blocks repeats for 600 s by default
- Set `VOICE_WS_URL` or `NINO_SERVER_LAN_HOST` if ESP cannot reach the PC WebSocket after `prompt_ack`

### Face registration hears name but fails capture

- Look at the camera during the voice turn — samples are taken from live frames while `capturing`
- Check `data/faces/<name>/` and re-run with better lighting / face centered in frame

---

## Related docs

- [../README.md](../README.md) — project overview, firmware, hardware
- [../docs/ALARM.md](../docs/ALARM.md) — alarm commands and medical flow
- [../docs/VOICE_FACE_REGISTRATION.md](../docs/VOICE_FACE_REGISTRATION.md) — automatic voice face registration
- [../phase_c_detailed.md](../phase_c_detailed.md) — Phase C daily summaries and startup greeting
- [../docs/serverP.md](../docs/serverP.md) — server architecture notes

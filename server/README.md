# NiNO Python Server

FastAPI server for the NiNO ESP32-P4 demo. It pulls camera frames from the board (or a local webcam), runs face recognition and **camera emotion detection**, handles voice queries over WebSocket, manages alarms and PostgreSQL memory, and sends TTS audio plus eye-expression tags back to the ESP.

---

## Table of contents

- [Architecture](#architecture)
- [Quick start](#quick-start)
- [Configuration](#configuration)
- [Modules](#modules)
- [Vision emotion pipeline](#vision-emotion-pipeline)
- [Voice pipeline](#voice-pipeline)
- [Face recognition](#face-recognition)
- [Memory (PostgreSQL)](#memory-postgresql)
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
  ├── emotion_service.py     Keras CNN (default) or FER+ ONNX
  ├── vision_emotion_service.py  2–2.5 s accumulation → empathy jobs
  ├── pipeline_priority.py   P0 voice blocks P1 vision emotion
  ├── voice_service.py       WebSocket STT → route → TTS
  ├── llm_service.py         Ollama voice replies + empathy prompts
  ├── tts_service.py         ElevenLabs / SAPI / espeak → esp_playback.py
  ├── alarm_service.py       Voice + scheduler + medical ack
  └── memory_service.py      PostgreSQL conversations + recall
```

### Priority model

| Priority | Service | Blocks when |
| -------- | ------- | ----------- |
| **P0** | Voice WebSocket (`/ws/voice`) | Active utterance + post-voice cooldown |
| **P1** | Vision emotion | P0 active, TTS busy, or within `VISION_EMOTION_AFTER_VOICE_SECONDS` |

---

## Quick start

### 1. Virtual environment

```bash
cd server
python3 -m venv .venv
source .venv/bin/activate          # Windows: .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

**Dependencies:** FastAPI, OpenCV, NumPy, **TensorFlow** (Keras emotion model), ONNX Runtime (optional FER+ fallback), faster-whisper, psycopg2, python-dotenv.

TensorFlow adds ~1.4 GB to the venv — required for the default emotion backend.

### 2. Environment file

```bash
cp .env.example .env
```

Edit `.env` for your machine:

```bash
DATABASE_URL=postgresql://nino:nino@127.0.0.1:5432/nino_memory
OLLAMA_URL=http://127.0.0.1:11435/api/generate
OLLAMA_MODEL=qwen2.5:1.5b
VISION_EMOTION_ENABLED=1
EMOTION_BACKEND=keras
# ESP_PLAY_WAV_URL=http://192.168.0.96/play_wav
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

### Vision emotion variables

All `*_S` settings are **seconds**, not milliseconds.

| Variable | Default | Purpose |
| -------- | ------- | ------- |
| `VISION_EMOTION_ENABLED` | `1` | Master switch |
| `EMOTION_BACKEND` | `keras` | `keras`, `ferplus`, `trial`, or `h5` |
| `EMOTION_KERAS_MODEL_PATH` | `data/models/emotion_model_best.h5` | Override weights path |
| `VISION_EMOTION_WINDOW_MIN_S` | `2.0` | Min time in frame before empathy |
| `VISION_EMOTION_WINDOW_MAX_S` | `2.5` | Max wait for stable emotion |
| `VISION_EMOTION_DOMINANCE` | `0.35` | Fraction of frames that must agree |
| `VISION_EMOTION_COOLDOWN_S` | `120` | Per-person empathy cooldown |
| `VISION_EMOTION_AFTER_VOICE_SECONDS` | `90` | Pause after voice query |
| `EMOTION_MIN_CONFIDENCE` | `0.12` | Min confidence for speakable class |
| `EMOTION_NEUTRAL_SUPPRESS_RATIO` | `0.22` | Promote sad/happy over dominant neutral |
| `EMOTION_SPEAKABLE_MIN` | `0.12` | Absolute min for speakable promotion |
| `EMOTION_DEVICE` | `auto` | FER+ only: `auto` / `cuda` / `cpu` |

---

## Modules

| File | Role |
| ---- | ---- |
| `app.py` | FastAPI routes, MJPEG generator, voice WebSocket, wiring |
| `camera.py` | HTTP snapshot polling, local USB fallback |
| `face_service.py` | YuNet detection, SFace embeddings, registration |
| `emotion_service.py` | Face crop, CNN inference, label mapping, neutral suppression |
| `emotion_model_loader.py` | Keras architecture + `.h5` weight load |
| `vision_emotion_service.py` | Frame accumulation, empathy job queue, overlay state |
| `pipeline_priority.py` | Voice/vision mutual exclusion |
| `llm_service.py` | Ollama voice replies, `empathy_for_detected_emotion()` |
| `eye_expression.py` | Reply-text → eye tag scoring (voice path) |
| `tts_service.py` | TTS synthesis, `speak_to_esp()`, head-motion timing |
| `esp_playback.py` | `POST /play_wav` + `X-Nino-Eye-Expression` header |
| `voice_service.py` | STT, routing (alarm/servo/recap/identity/llm) |
| `alarm_service.py` | Scheduler, persistence, medical repeat |
| `alarm_voice.py` | Voice alarm parse + fire |
| `memory_service.py` | PostgreSQL users, conversations, recall |

---

## Vision emotion pipeline

When `VISION_EMOTION_ENABLED=1` (default):

1. **MJPEG loop** reads ESP snapshots and runs face recognition.
2. **Primary face** selected (largest stabilized or strong candidate match).
3. **Emotion CNN** runs on padded face crop every frame.
4. **Votes accumulate** for 2.0–2.5 s while the same person stays in frame.
5. **Dominant speakable emotion** (≥35% of frames) queues an empathy job.
6. **Background worker** calls Ollama → TTS → `POST /play_wav` with eye header.
7. **Cooldown** — same person not addressed again for 120 s.

### Emotion model (default: Keras)

| Item | Detail |
| ---- | ------ |
| Weights | `data/models/emotion_model_best.h5` |
| Loader | `emotion_model_loader.py` |
| Input | 48×48 grayscale, `/255.0`, no CLAHE |
| Classes | anger, disgust, fear, happy, sad, surprise, neutral |
| Runtime | TensorFlow CPU |
| Seed source | `../emotion-trial/emotion_model_best.h5` if missing from `data/models/` |

### FER+ fallback

Set `EMOTION_BACKEND=ferplus` to use ONNX FER+ (`data/models/emotion-ferplus-8.onnx`, auto-downloaded). 64×64 input, 8 classes, CLAHE preprocessing.

### Speakable vs overlay-only

**Triggers empathy + eyes:** happy, surprise, sad, anger, fear  
**Overlay only:** neutral, disgust

### Eye tag mapping

| Detected | ESP header |
| -------- | ---------- |
| happy | `happy` |
| surprise | `surprised` |
| sad | `sad` |
| anger | `sad` |
| fear | `surprised` |
| neutral / disgust | (none — idle) |

### MJPEG overlay

Pink text on `/video_feed`:

```text
sad 21% raw=neutral 2.1s n:75% s:21% h:2%
```

Check status:

```bash
curl -s http://localhost:8000/api/status | python3 -m json.tool
```

Expect:

```json
"emotion": {
  "available": true,
  "backend": "keras",
  "model": "emotion_model_best.h5",
  "input_size": [48, 48]
},
"vision_emotion": {
  "enabled": true,
  "accumulating_for": "Chakri",
  "accum_frames": 42
}
```

Full design doc: [../docs/EMOTION_RECOGNITION.md](../docs/EMOTION_RECOGNITION.md)

---

## Voice pipeline

1. ESP sends 16 kHz WAV over WebSocket (`/ws/voice` or `/voice-query`).
2. STT: ElevenLabs Scribe (if key set) or faster-whisper.
3. Live face identity attached from camera (with TTL fallback).
4. Route: alarm / servo_360 / recap / identity / general LLM.
5. TTS → WAV streamed back; JSON metadata includes `eye_expression` from reply text.
6. Latency logged to `data/latency_log.json`; conversation queued to PostgreSQL.

Voice eyes come from **`eye_expression.py`** (text analysis), not the CNN — camera emotion is passive-only.

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

Register via web UI (`POST /api/register`) or the home page form.

---

## Memory (PostgreSQL)

Requires `DATABASE_URL`. Schema in `scripts/memory_schema.sql`.

| Phase | Env | Behavior |
| ----- | --- | -------- |
| A | `DATABASE_URL` | Log conversations per user |
| B | `MEMORY_EXTRACTION=1` | LLM extracts long-term facts |
| C | `MEMORY_SUMMARY_CRON=1` | Nightly summaries |

```bash
curl -s http://localhost:8000/api/memory/stats | python3 -m json.tool
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
| GET | `/video_feed` | MJPEG with face boxes + emotion overlay |
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
| `X-Nino-Prompt-Ack` | `1` — ESP listens for yes/no after medical TTS |

---

## Data files

```text
data/
├── models/
│   ├── emotion_model_best.h5      Keras emotion weights (default)
│   ├── emotion-ferplus-8.onnx     FER+ fallback (auto-download)
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
| `test_vision_emotion.py` | Emotion mapping, accumulation, Keras init |
| `test_eye_expression.py` | Reply → eye tag scoring |
| `test_alarm_voice.py` | Alarm phrase parsing |
| `test_memory_filters.py` | Recap context filtering |
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

### Emotion not available

```bash
curl -s http://localhost:8000/api/status | python3 -c "import sys,json; print(json.load(sys.stdin)['emotion'])"
```

- `"available": false` → check `data/models/emotion_model_best.h5` exists
- `tensorflow not installed` → `pip install tensorflow`
- Switch to FER+: `EMOTION_BACKEND=ferplus` in `.env`

### No empathy spoken

- Person must be **registered** and recognized (not `unknown`)
- Hold a **speakable** expression for ~2 s
- Check cooldown: `vision_emotion.last_error` in `/api/status`
- After voice query, wait `VISION_EMOTION_AFTER_VOICE_SECONDS` (90 s default)

### Camera disconnected

- Verify `http://<ESP_IP>/snapshot.jpg` in a browser
- Check `camera.connected` and `camera.last_error` in `/api/status`

### Voice slow

- Set `ELEVENLABS_API_KEY` for fast STT
- Confirm `llm.url` points to GPU Ollama (`127.0.0.1:11435`)

### PostgreSQL

- `"memory": { "ready": false }` → run `init_memory_db.sh`, set `DATABASE_URL`, restart

---

## Related docs

- [../README.md](../README.md) — project overview, firmware, hardware
- [../docs/EMOTION_RECOGNITION.md](../docs/EMOTION_RECOGNITION.md) — emotion pipeline deep dive
- [../docs/ALARM.md](../docs/ALARM.md) — alarm commands and medical flow
- [../docs/serverP.md](../docs/serverP.md) — server architecture notes

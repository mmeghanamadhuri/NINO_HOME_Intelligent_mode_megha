# NiNO Home — ESP32-P4 Voice, Vision, Emotion & Memory

NiNO is a smart-home demo built around the **ESP32-P4 Function EV Board**. The board handles camera streaming, wake-word capture, speaker playback, capacitive touch, servo motion, and animated OLED eyes. A **Python FastAPI server** on a PC runs face recognition, **camera emotion detection**, speech-to-text, LLM replies, persistent conversation memory, alarms, and TTS — then sends audio and eye-expression tags back to the board.

---

## Table of contents

- [Architecture](#architecture)
- [Features](#features)
- [Hardware](#hardware)
- [Requirements](#requirements)
- [Quick start](#quick-start)
- [Vision emotion (camera empathy)](#vision-emotion-camera-empathy)
- [PostgreSQL conversation memory](#postgresql-conversation-memory)
- [Voice assistant](#voice-assistant)
- [Face recognition](#face-recognition)
- [Alarms](#alarms)
- [Firmware: touch, servo & eyes](#firmware-touch-servo--eyes)
- [Server setup](#server-setup)
- [HTTP & WebSocket API](#http--websocket-api)
- [Environment variables](#environment-variables)
- [Repository layout](#repository-layout)
- [Troubleshooting](#troubleshooting)
- [Related docs](#related-docs)

---

## Architecture

```text
ESP32-P4 (firmware)                         PC — FastAPI server
─────────────────────                       ────────────────────
UVC camera ──► GET /stream ───────────────► CameraStream + YuNet/SFace face ID
                                             Keras CNN emotion on face crop
Wake word + mic ──► WS /voice-query ───────► STT → LLM → TTS → WAV reply
Speaker ◄──── POST /play_wav ◄───────────── empathy, alarms, voice replies
OLED eyes ◄── X-Nino-Eye-Expression ◄────── happy / sad / surprised tags
Servo ◄──── POST /servo/360 ◄────────────── voice-triggered 360° spin
Touch / eyes ── onboard only

                                            PostgreSQL (optional)
                                            users, conversations,
                                            memories, summaries
```

### Pipeline priorities

| Priority | Pipeline | Trigger | Eye delivery |
| -------- | -------- | ------- | ------------ |
| **P0** | Voice WebSocket | User says "Hi ESP" and speaks | JSON `eye_expression` + binary WAV |
| **P1** | Vision emotion | Registered face + stable emotion ~2 s | HTTP header `X-Nino-Eye-Expression` + WAV |

Voice always wins. Vision empathy is blocked during a voice query and for ~90 s afterward.

### Voice query flow

1. User says **"Hi ESP"** on the board → VAD captures speech → WebSocket to PC.
2. Server transcribes audio (ElevenLabs Scribe or local Whisper).
3. Camera resolves who is speaking from **live face recognition**.
4. If memory is enabled, recent conversation history is loaded from PostgreSQL.
5. Request is routed: volume / alarm / servo / identity / recap / general LLM.
6. Reply is synthesized to 16 kHz WAV and streamed back over the WebSocket.
7. Exchange is logged to PostgreSQL (when ready) and to `server/data/latency_log.json`.

---

## Features

| Area | What NiNO does |
| ---- | -------------- |
| **Vision** | YuNet detection + SFace 128-D embeddings; web UI registration |
| **Emotion** | 48×48 Keras CNN on face crop; ~2 s stabilization; empathetic TTS + OLED eyes |
| **Voice** | Wake word on ESP; ElevenLabs or Whisper STT; Ollama (Qwen) replies; cross-platform TTS |
| **Memory** | PostgreSQL per-user conversation log; recap questions from recent history |
| **Alarms** | Voice-set reminders; medical (P0) with yes/no auto-listen; web UI ack/delete |
| **Servo** | Dynamixel AX head motion during TTS; ID2 full 360° via voice, HTTP, or CLI |
| **Touch** | QT2120 capacitive sensor — warning audio **preempts** server playback, then resumes |
| **Eyes** | Dual SSD1351 OLEDs — idle / listening / thinking / happy / sad / surprised / … |
| **Observability** | `GET /api/status`, `GET /api/latency-log`, `server/data/latency_log.json` |

---

## Hardware

| Component | Details |
| --------- | ------- |
| **Board** | ESP32-P4 Function EV Board (16 MB flash recommended) |
| **Camera** | USB UVC webcam on J18 host port (640×480 stream) |
| **Servos** | ROBOTIS Dynamixel AX — ID **1** tilt, ID **2** pan — via **U2D2** on J18 hub |
| **Touch** | QT2120 on I2C — **SDA GPIO 7**, **SCL GPIO 8**, address `0x1C` |
| **Eyes** | 2× Waveshare 1.27" SSD1351 OLED (128×96) — CLK 23, DIN 22, DC 21, RST 20, CS 26/27 |
| **Audio** | ES8311 codec (mic + speaker) |
| **Network** | ESP and PC on the same LAN |
| **LLM host** | PC with Ollama (`qwen2.5:1.5b` default) |

> U2D2 and the UVC camera share the J18 USB hub. Servo scan may log `ESP_ERR_INVALID_STATE` while the camera is enumerating — normal if U2D2 is unplugged.

---

## Requirements

### Firmware

- ESP-IDF **5.5+**
- Target: **esp32p4**

### Server

- Python **3.10+**
- **Windows** or **Linux** (Ubuntu / NVIDIA DGX tested)
- [Ollama](https://ollama.com) for voice + alarm NLP + empathy replies
- See [`server/requirements.txt`](server/requirements.txt) — includes `tensorflow`, `opencv-contrib-python`, `fastapi`, `faster-whisper`, `onnxruntime`, `psycopg2-binary`
- **PostgreSQL** (optional, for conversation memory)
- **TTS** — one of: ElevenLabs API key, Windows SAPI, or Linux espeak-ng

Full server documentation: **[server/README.md](server/README.md)**

---

## Quick start

### 1. Build and flash firmware

From the project root in an ESP-IDF shell:

```bash
idf.py set-target esp32p4
idf.py build
idf.py flash monitor
```

### 2. Connect Wi-Fi

**Serial console:**

```text
wifi mode sta
wifi connect <SSID> <PASSWORD>
wifi status
```

Or use BLE provisioning — see [docs/WIFI_PROVISION.md](docs/WIFI_PROVISION.md).

After Wi-Fi is connected, mDNS discovery is available:

- `NINO-HOME.local`
- service `_nino._tcp` on port `443`

### 3. Set up the Python server

```bash
cd server
python3 -m venv .venv
source .venv/bin/activate          # Windows: .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
cp .env.example .env               # edit DATABASE_URL, ESP URLs, API keys
```

**Optional — PostgreSQL memory (recommended):**

```bash
bash scripts/init_memory_db.sh
```

**Linux — start GPU Ollama (once per boot):**

```bash
bash scripts/start_ollama_gpu.sh
```

**Run the server** (replace `<ESP_IP>`):

```bash
python app.py --host 0.0.0.0 --port 8000 \
  --camera-source http://<ESP_IP>/stream \
  --esp-play-wav-url http://<ESP_IP>/play_wav \
  --database-url "postgresql://nino:nino@127.0.0.1:5432/nino_memory"
```

Open the web UI at **[http://localhost:8000](http://localhost:8000)** — register faces, watch the live feed with emotion overlay, and manage alarms.

### 4. Connect voice on the ESP console

```text
voice connect <PC_IP> 8000
voice wake on
```

Say **"Hi ESP"**, then ask a question. Eyes animate: **listening** → **thinking** → reply expression → **idle**.

---

## Vision emotion (camera empathy)

When a **registered person** stands in front of the camera without speaking, NiNO can detect their expression and respond empathetically.

**Flow:**

1. Face recognized (YuNet + SFace).
2. Keras CNN runs on a 48×48 grayscale face crop every frame.
3. Emotion votes accumulate for **2.0–2.5 seconds**.
4. Ollama generates a short empathetic line (*"You look a bit down today, Chakri"*).
5. TTS plays on the ESP with an eye tag (`happy`, `sad`, `surprised`, …).

**Speakable emotions:** happy, surprise, sad, anger, fear  
**Overlay only:** neutral, disgust

**Live feed:** `/video_feed` shows a pink emotion overlay (effective label, confidence, accumulation time).

**Model:** `server/data/models/emotion_model_best.h5` (7-class CNN, TensorFlow). Optional fallback: `EMOTION_BACKEND=ferplus` for ONNX FER+.

Full details: **[docs/EMOTION_RECOGNITION.md](docs/EMOTION_RECOGNITION.md)**

---

## PostgreSQL conversation memory

When `DATABASE_URL` is set, NiNO persists conversation history per recognized user.

### Database setup

```bash
cd server
bash scripts/init_memory_db.sh
```

Creates database `nino_memory`, user `nino`, and applies `scripts/memory_schema.sql`.

### Schema

| Table | Purpose |
| ----- | ------- |
| `users` | One row per recognized person (`face_id`, `name`, first/last seen) |
| `conversations` | Every logged Q&A (`user_text`, `assistant_text`, timestamp) |
| `memories` | Long-term facts (Phase B — requires `MEMORY_EXTRACTION=1`) |
| `summaries` | Daily rollups (Phase C — requires `MEMORY_SUMMARY_CRON=1`) |

### How it works

1. Face recognition identifies you (e.g. **Chakri** → `face_id: chakri`).
2. Before each voice reply, the server loads your last **10** conversations (configurable).
3. After a successful reply, the exchange is queued for insert into `conversations`.
4. Recap questions use the LLM with a filtered context window from recent turns.
5. Recap is live-face gated: without a currently recognized face, personal context is not retrieved.

### Check memory status

```bash
curl -s http://localhost:8000/api/status | python3 -m json.tool
```

Look for `"memory": { "ready": true, ... }`.

---

## Voice assistant

### Speech-to-text (STT)

| Provider | When used | Typical latency |
| -------- | --------- | --------------- |
| **ElevenLabs Scribe** | Default when `ELEVENLABS_API_KEY` is set | ~1–2 s |
| **faster-whisper** | Fallback or `--stt-provider whisper` | 6–30 s on CPU |

Force provider: `--stt-provider elevenlabs|whisper`

### LLM (Ollama)

- Default model: **`qwen2.5:1.5b`**
- Linux auto-prefers GPU Ollama on **`127.0.0.1:11435`** over CPU snap on `:11434`
- Override: `--ollama-url`, `--ollama-model`

### Text-to-speech (TTS)

| Provider | Platform | When used |
| -------- | -------- | --------- |
| `elevenlabs` | Any | Default when API key set |
| `sapi` | Windows | Local fallback |
| `local` | Linux | espeak-ng `en+f3` fallback |

### Voice routing

| Path | Trigger | Behavior |
| ---- | ------- | -------- |
| `alarm` | Set/list/cancel alarm phrases | Alarm voice handler |
| `servo_360` | "Make a 360", "spin 360", … | Fixed TTS → `POST /servo/360` |
| `recap` | "What did we talk about?", … | LLM recap from PostgreSQL |
| `identity_llm` | "Who am I?", "What's my name?", … | Ollama + live camera identity |
| `llm` | Everything else | Ollama with optional memory context |

---

## Face recognition

- **Detector:** YuNet (auto-download to `server/data/models/`)
- **Recognizer:** SFace 128-D embeddings (cosine similarity)
- **Storage:** `server/data/faces/*.jpg` + `server/data/face_embeddings.json`
- **Threshold:** `FACE_MATCH_THRESHOLD` (default **0.42** — higher = stricter)
- **Session hold:** primary viewer remembered ~90 s across brief gaps
- **Registration:** web UI at `http://localhost:8000` — samples encode instantly

---

## Alarms

- Voice parsing via regex + **Ollama NLP fallback** (`ALARM_NLP_FALLBACK=1`)
- Persists to `server/data/alarms.json`
- **Normal alarms:** TTS + beep → ESP `/play_wav`
- **Medical (P0):** TTS only; repeats every 3 min until confirmed; board auto-listens for yes/no
- Web UI: view, ack, delete at `http://localhost:8000`

Full details: **[docs/ALARM.md](docs/ALARM.md)**

---

## Firmware: touch, servo & eyes

### Touch-priority audio

Touch clips (`PDTM.wav`) **pause** in-progress server WAV, play the warning, then **resume** from the saved offset.

### Servo 360

- **ID2 (pan)** full rotation: 512 → 0 → 1023 → 512
- Triggers: serial `360`, `POST /servo/360`, voice via server

Details: **[docs/SERVO.md](docs/SERVO.md)**

### OLED eyes

| State | When |
| ----- | ---- |
| **Idle** | Boot, after reply finishes |
| **Listening** | Wake word through end of user speech |
| **Thinking** | Audio sent to server until reply received |
| **happy / sad / surprised** | Voice reply text or camera emotion empathy |

Serial test: `eye idle` / `eye listening` / `eye thinking`

### Key firmware files

| File | Role |
| ---- | ---- |
| `main/main.c` | UVC, Wi-Fi, HTTP server, voice console |
| `main/voice_assist.c` | VAD, medical ack listen |
| `main/voice_ws_client.c` | WebSocket to PC |
| `main/audio_queue.c` | Touch-priority dual queues |
| `main/servo_dxl.c` | Dynamixel + 360 spin |
| `main/nino_eye.c` | Eye animation engine |
| `main/ssd1351.c` | Dual OLED SPI driver |

---

## Server setup

See **[server/README.md](server/README.md)** for module map, configuration, tests, and emotion tuning.

### Configuration precedence

1. CLI flags (`python app.py --…`)
2. Environment variables / `server/.env`
3. `server/server_config.json` (optional — keep API keys out of git)

### Run example

```bash
cd server
source .venv/bin/activate
python app.py \
  --camera-source http://192.168.0.96/stream \
  --esp-play-wav-url http://192.168.0.96/play_wav \
  --database-url "$DATABASE_URL"
```

---

## HTTP & WebSocket API

### ESP firmware

| Method | Path | Description |
| ------ | ---- | ----------- |
| GET | `/stream` | MJPEG live stream |
| GET | `/snapshot.jpg` | Single JPEG frame |
| POST | `/play_wav` | Queue WAV playback (+ optional `X-Nino-Eye-Expression`, `X-Nino-Prompt-Ack`) |
| POST | `/servo/360` | ID2 full rotation |

### Python server

| Method | Path | Description |
| ------ | ---- | ----------- |
| GET | `/` | Web UI (faces, alarms) |
| GET | `/video_feed` | Annotated MJPEG with face boxes + emotion overlay |
| GET | `/api/status` | Camera, faces, emotion, vision emotion, TTS, LLM, alarms, memory |
| GET | `/api/latency-log?limit=50` | Recent voice timing events |
| GET | `/api/memory/stats` | PostgreSQL row counts |
| GET | `/api/alarms` | List alarms |
| POST | `/api/alarms/{id}/ack` | Medical yes/no ack |
| DELETE | `/api/alarms` | Cancel all |
| DELETE | `/api/alarms/{id}` | Cancel one |
| POST | `/api/register` | Register face samples |
| POST | `/api/retrain` | Re-encode stored face crops |
| POST | `/api/camera` | Change camera source |
| WS | `/voice-query`, `/ws/voice` | Voice assistant (16 kHz WAV in/out) |

> `POST /play_wav` and `POST /servo/360` on the ESP have no authentication — use on a trusted LAN only.

---

## Environment variables

### Core server

| Variable | Default | Purpose |
| -------- | ------- | ------- |
| `DATABASE_URL` | — | PostgreSQL URL for conversation memory |
| `ESP_PLAY_WAV_URL` | CLI / config | TTS, alarms, empathy; derives servo host |
| `OLLAMA_URL` | `auto` | Ollama API; `auto` prefers GPU `:11435` on Linux |
| `OLLAMA_MODEL` | `qwen2.5:1.5b` | LLM model name |
| `ELEVENLABS_API_KEY` | — | Cloud STT/TTS |
| `STT_PROVIDER` | auto | `elevenlabs` or `whisper` |
| `TTS_PROVIDER` | auto | `elevenlabs`, `sapi`, or `local` |

### Vision emotion

| Variable | Default | Purpose |
| -------- | ------- | ------- |
| `VISION_EMOTION_ENABLED` | `1` | Master switch for camera empathy |
| `EMOTION_BACKEND` | `keras` | `keras` (default) or `ferplus` (ONNX fallback) |
| `VISION_EMOTION_WINDOW_MIN_S` | `2.0` | Min seconds in frame before empathy |
| `VISION_EMOTION_COOLDOWN_S` | `120` | Seconds before same person gets empathy again |
| `VISION_EMOTION_AFTER_VOICE_SECONDS` | `90` | Pause empathy after voice query |
| `EMOTION_MIN_CONFIDENCE` | `0.12` | Min confidence for speakable class |

See [server/README.md](server/README.md) and [docs/EMOTION_RECOGNITION.md](docs/EMOTION_RECOGNITION.md) for the full list.

---

## Repository layout

```text
.
├── main/                    ESP-IDF firmware (camera, audio, eyes, servo, voice WS)
├── server/                  Python FastAPI server — see server/README.md
│   ├── app.py               HTTP/WS entry point
│   ├── emotion_service.py   Keras / FER+ inference
│   ├── vision_emotion_service.py  Emotion accumulation + empathy queue
│   ├── face_service.py      YuNet + SFace
│   ├── llm_service.py       Ollama voice + empathy prompts
│   ├── tts_service.py       TTS + ESP playback
│   ├── data/models/         emotion_model_best.h5, YuNet, SFace, …
│   └── scripts/             DB init, Ollama GPU helpers
├── emotion-trial/           Original training weights + loader (seed for data/models)
├── docs/                    Design docs, alarm/servo/wifi guides, emotion flow
├── tools/                   Build helpers
└── README.md                This file
```

---

## Troubleshooting

### Camera & faces

- Verify ESP stream: `http://<ESP_IP>/snapshot.jpg`
- Register varied samples (angles, lighting)
- Too many false accepts → raise `FACE_MATCH_THRESHOLD` (e.g. `0.45`)
- Too many rejects → lower it (e.g. `0.38`)

### Vision emotion

- Check `GET /api/status` → `"emotion": { "available": true, "backend": "keras" }`
- No empathy spoken → hold expression ~2 s; only speakable emotions trigger TTS
- Blocked after voice → wait `VISION_EMOTION_AFTER_VOICE_SECONDS` (default 90 s)
- Wrong labels → confirm training label order matches FER2013 (anger, disgust, fear, happy, sad, surprise, neutral)

### Voice

- ESP cannot reach PC → `voice connect <PC_LAN_IP> 8000` and confirm firewall
- Slow STT → set `ELEVENLABS_API_KEY` or use `--stt-provider whisper`
- Slow LLM → start GPU Ollama: `bash server/scripts/start_ollama_gpu.sh`

### Memory

- `"memory": { "ready": false }` → set valid `DATABASE_URL`; run `init_memory_db.sh`

### Hardware

- Touch fails → check QT2120 on I2C GPIO 7/8
- Eyes black → verify SSD1351 wiring; boot log should show `SSD1351 ready: 2 panel(s)`

---

## Related docs

| Document | Contents |
| -------- | -------- |
| [server/README.md](server/README.md) | Server modules, setup, tests, API details |
| [docs/EMOTION_RECOGNITION.md](docs/EMOTION_RECOGNITION.md) | Camera emotion pipeline, tuning, overlay |
| [docs/emotion-detect.md](docs/emotion-detect.md) | Original design vs implementation |
| [docs/ALARM.md](docs/ALARM.md) | Voice alarm commands, medical flow |
| [docs/SERVO.md](docs/SERVO.md) | Dynamixel wiring, 360 sequence |
| [docs/WIFI_PROVISION.md](docs/WIFI_PROVISION.md) | BLE / soft-AP Wi-Fi setup |
| [docs/Eye states.md](docs/Eye%20states.md) | OLED eye states and triggers |
| [docs/TEST_QUESTIONS.md](docs/TEST_QUESTIONS.md) | Suggested demo questions |

---

## Notes

- Touch audio **preempts** server/voice playback and resumes afterward.
- Generic face greetings are disabled when vision emotion is enabled — empathy replaces them.
- Voice WebSocket replies derive eye expression from **reply text**; camera empathy uses **CNN labels**.
- Keep `server/server_config.json`, `.env`, and API keys **out of public commits**.

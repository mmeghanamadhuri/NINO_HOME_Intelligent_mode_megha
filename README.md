# NiNO Home — ESP32-P4 Voice, Vision, Memory & Alarms

NiNO is a smart-home demo built around the **ESP32-P4 Function EV Board**. The board handles camera streaming, wake-word capture, speaker playback, touch, servo motion, and animated OLED eyes. A **Python FastAPI server** on a PC runs face recognition, speech-to-text, LLM replies, persistent conversation memory, alarms, and TTS — then sends audio back to the board.

---

## Table of contents

- [Architecture](#architecture)
- [Features](#features)
- [Hardware](#hardware)
- [Requirements](#requirements)
- [Quick start](#quick-start)
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
Wake word + mic ──► WS /voice-query ───────► STT → LLM → TTS → WAV reply
Speaker ◄──── POST /play_wav ◄───────────── greetings, alarms, voice replies
Servo ◄──── POST /servo/360 ◄────────────── voice-triggered 360° spin
Touch / eyes ── onboard only

                                            PostgreSQL (optional)
                                            users, conversations,
                                            memories, summaries
```

**Voice query flow**

1. User says **"Hi ESP"** on the board → VAD captures speech → WebSocket to PC.
2. Server transcribes audio (ElevenLabs Scribe or local Whisper).
3. Camera resolves who is speaking from **live face recognition**.
4. If memory is enabled and a live face is recognized, recent conversation history is loaded from PostgreSQL.
5. Request is routed: volume / alarm / servo / identity / recap / general LLM.
6. Reply is synthesized to 16 kHz WAV and streamed back over the WebSocket.
7. Exchange is logged to PostgreSQL (when memory is ready) and to `latency_log.json`.

---

## Features


| Area              | What NiNO does                                                                                        |
| ----------------- | ----------------------------------------------------------------------------------------------------- |
| **Vision**        | YuNet detection + SFace 128-D embeddings; web UI registration; personalized greetings via `/play_wav` |
| **Voice**         | Wake word on ESP; ElevenLabs or Whisper STT; Ollama (Qwen) replies; cross-platform TTS                |
| **Memory**        | PostgreSQL per-user conversation log; recap questions summarized by LLM from recent stored history    |
| **Alarms**        | Voice-set reminders; medical (P0) with yes/no auto-listen; web UI ack/delete                          |
| **Servo**         | Dynamixel AX head motion during TTS; ID2 full 360° via voice, HTTP, or CLI                            |
| **Touch**         | QT2120 capacitive sensor — warning audio **preempts** server playback, then resumes                   |
| **Eyes**          | Dual SSD1351 OLEDs — idle / listening / thinking synced to voice pipeline                             |
| **Observability** | `GET /api/status`, `GET /api/latency-log`, thread-safe `server/data/latency_log.json`                 |


---

## Hardware


| Component    | Details                                                                            |
| ------------ | ---------------------------------------------------------------------------------- |
| **Board**    | ESP32-P4 Function EV Board (16 MB flash recommended)                               |
| **Camera**   | USB UVC webcam on J18 host port (640×480 stream)                                   |
| **Servos**   | ROBOTIS Dynamixel AX — ID **1** tilt, ID **2** pan — via **U2D2** on J18 hub       |
| **Touch**    | QT2120 on I2C — **SDA GPIO 7**, **SCL GPIO 8**, address `0x1C`                     |
| **Eyes**     | 2× Waveshare 1.27" SSD1351 OLED (128×96) — CLK 23, DIN 22, DC 21, RST 20, CS 26/27 |
| **Audio**    | ES8311 codec (mic + speaker)                                                       |
| **Network**  | ESP and PC on the same LAN                                                         |
| **LLM host** | PC with Ollama (`qwen2.5:1.5b` default)                                            |


> U2D2 and the UVC camera share the J18 USB hub. Servo scan may log `ESP_ERR_INVALID_STATE` while the camera is enumerating — normal if U2D2 is unplugged.

---

## Requirements

### Firmware

- ESP-IDF **5.5+**
- Target: **esp32p4**

### Server

- Python **3.10+**
- **Windows** or **Linux** (Ubuntu / NVIDIA DGX tested)
- [Ollama](https://ollama.com) for voice + alarm NLP
- `opencv-contrib-python`, `fastapi`, `faster-whisper`, `psycopg2-binary` (see `server/requirements.txt`)
- **PostgreSQL** (optional, for conversation memory)
- **TTS** — one of: ElevenLabs API key, Windows SAPI, or Linux espeak-ng

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

### 3. Set up the Python server

```bash
cd server
python3 -m venv .venv
source .venv/bin/activate          # Windows: .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

**Optional — PostgreSQL memory (recommended):**

```bash
bash scripts/init_memory_db.sh
export DATABASE_URL="postgresql://nino:nino@127.0.0.1:5432/nino_memory"
```

**Linux — start GPU Ollama (once per boot):**

```bash
bash scripts/start_ollama_gpu.sh
```

**Run the server** (replace `<ESP_IP>` and `<PC_IP>`):

```bash
export DATABASE_URL="postgresql://nino:nino@127.0.0.1:5432/nino_memory"   # optional
export ELEVENLABS_API_KEY="sk_..."                                         # optional

python app.py --host 0.0.0.0 --port 8000 \
  --camera-source http://<ESP_IP>/stream \
  --esp-play-wav-url http://<ESP_IP>/play_wav
```

Open the web UI at **[http://localhost:8000](http://localhost:8000)** — register faces and manage alarms.

### 4. Connect voice on the ESP console

```text
voice connect <PC_IP> 8000
voice wake on
```

Say **"Hi ESP"**, then ask a question. Eyes animate automatically: **listening** → **thinking** → **idle**.

---

## PostgreSQL conversation memory

When `DATABASE_URL` is set, NiNO persists conversation history per recognized user in PostgreSQL.

### Database setup

```bash
cd server
bash scripts/init_memory_db.sh
```

Creates database `nino_memory`, user `nino`, and applies `scripts/memory_schema.sql`.

### Schema


| Table           | Purpose                                                            |
| --------------- | ------------------------------------------------------------------ |
| `users`         | One row per recognized person (`face_id`, `name`, first/last seen) |
| `conversations` | Every logged Q&A (`user_text`, `assistant_text`, timestamp)        |
| `memories`      | Long-term facts (Phase B — requires `MEMORY_EXTRACTION=1`)         |
| `summaries`     | Daily rollups (Phase C — requires `MEMORY_SUMMARY_CRON=1`)         |


### How it works

1. Face recognition identifies you (e.g. **Chakri** → `face_id: chakri`).
2. Before each voice reply, the server loads your last **10** conversations (configurable).
3. After a successful reply, the exchange is queued for insert into `conversations`.
4. Recap/context questions (*"What did we just talk about?"*, *"What are we discussing?"*, etc.) use the LLM with a filtered context window:
  - fetch last 10 turns,
  - remove recap/context meta-questions and STT fragments,
  - send up to the latest 5 meaningful turns to the recap prompt.
5. Recap is live-face gated: without a currently recognized face, personal context is not retrieved.

### View records in SQL

```bash
export DATABASE_URL="postgresql://nino:nino@127.0.0.1:5432/nino_memory"
psql "$DATABASE_URL"
```

```sql
SELECT * FROM users ORDER BY last_seen DESC;

SELECT u.name, c.timestamp, c.user_text, c.assistant_text
FROM conversations c
JOIN users u ON u.id = c.user_id
ORDER BY c.timestamp DESC
LIMIT 20;
```

### Check memory status

```bash
curl -s http://localhost:8000/api/status | python3 -m json.tool
```

Look for `"memory": { "ready": true, ... }`.

Voice latency entries include memory fields when enabled:


| Field                                 | Meaning                                                   |
| ------------------------------------- | --------------------------------------------------------- |
| `memory_store: "queued"`              | Conversation saved to PostgreSQL                          |
| `reply_path: "recap"`                 | Context recap generated by LLM from filtered recent turns |
| `reply_path: "recap_blocked_no_face"` | Context request blocked because no live recognized face   |
| `memory_store: "user_resolve_failed"` | Could not look up/create user row — not saved             |
| `memory_store: "skipped_fragment"`    | Incomplete STT fragment — intentionally not logged        |
| `memory_turns`                        | Number of recent turns loaded for context                 |


### Enable advanced memory (optional)

```bash
export MEMORY_EXTRACTION=1      # LLM extracts long-term facts after each turn
export MEMORY_SUMMARY_CRON=1    # Nightly per-user conversation summaries
export MEMORY_RECENT_TURNS=10   # Recent turns fetched before recap filtering (default 10)
export VOICE_RECAP_MAX_WORDS=55 # Max recap length for context responses
```

---

## Voice assistant

### Speech-to-text (STT)


| Provider              | When used                                | Typical latency |
| --------------------- | ---------------------------------------- | --------------- |
| **ElevenLabs Scribe** | Default when `ELEVENLABS_API_KEY` is set | ~1–2 s          |
| **faster-whisper**    | Fallback or `--stt-provider whisper`     | 6–30 s on CPU   |


Force provider: `--stt-provider elevenlabs|whisper`

### LLM (Ollama)

- Default model: `**qwen2.5:1.5b`**
- Linux auto-prefers GPU Ollama on `**127.0.0.1:11435**` over CPU snap on `:11434`
- Override: `--ollama-url`, `--ollama-model`

### Text-to-speech (TTS)


| Provider     | Platform | When used                  |
| ------------ | -------- | -------------------------- |
| `elevenlabs` | Any      | Default when API key set   |
| `sapi`       | Windows  | Local fallback             |
| `local`      | Linux    | espeak-ng `en+f3` fallback |


Check active provider: `GET /api/status` → `tts`.

### Voice routing

After STT, the server picks a reply path:


| Path           | Trigger                                                 | Behavior                                        |
| -------------- | ------------------------------------------------------- | ----------------------------------------------- |
| `alarm`        | Set/list/cancel alarm phrases                           | Alarm voice handler                             |
| `servo_360`    | "Make a 360", "spin 360", …                             | Fixed TTS → `POST /servo/360`                   |
| `recap`        | "What did we talk about?", "What are we discussing?", … | LLM recap from filtered recent PostgreSQL turns |
| `identity_llm` | "Who am I?", "What's my name?", …                       | Ollama + live camera identity                   |
| `llm`          | Everything else                                         | Ollama with optional memory context             |


**Personalization:** ~18% of general replies include the viewer's name (`VOICE_PERSONALIZE_PROB=0.18`). Vision greetings always use the name.

### Example voice commands


| Say                                   | Result                                      |
| ------------------------------------- | ------------------------------------------- |
| "Who am I?"                           | Answer using live face recognition          |
| "What did we just talk about?"        | Recap from PostgreSQL (second person)       |
| "Make a 360"                          | TTS confirmation → servo spin               |
| "Remind me to go to school at 8 AM"   | Normal alarm — TTS + beep at fire time      |
| "Remind me to take medicines at 6 AM" | Medical (P0) — TTS only; yes/no auto-listen |
| "List my alarms"                      | Hear pending alarms                         |
| General questions                     | STT → Ollama → TTS                          |


---

## Face recognition

- **Detector:** YuNet (auto-download to `server/data/models/`)
- **Recognizer:** SFace 128-D embeddings (cosine similarity)
- **Storage:** `server/data/faces/*.jpg` + `server/data/face_embeddings.json`
- **Threshold:** `FACE_MATCH_THRESHOLD` (default **0.42** — higher = stricter)
- **Session hold:** primary viewer remembered ~90 s across brief gaps
- **Registration:** web UI at `http://localhost:8000` — samples encode instantly (no slow retrain)

Tune if needed:

```bash
export FACE_MATCH_THRESHOLD=0.38   # looser matching
export FACE_SESSION_PRIMARY_HOLD_SECONDS=90
```

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

Touch clips (`PDTM.wav`) **pause** in-progress server WAV, play the warning, then **resume** from the saved offset. Separate queues for touch (8 jobs) and server/voice (32 jobs).

### Servo 360

- **ID2 (pan)** full rotation: 512 → 0 → 1023 → 512
- Triggers: serial `360`, `POST /servo/360`, voice via server

Details: **[docs/SERVO.md](docs/SERVO.md)**

### OLED eyes


| State         | When                                      |
| ------------- | ----------------------------------------- |
| **Idle**      | Boot, after reply finishes                |
| **Listening** | Wake word through end of user speech      |
| **Thinking**  | Audio sent to server until reply received |


Serial test: `eye idle` / `eye listening` / `eye thinking`

### Key firmware files


| File                     | Role                                   |
| ------------------------ | -------------------------------------- |
| `main/main.c`            | UVC, Wi-Fi, HTTP server, voice console |
| `main/voice_assist.c`    | VAD, medical ack listen                |
| `main/voice_ws_client.c` | WebSocket to PC                        |
| `main/audio_queue.c`     | Touch-priority dual queues             |
| `main/servo_dxl.c`       | Dynamixel + 360 spin                   |
| `main/nino_eye.c`        | Eye animation engine                   |
| `main/ssd1351.c`         | Dual OLED SPI driver                   |


---

## Server setup

### Configuration precedence

1. CLI flags (`python app.py --…`)
2. Environment variables
3. `server/server_config.json` (optional — keep API keys out of git)

### Run examples

**ESP camera + speaker:**

```bash
python app.py \
  --camera-source http://192.168.0.98/stream \
  --esp-play-wav-url http://192.168.0.98/play_wav \
  --database-url "$DATABASE_URL"
```

**Local webcam instead of ESP stream:**

```bash
python app.py --camera-source auto
```

**Force Whisper + CPU Ollama:**

```bash
python app.py --stt-provider whisper --ollama-url http://127.0.0.1:11434/api/generate
```

### Ollama on Linux (GPU)

The default snap Ollama on `:11434` is often CPU-only. This project supports a user-local CUDA build on **:11435**:

```bash
bash server/scripts/install_ollama_gpu_user.sh   # one-time
bash server/scripts/start_ollama_gpu.sh          # each boot
bash server/scripts/stop_ollama_gpu.sh
```

The server resolves `--ollama-url auto` to GPU first, warms the model on startup.

---

## HTTP & WebSocket API

### ESP firmware


| Method | Path            | Description                                                           |
| ------ | --------------- | --------------------------------------------------------------------- |
| GET    | `/stream`       | MJPEG live stream                                                     |
| GET    | `/snapshot.jpg` | Single JPEG frame                                                     |
| POST   | `/play_wav`     | Queue WAV playback (header `X-Nino-Prompt-Ack: 1` for medical listen) |
| POST   | `/servo/360`    | ID2 full rotation                                                     |


### Python server


| Method | Path                        | Description                                 |
| ------ | --------------------------- | ------------------------------------------- |
| GET    | `/`                         | Web UI (faces, alarms)                      |
| GET    | `/video_feed`               | Annotated MJPEG with face boxes             |
| GET    | `/api/status`               | Camera, faces, TTS, LLM, alarms, **memory** |
| GET    | `/api/latency-log?limit=50` | Recent voice timing events                  |
| GET    | `/api/alarms`               | List alarms                                 |
| POST   | `/api/alarms/{id}/ack`      | Medical yes/no ack                          |
| DELETE | `/api/alarms`               | Cancel all                                  |
| DELETE | `/api/alarms/{id}`          | Cancel one                                  |
| POST   | `/api/register`             | Register face samples                       |
| POST   | `/api/retrain`              | Re-encode stored face crops                 |
| POST   | `/api/camera`               | Change camera source                        |
| WS     | `/voice-query`, `/ws/voice` | Voice assistant (16 kHz WAV in/out)         |


> `POST /play_wav` and `POST /servo/360` on the ESP have no authentication — use on a trusted LAN only.

---

## Environment variables

### Core server


| Variable                 | Default        | Purpose                                          |
| ------------------------ | -------------- | ------------------------------------------------ |
| `DATABASE_URL`           | —              | PostgreSQL URL for conversation memory           |
| `ESP_PLAY_WAV_URL`       | CLI / config   | Face TTS, alarms, greetings; derives servo host  |
| `ESP_MAX_PLAY_WAV_BYTES` | `389120`       | Server-side WAV cap (ESP limit 384 KiB)          |
| `OLLAMA_URL`             | `auto`         | Ollama API; `auto` prefers GPU `:11435` on Linux |
| `OLLAMA_MODEL`           | `qwen2.5:1.5b` | LLM model name                                   |
| `ELEVENLABS_API_KEY`     | —              | Cloud STT/TTS                                    |
| `STT_PROVIDER`           | auto           | `elevenlabs` or `whisper`                        |
| `TTS_PROVIDER`           | auto           | `elevenlabs`, `sapi`, or `local`                 |
| `WHISPER_MODEL`          | `small`        | faster-whisper model size                        |


### Memory


| Variable                | Default | Purpose                                                      |
| ----------------------- | ------- | ------------------------------------------------------------ |
| `MEMORY_RECENT_TURNS`   | `10`    | Recent conversations fetched per user before recap filtering |
| `MEMORY_TOP_MEMORIES`   | `10`    | Long-term facts injected into prompt                         |
| `MEMORY_MIN_IMPORTANCE` | `5`     | Minimum importance score for memories                        |
| `MEMORY_EXTRACTION`     | `0`     | Enable LLM memory extraction after each turn                 |
| `MEMORY_SUMMARY_CRON`   | `0`     | Enable nightly per-user summaries                            |


### Face & voice


| Variable                            | Default | Purpose                                    |
| ----------------------------------- | ------- | ------------------------------------------ |
| `FACE_MATCH_THRESHOLD`              | `0.42`  | SFace cosine threshold (higher = stricter) |
| `FACE_SESSION_PRIMARY_HOLD_SECONDS` | `90`    | Remember primary viewer across gaps        |
| `VOICE_PERSONALIZE_PROB`            | `0.18`  | Fraction of replies using viewer name      |
| `VOICE_VIEWER_TTL_SECONDS`          | `900`   | Last recognized face TTL for voice         |
| `VOICE_RECAP_MAX_WORDS`             | `55`    | Recap/context max word budget              |


### Alarms


| Variable                       | Default            | Purpose                            |
| ------------------------------ | ------------------ | ---------------------------------- |
| `ALARM_NLP_FALLBACK`           | `1`                | Ollama JSON when regex parse fails |
| `ALARM_MEDICAL_REPEAT_MINUTES` | `3`                | Re-fire medical alarms until ack   |
| `ALARM_WAV_PATH`               | `../main/beep.wav` | Beep for normal alarms             |


See also [Text-to-speech tuning](#text-to-speech-tts) in the voice section and `docs/ALARM.md` for alarm-specific options.

---

## Repository layout

```text
├── main/                    ESP-IDF firmware
│   ├── main.c               UVC, HTTP, Wi-Fi, console
│   ├── voice_assist.c       VAD + voice sessions
│   ├── voice_ws_client.c    WebSocket to PC
│   ├── audio_queue.c        Touch-priority playback
│   ├── servo_dxl.c          Dynamixel + 360 spin
│   └── nino_eye.c           OLED eye animations
├── server/                  Python FastAPI server
│   ├── app.py               HTTP/WS entry point
│   ├── voice_service.py     STT → routing → TTS pipeline
│   ├── llm_service.py       Ollama prompts (voice, recap, identity)
│   ├── memory_service.py    PostgreSQL memory layer
│   ├── face_service.py      YuNet + SFace recognition
│   ├── tts_service.py       TTS + vision greetings
│   ├── alarm_*.py           Alarm scheduler + voice parsing
│   ├── scripts/
│   │   ├── init_memory_db.sh
│   │   ├── memory_schema.sql
│   │   ├── start_ollama_gpu.sh
│   │   └── install_ollama_gpu_user.sh
│   ├── data/
│   │   ├── faces/           Registered face crops
│   │   ├── face_embeddings.json
│   │   ├── alarms.json
│   │   └── latency_log.json
│   └── templates/           Web UI
├── docs/
│   ├── ALARM.md
│   ├── SERVO.md
│   ├── WIFI_PROVISION.md
│   └── TEST_QUESTIONS.md
├── context.md               Target architecture spec
└── context_main.md          Implemented vs target mapping
```

---

## Troubleshooting

### Camera & faces

- Verify ESP stream: `http://<ESP_IP>/snapshot.jpg`
- Register varied samples (angles, lighting); check log for `detector=yunet`
- Too many false accepts → raise `FACE_MATCH_THRESHOLD` (e.g. `0.45`)
- Too many rejects → lower it (e.g. `0.38`); stand closest to camera

### Voice

- ESP cannot reach PC → `voice connect <PC_LAN_IP> 8000` and confirm firewall
- Slow STT (6–30 s) → set `ELEVENLABS_API_KEY` or check key has **Speech to Text** permission
- Slow LLM → check `GET /api/status` → `llm.url`; start GPU Ollama on Linux
- Robotic voice on Linux → ElevenLabs failed; check logs or install `espeak-ng`

### Memory

- `"memory": { "ready": false }` → set valid `DATABASE_URL`; run `init_memory_db.sh`
- `memory_store: "user_resolve_failed"` → DB user lookup failed; check server logs and restart after code updates
- Recap empty → no prior conversations saved for that user; have a normal chat first
- Invalid `--database-url` → use `export DATABASE_URL="postgresql://..."` or `--database-url "$DATABASE_URL"` (not `$postgresql://…`)

### Alarms

- Alarms need `--esp-play-wav-url` set
- Medical ack needs `voice connect` + firmware with `X-Nino-Prompt-Ack`
- `WAV too large for ESP` → medical uses TTS only; normal alarms use 16 kHz resample

### Hardware

- Touch fails → check QT2120 on I2C GPIO 7/8
- Eyes black → verify SSD1351 wiring; boot log should show `SSD1351 ready: 2 panel(s)`
- 360 spin fails → U2D2 connected, `/servo/360` in firmware, `--esp-play-wav-url` set

---

## Related docs


| Document                                         | Contents                                      |
| ------------------------------------------------ | --------------------------------------------- |
| [docs/ALARM.md](docs/ALARM.md)                   | Voice alarm commands, medical flow, scheduler |
| [docs/SERVO.md](docs/SERVO.md)                   | Dynamixel wiring, 360 sequence, API           |
| [docs/WIFI_PROVISION.md](docs/WIFI_PROVISION.md) | BLE / soft-AP Wi-Fi setup                     |
| [docs/TEST_QUESTIONS.md](docs/TEST_QUESTIONS.md) | Suggested demo questions                      |
| [context.md](context.md)                         | Full target architecture                      |
| [context_main.md](context_main.md)               | Implemented vs planned features               |


---

## Notes

- Touch audio **preempts** server/voice playback and resumes afterward.
- Face greeting and alarm TTS use head motion during `/play_wav`.
- Voice WebSocket replies also use head motion unless interrupted by touch or `/servo/360`.
- Keep `server/server_config.json` and `.env` files with API keys **out of public commits**.
- Both OLED eyes mirror by default; per-eye drawing is available via `ssd1351_target()` for future expressions.


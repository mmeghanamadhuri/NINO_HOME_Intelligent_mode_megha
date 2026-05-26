# NiNO Home — ESP32-P4 Camera, Face Recognition & Voice Assistant

NiNO is a smart-home demo built around the **ESP32-P4 Function EV Board** with a **USB UVC camera**, **QT2120 touch sensor (12 keys on I2C)**, **on-board speaker (ES8311)**, and a **Python server** on your PC. The server performs face detection and recognition, speaks personalized greetings, and answers voice questions when you say **“Hi ESP”**. A touch on the sensor plays an embedded **“please don’t touch me”** warning on the board speaker.

---

## Table of contents

1. [System overview](#1-system-overview)
2. [Hardware](#2-hardware)
3. [Firmware (ESP32-P4)](#3-firmware-esp32-p4)
4. [Python server (PC)](#4-python-server-pc)
5. [End-to-end workflows](#5-end-to-end-workflows)
6. [Configuration reference](#6-configuration-reference)
7. [Troubleshooting](#7-troubleshooting)
8. [Security notes](#8-security-notes)
9. [Repository layout](#9-repository-layout)

---

## 1. System overview

```text
┌──────────────────────────────────────────────────────────────────────────┐
│  ESP32-P4 Function EV Board                                               │
│  • USB UVC camera (host port J18) → MJPEG frames in RAM                   │
│  • Wi‑Fi SoftAP and/or STA (ESP-Hosted / ESP32-C6 coprocessor)            │
│  • HTTP: /stream, /snapshot.jpg, POST /play_wav                           │
│  • ES8311 mic + speaker (BSP)                                             │
│  • Wake word “Hi ESP” → record question → WebSocket to PC                 │
│  • Plays WAV replies + wake/done two-tone beeps on speaker                │
│  • QT2120 touch (I2C 0x1C) → embedded “please don’t touch me” on speaker   │
└───────────────────────────────┬──────────────────────────────────────────┘
                                │  Same LAN as your PC
                                ▼
┌──────────────────────────────────────────────────────────────────────────┐
│  PC — Python server (FastAPI + OpenCV + Ollama + Whisper)                 │
│  • Pulls video: local webcam OR ESP http://<IP>/stream (snapshot poll)    │
│  • Face detect (Haar) + recognize (LBPH) → browser UI :8000               │
│  • Vision greetings (LLM) → TTS → POST WAV to ESP speaker                 │
│  • Voice: STT → LLM (uses recognized name) → TTS → WAV back to ESP         │
└──────────────────────────────────────────────────────────────────────────┘
```

**Typical daily use**

| What you open | Purpose |
|---------------|---------|
| `http://localhost:8000` | Face UI, registration, live annotated video |
| ESP `http://<board-ip>/` | Optional — camera appliance / snapshot check |
| Serial monitor `usb_cam>` | Wi‑Fi, voice URL, `cpu_dump` |

**Data paths**

- **Video:** `USB camera → UVC → frame buffer → HTTP → PC OpenCV`
- **Vision speech:** `Recognized face → Ollama greeting → SAPI WAV → POST /play_wav → ESP speaker`
- **Voice Q&A:** `“Hi ESP” → mic + VAD → WAV → WebSocket → Whisper → Ollama → WAV → ESP playback`

---

## 2. Hardware

| Item | Notes |
|------|--------|
| **Board** | ESP32-P4 Function EV Board |
| **Camera** | Standard UVC USB webcam on **J18 USB host** |
| **Audio** | On-board **ES8311** codec (mic + speaker via BSP) |
| **PC** | Windows recommended (SAPI TTS); Linux/macOS need TTS changes |
| **Network** | PC and ESP on the same Wi‑Fi / LAN |
| **LLM** | [Ollama](https://ollama.com/) on the PC (e.g. `qwen2.5:1.5b`) |

**Flash:** 16 MB recommended; WakeNet model partition for “Hi ESP”.

---

## 3. Firmware (ESP32-P4)

Built with **ESP-IDF 5.5+** (ESP32-P4 target). Root `CMakeLists.txt` sets `BSP_CONFIG_NO_GRAPHIC_LIB=1` so the BSP is used for audio and pins without pulling the full LVGL display stack.

### 3.1 Features

| Area | Description |
|------|-------------|
| **USB camera** | UVC host; prefers **MJPEG**, typically **640×480**, ~20 FPS |
| **HTTP server** | Port **80** — stream, snapshot, WAV playback |
| **Wi‑Fi** | Default SoftAP `ESP32_P4_CAM` / `12345678`; STA via console |
| **Speaker** | `POST /play_wav` — PCM 16-bit WAV (mono or stereo; stereo averaged) |
| **Wake word** | ESP-SR WakeNet **`wn9_hiesp`** (“Hi ESP”) |
| **Touch sensor** | **QT2120** on BSP I2C (addr **0x1C**, 12 keys); stable touch → **`PDTM.wav`** on speaker |
| **Voice pipeline** | Wake beep → VAD capture → WebSocket to PC → play reply + done beep |
| **Discovery** | UDP **1900** (`discover`), TCP **8888** (text log; not used for TTS) |
| **Console** | Prompt `usb_cam>` |

### 3.2 Build and flash

From an **ESP-IDF** shell at the project root:

```powershell
idf.py set-target esp32p4
idf.py build
idf.py flash monitor
```

On Windows you can use `build-idf.bat` if your environment sets `IDF_PATH` and runs `export.bat` first.

**Managed components** (`main/idf_component.yml`): `esp_hosted`, `esp_wifi_remote`, `usb`, `usb_host_uvc`, `esp32_p4_function_ev_board`.

### 3.3 Wi‑Fi

**SoftAP (default after flash)**

- SSID: `ESP32_P4_CAM`
- Password: `12345678`
- Browser: `http://192.168.4.1/`

**STA (home router)** — serial console:

```text
wifi mode sta
wifi connect YOUR_SSID YOUR_PASSWORD
wifi status
```

Use the **STA IP** in the Python server (`--camera-source`, `--esp-play-wav-url`).

Wi‑Fi uses the **ESP-Hosted** path (`esp_wifi_remote` + `esp_hosted`) for the on-board **ESP32-C6** coprocessor — not the legacy `esp-extconn` stack.

### 3.4 HTTP API (firmware)

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/` | Short HTML; single snapshot preview |
| GET | `/stream` | MJPEG multipart live stream |
| GET | `/snapshot.jpg` | One JPEG still |
| POST | `/play_wav` | Raw WAV body (max **384 KiB**). Queued FIFO with touch/voice; **never dropped** when busy (blocks until queued). Response `{"ok":true,"queued":true}` |

WAV format: PCM **16-bit**, **mono** preferred; **8–48 kHz** (server usually sends **16 kHz** for voice, **22.05 kHz** possible for some TTS paths).

### 3.5 Voice assistant (firmware)

**Console setup** (once per board / saved to NVS):

```text
voice connect <PC_LAN_IP> [port]
```

Default port **8000**. Saves `ws://<ip>:8000/voice-query` and enables wake.

| Command | Action |
|---------|--------|
| `voice connect <ip> [port]` | Set WebSocket URL to PC voice endpoint |
| `voice url [<ws-uri>]` | Show or set full WebSocket URI |
| `voice wake on` / `off` | Enable/disable “Hi ESP” |

**Runtime flow**

1. Say **“Hi ESP”** → ascending **wake beep** (700 Hz → 980 Hz).
2. Speak your question → firmware **VAD** records (16 kHz mono WAV).
3. WAV sent to PC **`/voice-query`** (or `/ws/voice`).
4. PC returns TTS WAV → ESP plays answer → descending **done beep** (1040 Hz → 760 Hz).

Wake starts after USB/camera settle (on connect or ~5 s delay) to avoid boot watchdog issues.

### 3.6 Firmware source map

| File | Role |
|------|------|
| `main/main.c` | UVC, Wi‑Fi, HTTP, `play_wav` queue, voice console, discovery |
| `main/audio_playback.c` | ES8311 speaker, WAV play, pipeline drain (full beeps) |
| `main/audio_capture.c` | Microphone for VAD |
| `main/voice_wake.cpp` | WakeNet feed/fetch, “Hi ESP” |
| `main/voice_assist.c` | Chimes, VAD capture, WebSocket exchange |
| `main/voice_ws_client.c` | WebSocket client to PC |
| `main/touch_sensor.c` | QT2120 poll task, touch → warning WAV |
| `main/bsp_qt2120.c` | QT2120 I2C driver |
| `main/PDTM.wav` | Embedded “please don’t touch me” clip |
| `partitions.csv` | Includes `srmodels` for wake word |

### 3.7 Useful console commands

```text
cpu_dump          # FreeRTOS task CPU usage
wifi status       # IP addresses
voice connect …   # Point to PC server
```

### 3.8 Firmware notes

- **UVC:** Always return frames with `uvc_host_frame_return()` or the stream underflows.
- **Partition:** 16 MB flash, large app partition; speech recognition model in `srmodels`.
- **Logs:** Occasional `Timed out waiting for a UVC frame` or `uvc-isoc` under load is common; stream usually recovers.

---

## 4. Python server (PC)

### 4.1 Stack

| Component | Use |
|-----------|-----|
| **FastAPI + Uvicorn** | HTTP API, Web UI, WebSockets |
| **OpenCV** (`opencv-contrib-python`) | Haar face detection + **LBPH** recognition |
| **faster-whisper** | Speech-to-text from ESP mic WAV |
| **Ollama** | Greetings, voice answers, error recovery lines |
| **Windows SAPI** | Text-to-speech (PowerShell `System.Speech`) |
| **requests** | POST synthesized WAV to ESP `play_wav` |

### 4.2 Prerequisites

1. **Python 3.10+**
2. **Ollama** running locally, e.g. `ollama pull qwen2.5:1.5b`
3. **opencv-contrib-python** (LBPH face recognizer)
4. **Windows** for default TTS (or adapt `tts_service.py` / `voice_service.py`)

### 4.3 Install and run

```powershell
cd server
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

**With ESP as camera + speaker** (replace IP with your board STA IP):

```powershell
python app.py --host 0.0.0.0 --port 8000 `
  --camera-source http://192.168.0.243/stream `
  --esp-play-wav-url http://192.168.0.243/play_wav
```

Open **`http://localhost:8000`**.

**Optional config file** `server/server_config.json`:

```json
{
  "camera_source": "http://192.168.0.243/stream",
  "esp_play_wav_url": "http://192.168.0.243/play_wav"
}
```

Then `python app.py` is enough if CLI args are omitted.

**Local webcam only** (no ESP):

```powershell
python app.py --camera-source auto
```

### 4.4 Camera transport (important)

If `--camera-source` ends with **`/stream`**, `camera.py` **does not** open a second MJPEG client. It polls **`http://<host>/snapshot.jpg`** instead so the ESP HTTP stack is not overloaded. Status shows `transport: http_snapshot`.

### 4.5 Web UI workflow

1. Confirm **Camera connected** and live preview.
2. **Register:** enter name → **Capture Samples and Train** (face centered, good light).
3. Green box + name when recognized; yellow **Unknown** if not trained or score too weak.
4. **Retrain** if you added samples under `server/data/faces/` manually.

### 4.6 Face recognition

| Setting | Default | Meaning |
|---------|---------|---------|
| `FACE_RECOGNITION_THRESHOLD` | `72` | LBPH distance; **lower = stricter match** |
| Samples | 15 (UI) | Cropped 200×200 grayscale faces per person |
| Storage | `server/data/faces/<id>/` | JPEG samples |
| Model | `server/data/face_model.yml` | Trained LBPH |
| Labels | `server/data/labels.json` | ID → display name |

Detection uses **CLAHE** + dual-pass Haar for soft ESP JPEGs. Only the **largest recognized face** in frame drives vision greetings and voice personalization (avoids background false greets).

### 4.7 Vision greetings (camera → speech)

When a **registered** person is the **primary** face in view:

- **First time this session:** LLM generates a short welcome (`greeting_for_face`).
- **Return after interval:** welcome-back (default **600 s**, `--face-greeting-interval`).

Speech is synthesized on the PC and sent to the ESP via **`ESP_PLAY_WAV_URL`**. After a **voice Q&A reply**, auto-greetings are **paused ~90 s** and stale greeting jobs are cleared so you do not hear the wrong name (e.g. Chakri in view but “Hello Khyati” from a queued job).

### 4.8 Voice assistant (PC side)

**WebSocket endpoints** (same pipeline):

- `ws://<PC>:8000/voice-query` ← ESP default after `voice connect`
- `ws://<PC>:8000/ws/voice`

**Pipeline per message**

1. Receive **WAV** from ESP (16 kHz mono).
2. **Whisper** → user text.
3. Resolve **viewer name** from current camera frame + session memory (largest recognized face).
4. **Ollama** → spoken answer; prompt requires using their **name on every reply**, including follow-ups.
5. **SAPI** → resample to **16 kHz** → send WAV bytes back to ESP.

**Personalization:** Not hardcoded strings — the LLM is told who the camera recognizes (e.g. Chakri) and writes natural lines like *“Hi Chakri, here is what you asked for …”*.

### 4.9 HTTP API (server)

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/` | Web UI |
| GET | `/video_feed` | Annotated MJPEG |
| GET | `/snapshot.jpg` | Annotated JPEG |
| GET | `/api/status` | Camera, faces, TTS, `latest_results` |
| POST | `/api/camera` | `{"source":"..."}` — change camera |
| POST | `/api/register` | `name`, `samples`, `interval_ms` |
| POST | `/api/retrain` | Retrain from `data/faces/` |
| WS | `/voice-query`, `/ws/voice` | Voice assistant binary WAV |

### 4.10 Server source map

| File | Role |
|------|------|
| `app.py` | Routes, MJPEG generator, voice WebSocket, CLI |
| `camera.py` | Local / HTTP snapshot transport |
| `face_service.py` | Detect, register, train, recognize, annotate |
| `tts_service.py` | Vision greetings queue, ESP WAV POST |
| `voice_service.py` | Whisper + voice WAV pipeline |
| `llm_service.py` | Ollama prompts (greeting, Q&A, errors) |
| `wav_resample.py` | Mono 16-bit WAV for ESP |
| `templates/`, `static/` | Web UI |

---

## 5. End-to-end workflows

### 5.1 First-time setup

1. Flash firmware; connect camera and power.
2. `wifi mode sta` + `wifi connect …` → note IP.
3. Start Ollama on PC.
4. Run `python app.py` with `--camera-source` and `--esp-play-wav-url`.
5. Register faces at `http://localhost:8000`.
6. On serial: `voice connect <PC_IP> 8000` and `voice wake on`.

### 5.2 Vision-only greeting

Sit in front of the camera → server recognizes you → LLM greeting plays on **ESP speaker** (if `esp_play_wav_url` set).

### 5.3 Voice question

1. Face recognized in UI (green box).
2. Say **“Hi ESP”** → wake beep.
3. Ask a question → wait for answer + done beep.
4. Follow-up: say **“Hi ESP”** again; name should stay in replies while you remain in view / session memory.

### 5.4 Two URLs (normal)

| URL | Role |
|-----|------|
| `http://localhost:8000` | Face server + UI (use daily) |
| `http://<ESP-ip>/` | Camera device (optional) |

---

## 6. Configuration reference

### 6.1 Environment variables (server)

| Variable | Meaning |
|----------|---------|
| `CAMERA_SOURCE` / `CAMERA_STREAM_URL` | Default camera URL |
| `ESP_PLAY_WAV_URL` | POST TTS WAV to ESP |
| `OLLAMA_URL` | Default `http://127.0.0.1:11434/api/generate` |
| `OLLAMA_MODEL` | e.g. `qwen2.5:1.5b` |
| `WHISPER_MODEL` | e.g. `tiny`, `base` |
| `WHISPER_LANGUAGE` | `en` or `auto` |
| `FACE_RECOGNITION_THRESHOLD` | LBPH threshold (default `72`) |
| `FACE_GREETING_INTERVAL_SECONDS` | Welcome-back interval (default `600`) |
| `VISION_GREETING_AFTER_VOICE_SECONDS` | Pause vision greet after voice (default `90`) |
| `VOICE_VIEWER_TTL_SECONDS` | Remember speaker for follow-ups (default `900`) |

### 6.2 CLI (`python app.py`)

| Flag | Purpose |
|------|---------|
| `--host` | Bind address (default `0.0.0.0`) |
| `--port` | Port (default `8000`) |
| `--camera-source` | `auto`, `0`, or `http://…/stream` |
| `--esp-play-wav-url` | ESP `http://<ip>/play_wav` |
| `--face-greeting-interval` | Seconds between welcome-back greets |

---

## 7. Troubleshooting

| Symptom | What to check |
|---------|----------------|
| No video on UI | ESP IP; open `http://<ip>/snapshot.jpg`; `/api/status` → `camera.last_error` |
| **Unknown (score)** | Retrain; more samples; lighting; lower threshold slightly via `FACE_RECOGNITION_THRESHOLD` |
| Register **500** / OpenCV error | Update `face_service.py` (safe Haar); face centered; restart server |
| No sound from ESP | Serial: speaker init; test `curl -X POST --data-binary @test.wav http://<ip>/play_wav` |
| TTS on PC not ESP | Set `--esp-play-wav-url` before speaking |
| Wake word no action | `voice connect`; `voice wake on`; model partition flashed; WS reachable from ESP |
| Truncated beeps | Reflash firmware with pipeline drain fix in `audio_playback.c` |
| Wrong name after voice | Reflash server: primary-face-only greetings + `notify_voice_interaction` |
| Voice without name on follow-up | Stay in frame; check logs for `Voice query viewer: <name>` |
| Ollama errors | `ollama serve`; model pulled; firewall |
| `movemodel` / build lock on Windows | Close OneDrive locks on `build/srmodels`; delete folder and rebuild |

---

## 8. Security notes

- **`POST /play_wav`** and the camera HTTP server have **no authentication** — use only on a **trusted LAN**.
- Do not port-forward to the internet without TLS and auth.
- Face images and models live in `server/data/` — treat as personal data.

---

## 9. Repository layout

```text
CMakeLists.txt              # ESP-IDF project (BSP without full LVGL)
build-idf.bat               # Windows build helper
partitions.csv              # App + srmodels partitions
sdkconfig.defaults          # IDF defaults

main/
  main.c                    # UVC, Wi‑Fi, HTTP, voice CLI
  audio_playback.c / .h     # Speaker, WAV, beeps
  audio_capture.c / .h      # Microphone
  voice_wake.cpp / .h       # “Hi ESP” WakeNet
  voice_assist.c / .h       # VAD, chimes, voice session
  voice_ws_client.c / .h    # WebSocket to PC
  touch_sensor.c / .h       # QT2120 touch → warning speech
  bsp_qt2120.c / .h         # QT2120 I2C driver
  PDTM.wav                  # Touch warning audio (embedded)
  idf_component.yml
  CMakeLists.txt

server/
  app.py                    # FastAPI application
  camera.py
  face_service.py
  tts_service.py
  voice_service.py
  llm_service.py
  wav_resample.py
  requirements.txt
  server_config.json        # Optional defaults
  templates/  static/
  data/                     # faces/, face_model.yml, labels.json
```

---

## Quick command cheat sheet

**Firmware**

```powershell
idf.py build flash monitor
```

```text
wifi connect MySSID MyPassword
voice connect 192.168.1.50 8000
voice wake on
```

**Server**

```powershell
cd server
python app.py --camera-source http://<ESP_IP>/stream --esp-play-wav-url http://<ESP_IP>/play_wav
```

**Browser:** `http://localhost:8000`

---

*NiNO Home — USB camera on the P4, face intelligence and voice on the PC, speech on the board speaker.*




## GitHub clone and update

Use these commands on a new system to download the project from GitHub:

```powershell
git clone https://github.com/ESP32-P4/voice-vision-server-setup-esp32p4.git
cd voice-vision-server-setup-esp32p4
```

If the project is already cloned and you just want the latest changes:

```powershell
git pull origin main
```

If GitHub SSH access is configured, you can clone with SSH instead:

```powershell
git clone git@github.com:ESP32-P4/voice-vision-server-setup-esp32p4.git
```

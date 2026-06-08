# NiNO Home — Voice, Vision, Touch & Servo  (ESP32-P4)

NiNO is a smart-home demo for the ESP32-P4 Function EV Board that uses vision, voice, touch, and servo motion together.

- **Vision**: USB UVC camera on the board, face detection/recognition on the PC, personalized greetings played through the ESP speaker.
- **Voice**: Wake-word capture on the board, Whisper speech recognition and Ollama LLM responses on the PC, audio returned to the board. Supports identity questions and servo voice commands.
- **Touch**: QT2120 capacitive touch sensor triggers an embedded warning audio clip with **priority over** server/voice playback.
- **Servo**: Dynamixel AX servos (IDs 1 & 2) via U2D2 on the J18 USB hub — head motion during TTS, **ID2 full 360° spin** via CLI, HTTP, or voice.

## Features

- ESP32-P4 firmware with UVC camera support, HTTP streaming, WAV playback, voice wake capture, touch handling, and Dynamixel servo control.
- Python FastAPI server for face UI, Whisper STT, Ollama LLM prompts, and TTS delivery to the board.
- **Dual-priority speaker queue** on the ESP: touch warnings preempt server/voice audio and resume playback afterward.
- **YuNet face detector** + LBPH recognition tuned for ~1 m range; vision greetings always personalized; general voice replies personalized ~18% of the time.
- **Voice identity** (“who am I?”) answered using live camera recognition context via Ollama.
- **Voice servo 360** (“make a 360”, “spin 360”) — fixed TTS confirmation, then `POST /servo/360` on the board (no LLM for this command).
- Single shared speaker path on the ESP to serialize touch, greetings, and voice replies.
- Recommended Windows server support for default SAPI text-to-speech.

## Recent changes

Summary of enhancements made in this integration branch:

### Touch-priority audio (firmware)

- Separate queues: **touch** (8 jobs) and **normal** server/voice (32 jobs).
- Touch clips (`PDTM.wav`, nod-L/R servo mode) **pause** an in-progress server WAV, play the warning, then **resume** from the saved PCM offset.
- Fixed mono touch WAV corruption (PCM buffer now owned after decode in `nino_audio_decode_wav()`).

### Face recognition (server)

- **YuNet** detector (auto-download to `server/data/models/`) with Haar fallback.
- LBPH with **per-person models**, **calibrated caps per person**, and a **1st vs 2nd margin** (blocks wrong-name swaps).
- **Green box** on a confident strict match (not only after many confirm frames). Yellow = close but not strict yet.
- Better **distance** detection (smaller faces, stronger upscale) and training augments (far + slight rotation).
- Face crop padding, upscale, bilateral filter, training augmentation.
- **Retrain** on the web UI after pulling recognition changes (required once).

### Voice assistant (server + firmware)

- **Identity questions** (“who am I?”, “what’s my name?”, …): Ollama reply grounded in live camera recognition (recognized name / unknown / no face).
- **Random personalization**: ~18% of general voice replies include the viewer’s name (`VOICE_PERSONALIZE_PROB` env override). Vision greetings always use the name.
- **Servo 360 voice command**: phrases like “make a 360”, “do a 360”, “spin 360” → fixed TTS (*“OK, doing the spin now.”*) → delayed `POST http://<ESP_IP>/servo/360`. Does **not** use Ollama.
- Voice reply playback uses **L/R/U/D head motion** during WebSocket TTS (same as `/play_wav`). `/servo/360` stops motion before the spin.

### Dynamixel servo 360 (firmware + server)

- **ID2 (pan)** full rotation: home to **512** if needed, then **512 → 0 → 1023 → 512**.
- Triggers: serial CLI `360`, **`POST /servo/360`**, voice via server.
- Present-position read over Dynamixel bus; background `servo_360` task.
- Details: **[docs/SERVO.md](docs/SERVO.md)**.

### Hardware note

- U2D2 and UVC camera share the **J18 USB hub**. Servo USB scan may log `ESP_ERR_INVALID_STATE` if the camera holds devices — normal when U2D2 is not connected or still enumerating.

## Requirements

### Firmware

- ESP-IDF 5.5 or later
- ESP32-P4 target
- 16 MB flash recommended

### Server

- Python 3.10+
- Windows recommended for SAPI TTS
- Ollama installed and running locally
- `opencv-contrib-python`

## Hardware

- **Board**: ESP32-P4 Function EV Board
- **Camera**: USB UVC webcam on J18 host port
- **Servos**: ROBOTIS Dynamixel AX (ID **1** = tilt, ID **2** = pan) via **U2D2** on the same J18 hub
- **Touch**: QT2120 capacitive sensor, I2C address `0x1C`
- **Audio**: ES8311 codec for microphone and speaker
- **Network**: PC and ESP on the same LAN
- **LLM**: Ollama model such as `qwen2.5:1.5b`

## Firmware overview

The ESP firmware provides:

- UVC host video capture
- HTTP endpoints for `/stream`, `/snapshot.jpg`, `/play_wav`, and **`/servo/360`**
- Wake-word support using ESP-SR WakeNet (“Hi ESP”)
- VAD-based voice capture and WebSocket transport to the PC
- QT2120 touch sensor warnings with **preemptive playback priority**
- Dynamixel joint-mode servo control (neutral **512**, position 0–1023)
- **ID2 full 360° spin** task (`nino_servo_dxl_spin_360`)
- Dual-queue speaker system with touch interrupt/resume in `main/audio_queue.c`

### Key firmware files

- `main/main.c` — UVC, Wi-Fi, HTTP server, voice console, **`360` CLI**, `/servo/360` handler
- `main/audio_queue.c` — touch-priority dual queues, suspend/resume server WAV
- `main/audio_playback.c` — ES8311 playback, WAV decode, interruptible partial play
- `main/audio_capture.c` — microphone capture
- `main/voice_wake.cpp` — wake word detection
- `main/voice_assist.c` — VAD and voice session management
- `main/voice_ws_client.c` — WebSocket client to PC
- `main/touch_sensor.c` — QT2120 capacitive touch handling
- `main/bsp_qt2120.c` — QT2120 I2C driver
- `main/servo_dxl.c` — Dynamixel USB host, read/write, **360 spin**
- `main/servo_dxl.h` — Dynamixel servo API
- `main/servo_motion.c` — cyclic head motion during face/touch TTS
- `main/servo_motion.h` — servo motion helpers
- `main/PDTM.wav` — embedded touch warning audio

### Servo documentation

See **[docs/SERVO.md](docs/SERVO.md)** for wiring, 360 sequence, voice trigger flow, CLI/HTTP API, and troubleshooting.

## Build and flash firmware

From the project root in an ESP-IDF shell:

```powershell
idf.py set-target esp32p4
idf.py build
idf.py flash monitor
```

On Windows, ensure `IDF_PATH` is set and the ESP-IDF environment is initialized first.

## Server setup

From the `server/` directory:

```powershell
cd server
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

### Run the server

Replace `<ESP_IP>` with the ESP board address:

```powershell
python app.py --host 0.0.0.0 --port 8000 \
  --camera-source http://<ESP_IP>/stream \
  --esp-play-wav-url http://<ESP_IP>/play_wav \
  --face-threshold 62 \
  --face-margin 10
```

To use a local webcam instead of the ESP stream:

```powershell
python app.py --camera-source auto
```

Open the UI at `http://localhost:8000`.

## Typical workflow

1. Flash the ESP firmware.
2. Power the board and connect the camera.
3. Configure Wi-Fi:

   - **Android app (recommended):** BLE GATT to device **PROV_NINO** (service `4facb001-5a2e-4b7c-9e1f-a8d3e6f20401`) — write SSID, password, then command `0x01`. HTTP fallback via soft AP `ESP32_P4_CAM`. See **[docs/WIFI_PROVISION.md](docs/WIFI_PROVISION.md)**.
   - **Serial console:**

```text
wifi mode sta
wifi connect <SSID> <PASSWORD>
wifi status
```

4. Start the Python server with the ESP camera and `play_wav` URL.
5. Visit `http://localhost:8000` to register faces and monitor the system.
6. On the ESP console, connect voice to the PC:

```text
voice connect <PC_IP> 8000
voice wake on
```

7. Say **"Hi ESP"** to trigger the voice assistant (general questions).

**Medical alarm ack on the board** (one-time serial setup):

```text
voice connect <PC_LAN_IP> 8000
voice wake on
```

After a **medical** reminder plays from the server, the board **automatically listens** for **5 s** for **yes** / **no** — you do **not** need to say “Hi ESP” for that step. Rebuild and flash firmware so `/play_wav` honors `X-Nino-Prompt-Ack`. You can also ack from the server web UI (**Yes** / **No** buttons).

Example voice commands after wake:

| Say | Behavior |
|-----|----------|
| “Who am I?” / “What’s my name?” | Ollama answer using live face recognition |
| “Make a 360” / “Spin 360” | Fixed TTS, then ID2 full rotation |
| “Set an alarm at 4:30 AM today” | Saves alarm; at fire time POSTs TTS + alarm WAV to ESP |
| “Remind me to take medicines at 6 AM” | Saves labeled reminder; fires with *“It's 6 AM, time for take medicines.”* + beep |
| “Remind me to take my medicine at 8 AM” | **P0 medical** — fires before other alarms at the same time; asks for yes/no ack; repeats every 3 min until confirmed |
| “Remind me to go to school at 8 AM” | Normal priority — one-shot alarm |
| After medical alarm: “yes” / “I took it” | Confirms and clears the reminder (auto-listen on board if `voice connect` is set) |
| After medical alarm: “no” | Asks to **reschedule** or **cancel** |
| Web UI **Yes** / **No** on awaiting row | Same ack without voice |
| “Reschedule for 6 PM” / “cancel it” | Follow-up after a negative ack |
| “List my alarms” | Hear pending alarms |
| “Cancel all alarms” / “Delete my alarms” | Remove every pending alarm |
| “Cancel alarm at 4 AM” / “Delete my coffee reminder” | Remove one matching alarm |
| General questions | Whisper → Ollama → TTS (name used ~18% of the time) |

Serial CLI servo test (U2D2 ready):

```text
360
```

## HTTP API

### Firmware endpoints

- `GET /` — simple device page and snapshot link
- `GET /stream` — MJPEG live stream
- `GET /snapshot.jpg` — one JPEG snapshot
- `POST /play_wav` — queue WAV audio for playback
- `POST /servo/360` — start ID2 full rotation (512 → 0 → 1023 → 512)

### Server endpoints

- `GET /` — web UI
- `GET /video_feed` — annotated MJPEG stream
- `POST /api/camera` — change camera source
- `POST /api/register` — register face data
- `POST /api/retrain` — retrain face recognition model
- `GET /api/alarms` — list pending alarms
- `DELETE /api/alarms` — delete all pending alarms
- `DELETE /api/alarms/{id}` — delete one alarm (web UI **Delete** button per row)
- `WS /voice-query` — voice assistant WebSocket (also `/ws/voice`)

### Server environment (optional)

| Variable | Default | Purpose |
|----------|---------|---------|
| `ESP_PLAY_WAV_URL` | CLI / config | Face TTS + derives `/servo/360` host for voice spin |
| `VOICE_PERSONALIZE_PROB` | `0.18` | Fraction of voice replies that use viewer name |
| `SERVO_360_TRIGGER_DELAY_SECONDS` | `2.0` | Delay after 360 confirmation TTS before POST spin |
| `VOICE_VIEWER_TTL_SECONDS` | `900` | How long last recognized face is remembered for voice |
| `ALARM_WAV_PATH` | `../main/beep.wav` | WAV POSTed to ESP when an alarm fires (after spoken alert) |
| `ALARM_TICK_SECONDS` | `1.0` | Scheduler poll interval for due alarms |
| `ALARM_MEDICAL_REPEAT_MINUTES` | `3` | Re-fire medical alarms until user confirms |
| `ALARM_NLP_FALLBACK` | `1` | Use Ollama JSON when regex fails (`0` to disable) |

## Repository layout

```text
CMakeLists.txt
build-idf.bat
partitions.csv
sdkconfig.defaults
sdkconfig.defaults.esp32p4
sdkconfig.old

main/
  CMakeLists.txt
  idf_component.yml
  main.c
  audio_capture.c
  audio_playback.c
  audio_queue.c
  bsp_qt2120.c
  touch_sensor.c
  voice_assist.c
  voice_ws_client.c
  voice_wake.cpp
  servo_dxl.c
  servo_motion.c
  PDTM.wav

docs/
  SERVO.md

server/
  app.py
  alarm_service.py
  alarm_voice.py
  esp_playback.py
  camera.py
  face_service.py
  llm_service.py
  tts_service.py
  voice_service.py
  wav_resample.py
  data/alarms.json
  requirements.txt
  server_config.json
  templates/
  static/
  data/

managed_components/
  ...
```

## Notes

- Touch audio **preempts** server/voice playback and resumes afterward; server/voice uses a separate queue from touch.
- Voice WebSocket replies play **without** head motion so servo 360 is not blocked.
- Face greeting TTS from the server still uses head motion during `/play_wav`.
- `POST /play_wav` and `POST /servo/360` have no authentication — use only on a trusted LAN.
- Windows is the recommended platform for the default speech synthesis path.
- **`server/data/face_model.yml`** can grow very large after retraining; do not commit files over GitHub’s 100 MB limit — retrain locally on each machine or share the model out of band.

## Troubleshooting

- If video is missing, verify `http://<ESP_IP>/snapshot.jpg`.
- If faces are not recognized, retrain with more samples and improve lighting; check server log for YuNet/LBPH detector.
- If voice does not connect, verify the PC IP is reachable from the ESP (`voice connect <PC_IP> 8000`) and the server is running.
- If touch audio fails, check QT2120 initialization and speaker setup in the serial logs.
- If **360 spin** does not run from voice, confirm `--esp-play-wav-url http://<ESP_IP>/play_wav` is set, firmware includes `/servo/360`, and U2D2/servos show ready in logs.
- If **`git push` fails** on `face_model.yml`, exclude it from commits (see `.gitignore`); the trained model is machine-local.
- USB hub: U2D2 scan errors alongside UVC camera on J18 are common when the Dynamixel adapter is unplugged or still enumerating.

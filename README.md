# NiNO Home — Voice, Vision, Touch, Alarms, Servo & Eyes  (ESP32-P4)

NiNO is a smart-home demo for the ESP32-P4 Function EV Board that uses vision, voice, alarms, touch, servo motion, and animated OLED eyes together.

- **Vision**: USB UVC camera on the board, face detection/recognition on the PC, personalized greetings played through the ESP speaker.
- **Voice**: Wake-word capture on the board, ElevenLabs Scribe (cloud) or Whisper (local) speech recognition and Ollama LLM responses on the PC, audio returned to the board. Supports identity questions and servo voice commands.
- **Alarms**: Voice-set reminders and alarms on the PC; medical (P0) reminders with yes/no ack, auto-repeat, and reschedule/cancel follow-up — spoken TTS fired to the ESP at the scheduled time.
- **Touch**: QT2120 capacitive touch sensor triggers an embedded warning audio clip with **priority over** server/voice playback.
- **Servo**: Dynamixel AX servos (IDs 1 & 2) via U2D2 on the J18 USB hub — head motion during TTS, **ID2 full 360° spin** via CLI, HTTP, or voice.
- **Eyes**: Dual SSD1351 OLED displays animate NINO's eyes — **idle**, **listening**, and **thinking** expressions follow the voice-assistant state automatically (wake word → listening, query sent → thinking, reply received → idle).

## Features

- ESP32-P4 firmware with UVC camera support, HTTP streaming, WAV playback, voice wake capture, touch handling, **medical alarm auto-listen**, and Dynamixel servo control.
- Python FastAPI server for face UI, **ElevenLabs/Whisper STT**, Ollama LLM prompts, **alarm scheduler**, and TTS delivery to the board.
- **Per-query latency log** (`server/data/latency_log.json`): STT engine + STT/LLM/TTS/total seconds for every voice query.
- **Voice & NLP alarms**: set/list/cancel reminders by voice (“remind me to go to school at 8 AM”); regex parsing with **Ollama fallback** for natural phrasing; persists to `server/data/alarms.json`.
- **Medical (P0) reminders**: medication labels get priority, spoken TTS on the ESP, **yes/no auto-listen** (no wake word), repeat every 3 min until confirmed; **reschedule or cancel** follow-up with mic re-open.
- **Alarm web UI**: view pending and awaiting-ack alarms; **Yes** / **No** / **Delete** per row at `http://localhost:8000`.
- **Dual-priority speaker queue** on the ESP: touch warnings preempt server/voice audio and resume playback afterward.
- **YuNet face detector** + **SFace deep-embedding recognition** (128-D vectors, cosine similarity); vision greetings always personalized; general voice replies personalized ~18% of the time.
- **Voice identity** (“who am I?”) answered using live camera recognition context via Ollama.
- **Voice servo 360** (“make a 360”, “spin 360”) — fixed TTS confirmation, then `POST /servo/360` on the board (no LLM for this command).
- Single shared speaker path on the ESP to serialize touch, greetings, alarms, and voice replies.
- **NINO eye displays**: two mirrored 1.27" SSD1351 OLEDs (128×96) on one SPI bus render flicker-free eye animations; expressions are driven by firmware events and testable from the serial console (`eye <idle|listening|thinking>`).
- Recommended Windows server support for default SAPI text-to-speech.

## Recent changes

Summary of enhancements made in this integration branch:

### Touch-priority audio (firmware)

- Separate queues: **touch** (8 jobs) and **normal** server/voice (32 jobs).
- Touch clips (`PDTM.wav`, nod-L/R servo mode) **pause** an in-progress server WAV, play the warning, then **resume** from the saved PCM offset.
- Fixed mono touch WAV corruption (PCM buffer now owned after decode in `nino_audio_decode_wav()`).

### Face recognition (server)

- **Migrated from LBPH to SFace deep embeddings**: each registered sample is encoded once into a 128-D vector (`face_recognition_sface_2021dec.onnx`, auto-download to `server/data/models/`); live faces are matched by **cosine similarity** against the store. No LBPH training, augmentation, or per-person thresholds.
- **YuNet** detector (auto-download to `server/data/models/`) with Haar fallback.
- Single tunable acceptance threshold: `FACE_MATCH_THRESHOLD` (cosine, default **0.36**; higher = stricter).
- Embeddings persist in **`server/data/face_embeddings.json`** — small and portable (no more 100 MB `face_model.yml`).
- **Register encodes instantly**: new samples are matchable immediately, no retrain needed. **Retrain** on the web UI just re-encodes stored crops (fast — useful after the LBPH → SFace migration, which also runs automatically on first start).
- **Primary viewer only**: smaller background faces cannot steal a match (closest/largest face wins).
- **Session memory** (~90 s): faster re-confirm when you walk away and return; stabilizes voice/TTS identity.
- **Multi-frame “who am I?”** vote (5 frames) instead of a single snapshot.
- Firmware UVC stream bumped from 480×320 to **640×480** for better detection/recognition range.

### Voice assistant (server + firmware)

- **ElevenLabs Scribe STT** (cloud, default `scribe_v1`): used automatically when an API key is set via `ELEVENLABS_API_KEY`, `--elevenlabs-api-key`, or `elevenlabs_api_key` in `server/server_config.json` (precedence: CLI > env > config file); typically ~1–2 s vs 6–30 s for local Whisper on CPU. Falls back to Whisper automatically if the API call fails, so the assistant keeps working offline.
- **Whisper** STT via `faster-whisper` (default model **`small`**; override with `--whisper-model`) — local fallback or forced with `--stt-provider whisper`.
- **Latency logging**: every voice query appends a record to `server/data/latency_log.json` (heard text, reply path, STT engine, `stt_seconds`, `reply_seconds`, `tts_seconds`, totals).
- **Identity questions** (“who am I?”, “what’s my name?”, …): Ollama reply grounded in live camera recognition (recognized name / unknown / no face).
- **Random personalization**: ~18% of general voice replies include the viewer’s name (`VOICE_PERSONALIZE_PROB` env override). Vision greetings always use the name.
- **Servo 360 voice command**: phrases like “make a 360”, “do a 360”, “spin 360” → fixed TTS (*“OK, doing the spin now.”*) → delayed `POST http://<ESP_IP>/servo/360`. Does **not** use Ollama.
- Voice reply playback uses **L/R/U/D head motion** during WebSocket TTS (same as `/play_wav`). `/servo/360` stops motion before the spin.
- **Medical alarm follow-up listen**: after you say **no** to a medical reminder, the server asks *reschedule or cancel?* and the board **opens the mic again** automatically (WebSocket `prompt_medical_ack` metadata + firmware re-listen).

### Alarms (server + firmware)

- Regex alarm parsing first; **Ollama NLP fallback** when phrasing is non-standard (`ALARM_NLP_FALLBACK=1`).
- Normalizes Whisper/Ollama time quirks (e.g. `8.36pm`, `20:36 PM` → valid 12-hour parse).
- **Medical (P0)** reminders: TTS on ESP with **yes/no auto-listen** (`X-Nino-Prompt-Ack` on `/play_wav`); repeat every 3 min until confirmed.
- Alarm TTS resampled to **16 kHz** with faster-speech fallback so WAV fits ESP **`/play_wav` limit (384 KiB)**.
- Medical fires **TTS only** (no beep clip — avoids exceeding size limit). Normal alarms: TTS + beep.
- Details: **[docs/ALARM.md](docs/ALARM.md)**.

### Dynamixel servo 360 (firmware + server)

- **ID2 (pan)** full rotation: home to **512** if needed, then **512 → 0 → 1023 → 512**.
- Triggers: serial CLI `360`, **`POST /servo/360`**, voice via server.
- Present-position read over Dynamixel bus; background `servo_360` task.
- Details: **[docs/SERVO.md](docs/SERVO.md)**.

### NINO eye displays (firmware)

- **Dual SSD1351 OLED driver** (`main/ssd1351.c`): two 1.27" 128×96 panels share one SPI bus (SPI2, 20 MHz); only CS differs per panel, so both eyes render mirrored by default (`ssd1351_target()` can address one eye).
- **Eye animation engine** (`main/nino_eye.c`): dedicated FreeRTOS task; state switches are instant and non-blocking from any task via `nino_eye_<emotion>()`.
- **Expressions integrated so far** (taken from the standalone display project):
  - **Idle** — neutral black eye on white, slow ~5 s eyelid blink (boot default).
  - **Listening** — wider/taller eye, snappier ~3 s blink.
  - **Thinking** — eye slowly rolls around the top (up / up-left / up-right), no blink.
- **Voice pipeline hooks**: wake word accepted → **listening** (through chime, VAD capture, and upload); WAV sent to server → **thinking**; reply WAV received (or any failure) → **idle**. Eyes can never stick in a state — every error path falls back to idle.
- **Flicker-free rendering**: the previous shape is "un-drawn" along its own outline instead of erasing rectangles, so the static white background is never re-touched (no full-screen flash on state changes).
- Serial test command on the existing console: `eye <idle|listening|thinking>`.

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
- Ollama installed and running locally (voice replies + alarm NLP fallback)
- `opencv-contrib-python`

## Hardware

- **Board**: ESP32-P4 Function EV Board
- **Camera**: USB UVC webcam on J18 host port
- **Servos**: ROBOTIS Dynamixel AX (ID **1** = tilt, ID **2** = pan) via **U2D2** on the same J18 hub
- **Touch**: QT2120 capacitive sensor on shared **I2C** bus (**SDA GPIO 7**, **SCL GPIO 8**), address `0x1C`
- **Eyes**: 2× Waveshare 1.27" RGB OLED (SSD1351, 128×96, 4-wire SPI) on the J1 header — shared **CLK GPIO 23**, **DIN GPIO 22**, **DC GPIO 21**, **RST GPIO 20**; per-panel **CS GPIO 26** (left) / **GPIO 27** (right); 3.3 V + GND
- **Audio**: ES8311 codec for microphone and speaker
- **Network**: PC and ESP on the same LAN
- **LLM**: Ollama model such as `qwen2.5:1.5b`

## Firmware overview

The ESP firmware provides:

- UVC host video capture
- HTTP endpoints for `/stream`, `/snapshot.jpg`, `/play_wav`, and **`/servo/360`**
- Wake-word support using ESP-SR WakeNet (“Hi ESP”)
- VAD-based voice capture and WebSocket transport to the PC
- **Medical alarm ack**: after `/play_wav` with `X-Nino-Prompt-Ack`, auto-listen for yes/no; WebSocket `prompt_medical_ack` for reschedule/cancel follow-up
- QT2120 touch sensor warnings with **preemptive playback priority**
- Dynamixel joint-mode servo control (neutral **512**, position 0–1023)
- **ID2 full 360° spin** task (`nino_servo_dxl_spin_360`)
- Dual-queue speaker system with touch interrupt/resume in `main/audio_queue.c`
- **Animated OLED eyes** synced to the voice assistant (idle / listening / thinking), with a `eye` console command for manual testing

### Key firmware files

- `main/main.c` — UVC, Wi-Fi, HTTP server, voice console, **`360` CLI**, `/servo/360` handler
- `main/audio_queue.c` — touch-priority dual queues, suspend/resume server WAV
- `main/audio_playback.c` — ES8311 playback, WAV decode, interruptible partial play
- `main/audio_capture.c` — microphone capture
- `main/voice_wake.cpp` — wake word detection
- `main/voice_assist.c` — VAD, voice session, **medical ack listen** (`nino_voice_assist_prompt_medical_ack`)
- `main/voice_ws_client.c` — WebSocket client to PC (parses `prompt_medical_ack` metadata)
- `main/touch_sensor.c` — QT2120 capacitive touch handling
- `main/bsp_qt2120.c` — QT2120 I2C driver
- `main/servo_dxl.c` — Dynamixel USB host, read/write, **360 spin**
- `main/servo_dxl.h` — Dynamixel servo API
- `main/servo_motion.c` — cyclic head motion during face/touch TTS
- `main/servo_motion.h` — servo motion helpers
- `main/nino_eye.c` — eye animation engine (idle/listening/thinking states, blink renderer)
- `main/ssd1351.c` — dual SSD1351 OLED SPI driver (mirrored eyes, per-panel targeting)
- `main/PDTM.wav` — embedded touch warning audio

### Servo documentation

See **[docs/SERVO.md](docs/SERVO.md)** for wiring, 360 sequence, voice trigger flow, CLI/HTTP API, and troubleshooting.

### Alarm documentation

See **[docs/ALARM.md](docs/ALARM.md)** for voice commands, medical ack flow, NLP time parsing, scheduler, and troubleshooting.

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
python app.py --host 0.0.0.0 --port 8000 `
  --camera-source http://<ESP_IP>/stream `
  --esp-play-wav-url http://<ESP_IP>/play_wav `
  --whisper-model small
```

Face matching uses a cosine-similarity threshold (default **0.36**); tune with `FACE_MATCH_THRESHOLD` if needed (higher = stricter). Legacy LBPH-style `--face-threshold` values above 1 are ignored.

For fast cloud STT, set an ElevenLabs API key (with the **Speech to Text** permission) before starting the server — it is picked up automatically:

```powershell
setx ELEVENLABS_API_KEY "sk_your_key_here"   # then open a new terminal
```

or pass `--elevenlabs-api-key sk_...` on the command line. Force a specific engine with `--stt-provider elevenlabs` or `--stt-provider whisper`.

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

4. Start the Python server with the ESP camera and `play_wav` URL (required for face TTS, **alarms**, and vision greetings).
5. Visit `http://localhost:8000` to register faces, **view/manage alarms**, and monitor the system.
6. On the ESP console, connect voice to the PC:

```text
voice connect <PC_IP> 8000
voice wake on
```

7. Say **"Hi ESP"** to trigger the voice assistant (general questions).

   The OLED eyes follow along automatically: **listening** (wide eye) from the wake word through your question, **thinking** (eye rolls upward) while the PC transcribes and generates the answer, and back to **idle** as the reply plays. You can also drive them manually from the serial console:

```text
eye listening
eye thinking
eye idle
```

**Medical alarm ack on the board** (one-time serial setup):

```text
voice connect <PC_LAN_IP> 8000
voice wake on
```

After a **medical** reminder plays from the server, the board **automatically listens** for **8 s** for **yes** / **no** — you do **not** need to say “Hi ESP” for that step. Flash firmware that supports `/play_wav` **`X-Nino-Prompt-Ack`** and WebSocket **`prompt_medical_ack`** follow-up listens. You can also ack from the server web UI (**Yes** / **No** buttons).

**Medical flow after “no”:**

1. Board asks *reschedule or cancel?* (spoken by server TTS).
2. Mic opens again automatically — say **“cancel”** or **“reschedule for 6 PM”** (no wake word).
3. Repeat listens continue while the alarm is in `reschedule_prompt` state.

Example voice commands after wake:

| Say | Behavior |
|-----|----------|
| “Who am I?” / “What’s my name?” | Ollama answer using live face recognition |
| “Make a 360” / “Spin 360” | Fixed TTS, then ID2 full rotation |
| “Set an alarm at 4:30 AM today” | Saves alarm; at fire time POSTs TTS + beep to ESP |
| “Remind me to take medicines at 6 AM” | **P0 medical** — spoken TTS on ESP; yes/no auto-listen; repeats every 3 min (no beep) |
| “Remind me to take my medicine at 8 AM” | Same as medical — priority over normal alarms at the same time |
| “Remind me to go to school at 8 AM” | Normal priority — spoken TTS + beep; one-shot |
| After medical alarm: “yes” / “I took it” | Confirms and clears the reminder (auto-listen on board if `voice connect` is set) |
| After medical alarm: “no” / “not taken” | Asks to **reschedule** or **cancel**; mic re-opens automatically |
| Web UI **Yes** / **No** on awaiting row | Same ack without voice |
| “Reschedule for 6 PM” / “cancel it” | Follow-up after negative ack (mic already listening) |
| “List my alarms” | Hear pending alarms |
| “Cancel all alarms” / “Delete my alarms” | Remove every pending alarm |
| “Cancel alarm at 4 AM” / “Delete my coffee reminder” | Remove one matching alarm |
| General questions | STT (ElevenLabs/Whisper) → Ollama → TTS (name used ~18% of the time) |

Serial CLI servo test (U2D2 ready):

```text
360
```

## HTTP API

### Firmware endpoints

- `GET /` — simple device page and snapshot link
- `GET /stream` — MJPEG live stream
- `GET /snapshot.jpg` — one JPEG snapshot
- `POST /play_wav` — queue WAV audio for playback (optional header `X-Nino-Prompt-Ack: 1` for medical yes/no listen after play)
- `POST /servo/360` — start ID2 full rotation (512 → 0 → 1023 → 512)

### Server endpoints

- `GET /` — web UI
- `GET /video_feed` — annotated MJPEG stream
- `POST /api/camera` — change camera source
- `POST /api/register` — register face data
- `POST /api/retrain` — retrain face recognition model
- `GET /api/alarms` — list pending alarms
- `POST /api/alarms/{id}/ack` — confirm or decline a medical alarm awaiting ack (same as voice yes/no)
- `DELETE /api/alarms` — delete all pending alarms
- `DELETE /api/alarms/{id}` — delete one alarm (web UI **Delete** button per row)
- `WS /voice-query` — voice assistant WebSocket (also `/ws/voice`)

### Server environment (optional)

| Variable | Default | Purpose |
|----------|---------|---------|
| `ESP_PLAY_WAV_URL` | CLI / config | Face TTS + derives `/servo/360` host for voice spin |
| `ESP_MAX_PLAY_WAV_BYTES` | `389120` | Server-side cap (ESP `/play_wav` hard limit is 384 KiB) |
| `STT_PROVIDER` | auto | `elevenlabs` or `whisper` (`--stt-provider`); defaults to ElevenLabs when an API key is set |
| `ELEVENLABS_API_KEY` | — | ElevenLabs API key for cloud STT (`--elevenlabs-api-key`); needs the **Speech to Text** permission |
| `ELEVENLABS_STT_MODEL` | `scribe_v1` | ElevenLabs Scribe model id |
| `WHISPER_MODEL` | `small` | faster-whisper model (`--whisper-model`); local fallback engine |
| `VOICE_PERSONALIZE_PROB` | `0.18` | Fraction of voice replies that use viewer name |
| `SERVO_360_TRIGGER_DELAY_SECONDS` | `2.0` | Delay after 360 confirmation TTS before POST spin |
| `VOICE_VIEWER_TTL_SECONDS` | `900` | How long last recognized face is remembered for voice |
| `FACE_MATCH_THRESHOLD` | `0.36` | SFace cosine-similarity acceptance (higher = stricter) |
| `FACE_SESSION_PRIMARY_HOLD_SECONDS` | `90` | Remember primary viewer across brief gaps |
| `FACE_SECONDARY_AREA_RATIO` | `0.40` | Suppress strict ID on small background faces |
| `ALARM_WAV_PATH` | `../main/beep.wav` | Beep POSTed after **normal** (non-medical) alarm TTS |
| `ALARM_TTS_SAMPLE_RATE` | `16000` | Alarm TTS resample rate (keeps WAV under ESP limit) |
| `ALARM_TICK_SECONDS` | `1.0` | Scheduler poll interval for due alarms |
| `ALARM_MEDICAL_REPEAT_MINUTES` | `3` | Re-fire medical alarms until user confirms |
| `ALARM_NLP_FALLBACK` | `1` | Use Ollama JSON when regex fails (`0` to disable) |
| `OLLAMA_MODEL` | `qwen2.5:1.5b` | LLM for voice + alarm NLP fallback |

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
  nino_eye.c
  ssd1351.c
  PDTM.wav

docs/
  SERVO.md
  ALARM.md
  WIFI_PROVISION.md

server/
  app.py
  alarm_service.py
  alarm_voice.py
  alarm_nlp.py
  alarm_ack.py
  alarm_medical.py
  esp_playback.py
  camera.py
  face_service.py
  llm_service.py
  tts_service.py
  voice_service.py
  wav_resample.py
  data/alarms.json
  data/latency_log.json
  data/face_embeddings.json
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
- **Alarms** require `--esp-play-wav-url` (or `ESP_PLAY_WAV_URL`); medical alarms need `voice connect` on the board for yes/no and follow-up.
- Face greeting and alarm TTS from the server use head motion during `/play_wav`.
- Voice WebSocket `/voice-query` replies also queue with head motion unless interrupted by touch or `/servo/360`.
- `POST /play_wav` and `POST /servo/360` have no authentication — use only on a trusted LAN.
- Eye displays initialize first in `app_main`, so the idle face shows during the rest of boot; if OLED init fails the firmware logs a warning and runs without eyes.
- Both OLEDs mirror the same eye by default; the driver supports per-eye drawing (`ssd1351_target()`) for future asymmetric expressions.
- Windows is the recommended platform for the default speech synthesis path.
- Face data lives in `server/data/faces/` (JPEG crops) and `server/data/face_embeddings.json` (small JSON) — the old LBPH `face_model.yml` is no longer generated and can be deleted.
- If you store `elevenlabs_api_key` in `server/server_config.json`, keep that file out of public commits — it contains a secret.

## Troubleshooting

- If video is missing, verify `http://<ESP_IP>/snapshot.jpg`.
- If faces are not recognized, register **a handful of varied samples per person** (angles, lighting, distance) and check the server log for `detector=yunet` and the SFace model in `server/data/models/`; lower `FACE_MATCH_THRESHOLD` slightly (e.g. `0.32`) if matches are too strict.
- If the wrong person is recognized, raise `FACE_MATCH_THRESHOLD` (e.g. `0.42`) and stand closest to the camera (primary face wins).
- If voice does not connect, verify the PC IP is reachable from the ESP (`voice connect <PC_IP> 8000`) and the server is running.
- If the log shows **`ElevenLabs STT failed ... missing_permissions`**, your API key was created without the **Speech to Text** scope — edit the key in the ElevenLabs dashboard (or create one with full access), update `ELEVENLABS_API_KEY`, and restart the server in a new terminal.
- If STT is slow (6–30 s `stt_seconds` in `server/data/latency_log.json`), the server is using local Whisper — check that `ELEVENLABS_API_KEY` is set and the log line reads `stt(elevenlabs)=...`.
- If **medical alarm ack** works for yes/no but not after *reschedule or cancel?*, flash latest firmware (WebSocket `prompt_medical_ack`) and restart the server.
- If alarm fire logs **`WAV too large for ESP`**, restart server (16 kHz + shorter repeat TTS); medical alarms use TTS only, no beep.
- If alarm time voice fails with *“Please use a 12-hour time”*, restart server (NLP normalizes `20:36 PM` / `8.36pm` style output).
- If touch audio fails, check QT2120 init on I2C (GPIO 7/8) and speaker setup in serial logs.
- If the **eyes stay black**, check the boot log for `SSD1351 ready: 2 panel(s) 128x96`; verify wiring on J1 (CLK 23, DIN 22, DC 21, RST 20, CS 26/27) and that both panels share 3.3 V/GND. If red/blue look swapped, set `SSD1351_SWAP_RB` to `1` in `main/ssd1351.h`.
- If the eyes show but never change during a voice query, confirm the wake word fires (`Hi ESP detected` in the log) — the listening/thinking expressions are driven by the voice pipeline, and `eye listening` on the console tests the display path alone.
- If **360 spin** does not run from voice, confirm `--esp-play-wav-url http://<ESP_IP>/play_wav` is set, firmware includes `/servo/360`, and U2D2/servos show ready in logs.
- USB hub: U2D2 scan errors alongside UVC camera on J18 are common when the Dynamixel adapter is unplugged or still enumerating.

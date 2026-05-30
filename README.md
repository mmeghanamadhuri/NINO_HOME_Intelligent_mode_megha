# NiNO Home — Voice, Vision, Touch & Servo  (ESP32-P4)

NiNO is a smart-home demo for the ESP32-P4 Function EV Board that uses vision, voice, touch, and servo motion together.

- **Vision**: USB UVC camera on the board, face detection/recognition on the PC, personalized greetings played through the ESP speaker.
- **Voice**: Wake-word capture on the board, Whisper speech recognition and Ollama LLM responses on the PC, audio returned to the board.
- **Touch**: QT2120 capacitive touch sensor triggers an embedded warning audio clip.
- **Servo**: embedded servo motion control for connected actuators on the ESP board.

## Features

- ESP32-P4 firmware with UVC camera support, HTTP streaming, WAV playback, voice wake capture, touch handling, and servo motion control.
- Python FastAPI server for face UI, Whisper STT, Ollama LLM prompts, and TTS delivery to the board.
- Single shared speaker queue on the ESP to serialize touch warnings, greetings, and voice replies.
- Recommended Windows server support for default SAPI text-to-speech.

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
- **Touch**: QT2120 capacitive sensor, I2C address `0x1C`
- **Audio**: ES8311 codec for microphone and speaker
- **Network**: PC and ESP on the same LAN
- **LLM**: Ollama model such as `qwen2.5:1.5b`

## Firmware overview

The ESP firmware provides:

- UVC host video capture
- HTTP endpoints for `/stream`, `/snapshot.jpg`, and `/play_wav`
- Wake-word support using ESP-SR WakeNet
- VAD-based voice capture and WebSocket transport
- QT2120 touch sensor warnings
- Embedded servo motion control for board actuators
- Shared speaker FIFO queue in `main/audio_queue.c`

### Key firmware files

- `main/main.c` — UVC, Wi-Fi, HTTP server, voice console
- `main/audio_queue.c` — speaker FIFO queue
- `main/audio_playback.c` — ES8311 playback and beep tones
- `main/audio_capture.c` — microphone capture
- `main/voice_wake.cpp` — wake word detection
- `main/voice_assist.c` — VAD and voice session management
- `main/voice_ws_client.c` — WebSocket client to PC
- `main/touch_sensor.c` — QT2120 capacitive touch handling
- `main/bsp_qt2120.c` — QT2120 I2C driver
- `main/servo_dxl.c` — Dynamixel servo control interface
- `main/servo_dxl.h` — Dynamixel servo control definitions
- `main/servo_motion.c` — servo motion sequencing
- `main/servo_motion.h` — servo motion control helpers
- `main/PDTM.wav` — embedded touch warning audio

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
  --face-threshold 55 \
  --face-confirm-frames 4
```

To use a local webcam instead of the ESP stream:

```powershell
python app.py --camera-source auto
```

Open the UI at `http://localhost:8000`.

## Typical workflow

1. Flash the ESP firmware.
2. Power the board and connect the camera.
3. Configure Wi-Fi from the ESP serial console:

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

7. Say **"Hi ESP"** to trigger the voice assistant.

## HTTP API

### Firmware endpoints

- `GET /` — simple device page and snapshot link
- `GET /stream` — MJPEG live stream
- `GET /snapshot.jpg` — one JPEG snapshot
- `POST /play_wav` — queue WAV audio for playback

### Server endpoints

- `GET /` — web UI
- `GET /video_feed` — annotated MJPEG stream
- `POST /api/camera` — change camera source
- `POST /api/register` — register face data
- `POST /api/retrain` — retrain face recognition model
- `WS /voice-query` — voice assistant WebSocket

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
  PDTM.wav

server/
  app.py
  camera.py
  face_service.py
  llm_service.py
  tts_service.py
  voice_service.py
  wav_resample.py
  requirements.txt
  server_config.json
  templates/
  static/
  data/

managed_components/
  ...
```

## Notes

- The ESP speaker queue ensures one audio clip plays at a time.
- Touch warnings are queued if other audio is already playing.
- `POST /play_wav` has no authentication, so use this only on a trusted LAN.
- Windows is the recommended platform for the default speech synthesis path.

## Troubleshooting

- If video is missing, verify `http://<ESP_IP>/snapshot.jpg`.
- If faces are not recognized, retrain with more samples and improve lighting.
- If voice does not connect, verify the PC IP is reachable from the ESP and the server is running.
- If touch audio fails, check QT2120 initialization and speaker setup in the serial logs.

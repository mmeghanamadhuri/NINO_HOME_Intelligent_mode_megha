
## 4. Module Map

| File | Responsibility |
|------|----------------|
| **`main.c`** | Orchestration: Wi-Fi, HTTP, UVC pipeline, face-track task, discovery, boot chimes, console |
| **`wifi_config.h`** | AP defaults (`ESP32_P4_CAM`), STA credential API |
| **`wifi_prov_ble.c`** | NimBLE GATT Wi-Fi provisioning via ESP-Hosted C6 |
| **`ssd1351.c`** | Dual SPI OLED driver (128×96) |
| **`nino_eye.c`** | Eye animation engine (9 states, blink/gaze/heart) |
| **`audio_playback.c`** | ES8311 I2S speaker, volume, WAV decode/play, bus lock |
| **`audio_capture.c`** | Mic buffer helpers |
| **`audio_queue.c`** | Dual-queue playback worker; touch preemption; servo sync |
| **`voice_wake.cpp`** | esp-sr AFE + WakeNet (“Hi ESP”) |
| **`voice_assist.c`** | VAD capture → WAV; medical ack re-listen |
| **`voice_ws_client.c`** | WebSocket client to PC; parses `eye_expression`, `prompt_medical_ack` |
| **`face_detect.cpp`** | ESP-DL human face detector on JPEG frames |
| **`face_tracker.c`** | Pan servo (ID 2) follows face centroid |
| **`servo_dxl.c`** | USB FTDI/U2D2 → Dynamixel protocol; 360 spin; track hon |
| **`servo_motion.c`** | Head motion during TTS (L/R/U/D or nod-only) |
| **`touch_sensor.c`** | QT2120 poll → `PDTM.wav` on touch |
| **`bsp_qt2120.c`** | QT2120 I2C driver |

---
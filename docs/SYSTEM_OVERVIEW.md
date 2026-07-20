# NiNO — System Overview

Short feature map of Voice, Vision, Touch, Servo, Alarm, LCD, and apps.

---

## Stack at a glance


| Layer     | Current                                                         |
| --------- | --------------------------------------------------------------- |
| Board     | ESP32-P4 — camera, USB 4-mic, speaker, touch, servos, OLED eyes |
| Server    | Python FastAPI on PC (GPU preferred)                            |
| LLM       | Ollama `qwen2.5:1.5b`                                           |
| STT / TTS | ElevenLabs (local Whisper / SAPI / espeak as fallback)          |
| Faces     | YuNet detect + SFace embeddings                                 |
| Emotion   | AWS Rekognition + local model fallback                          |
| Memory    | PostgreSQL — conversations, summaries, alarms context           |
| Latency   | ~2–3 s end-to-end on GPU                                        |


---

## Voice

**Path:** local STT + TTS + LLM → **ElevenLabs STT/TTS** + same LLM.

- Wake: **"Hi ESP"** on USB 4-mic → beep → VAD → WebSocket to PC
- Pipeline: STT → face identity → route (alarm / servo / recap / identity / LLM) → TTS → ESP speaker
- LLM stays `**qwen2.5:1.5b`** (Ollama)
- Ran on CPU first; now **GPU** — typical reply latency **~2–3 s**

---

## Vision — face recognition

**Path:** Haar cascade → **YuNet** + **SFace** (CNN embeddings).

- Live match against stored face embeddings
- Samples + embeddings on disk; retrain from web UI
- Session hold keeps primary viewer across short gaps

---

## Emotion recognition

**Path:** custom trained model (weaker) → **AWS Rekognition** + **local stable model** as fallback when AWS is unavailable.

- Labels attached on the live video feed
- Drives LCD eye expression (happy / sad / …)

---

## Face registration

Both paths work:


| Method    | How                                                                             |
| --------- | ------------------------------------------------------------------------------- |
| **Web**   | Name + samples on `http://localhost:8000` → capture & train                     |
| **Voice** | Unknown face held ~3 s → bot asks name → USB mic capture → same sample pipeline |


---

## Face tracking (on-board)

- **ESP-DL** detects face on the board (no cloud in the control loop)
- Error = frame center vs face center → **pan servo** centers the face
- CLI: `track on` / `track off` / `track status`
- Pauses when audio motion or 360 spin owns the servos

---

## Alarms

- Fire from **local PC time**
- Set / list / cancel by **voice**; also manage on web UI
- **Priority** scheduling; **medical (P0)** needs yes/no acknowledgement (board auto-listen or web Yes/No)
- Persist to `alarms.json`

---

## Context / memory (PostgreSQL)

Stores:

- Users (face-linked)
- Conversations
- Long-term memories / facts
- Daily **summaries**

Summaries feed **startup greetings** and recap answers (“what did we talk about?”).

---

## LCD eyes (OLED)


| Source          | Effect                                                |
| --------------- | ----------------------------------------------------- |
| Vision emotion  | Matching expression on dual SSD1351 OLEDs             |
| Voice text      | e.g. “I’m so sad today” → **sad** for a few seconds   |
| Pipeline states | idle / listening / thinking / happy / sad / surprised |


---

## Microphone

**Path:** onboard **ES8311** mic → **USB Seeed ReSpeaker 4-mic** on GPIO header (D− 24, D+ 25).

- Far-field wake + capture tested and working
- ES8311 remains **speaker playback only**

---

## Touch & servo (extra)

- **Touch (QT2120):** warning audio preempts server playback, then resumes
- **Servos:** Dynamixel AX — tracking pan + voice / HTTP **360°** spin (ID2)

---

## Application — web UI

**URL:** [http://localhost:8000](http://localhost:8000)


| Area      | What it does                                                          |
| --------- | --------------------------------------------------------------------- |
| Live feed | Annotated MJPEG — faces + emotion labels                              |
| Camera    | Switch local webcam / ESP stream (`auto` or `http://<ESP_IP>/stream`) |
| Register  | Name + sample count → capture & train; Retrain existing samples       |
| Alarms    | List, ack medical Yes/No, delete one / all                            |
| Status    | Live JSON — camera, faces, emotion, TTS, LLM, alarms, memory          |


Also: snapshot, latency log, memory stats APIs.

---

## Application — mobile

- Discovers board via **mDNS** (`_nino._tcp`, host e.g. `NINO-HOME.local`)
- **HTTP `GET /status`** — Wi-Fi, volume, firmware, IP
- Optional **WS `/ws/status`** for live status frames
- Used for discovery + device status on the LAN

---

## How the pieces connect

```text
USB 4-mic ── wake / VAD ──► PC: STT → LLM → TTS ──► ES8311 speaker
UVC camera ── stream ─────► YuNet/SFace + emotion ──► LCD eyes
              └─ ESP-DL track ──► pan servo (center face)
Touch ──► preempt WAV
Voice / web ──► face register, alarms
PostgreSQL ──► greetings + recap context
Mobile ──► mDNS + /status
```

---

## Related docs


| Doc                                                      | Topic              |
| -------------------------------------------------------- | ------------------ |
| [README.md](../README.md)                                | Full setup & API   |
| [server/README.md](../server/README.md)                  | Server modules     |
| [USB-4MIC-INTEGRATION.md](USB-4MIC-INTEGRATION.md)       | Mic port           |
| [VOICE_FACE_REGISTRATION.md](VOICE_FACE_REGISTRATION.md) | Voice register     |
| [ALARM.md](ALARM.md)                                     | Alarms             |
| [Track.md](Track.md)                                     | Face tracking      |
| [MOBILE-APP COM.md](MOBILE-APP%20COM.md)                 | Mobile status flow |
| [Eye states.md](Eye%20states.md)                         | LCD expressions    |



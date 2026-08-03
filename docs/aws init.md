# AWS Init — Server-Side Changes for Full NiNO on AWS

What must change in `server/` so the brain runs on **AWS (EC2 / ECS)** and the bot works on **any Wi‑Fi** — voice, vision, emotion, alarms — without a PC on the same LAN.

Pair with [OPEN_PLAN.md](OPEN_PLAN.md) (Phase 1) and [SERVER_ARCHITECTURE.md](SERVER_ARCHITECTURE.md).

---

## Table of contents

- [1. One-page summary](#1-one-page-summary)
- [2. Why AWS alone is not enough today](#2-why-aws-alone-is-not-enough-today)
- [3. What works on AWS with zero code changes](#3-what-works-on-aws-with-zero-code-changes)
- [4. Target architecture on AWS](#4-target-architecture-on-aws)
- [5. Server changes by file](#5-server-changes-by-file)
- [6. New modules to add](#6-new-modules-to-add)
- [7. Environment variables on AWS](#7-environment-variables-on-aws)
- [8. AWS infrastructure checklist](#8-aws-infrastructure-checklist)
- [9. Implementation order](#9-implementation-order)
- [10. Firmware dependencies (not server, but required)](#10-firmware-dependencies-not-server-but-required)
- [11. Local vs AWS quick reference](#11-local-vs-aws-quick-reference)

---

## 1. One-page summary

**Goal:** Run `server/` on AWS. Bot on any Wi‑Fi. Full NiNO: voice, face ID, emotion empathy, alarms, memory.

**The rule:** The bot can always connect **out** to AWS. AWS **cannot** connect **in** to the bot’s private `192.168.x.x` address.

| Data path | Today (LAN) | Required on AWS |
|-----------|-------------|-----------------|
| Voice audio | Bot → server WSS | **Same** — already works |
| Camera / vision | Server **pulls** `GET http://<ESP_IP>/stream` | Bot **pushes** JPEG to AWS |
| TTS / alarms / empathy | Server **POSTs** `http://<ESP_IP>/play_wav` | AWS **pushes** on open WSS |
| Servo 360 | Server **POSTs** `http://<ESP_IP>/servo/360` | AWS **pushes** on open WSS |

**Bottom line:** Deploying `server/` to EC2 gives you **voice only** until you add **frame upload** and **WSS push playback** on the server (and matching firmware).

---

## 2. Why AWS alone is not enough today

“Bot is on the internet and talks to AWS” is true for **voice** — the bot opens the WebSocket.

Vision and alarms use the **opposite direction** — the server calls the bot’s LAN HTTP API. EC2 cannot reach `192.168.x.x`.

```text
WORKS TODAY ON AWS:
  Bot ──WSS──► EC2   /voice-query   (mic audio in, TTS WAV out)

BROKEN TODAY ON AWS:
  EC2 ──HTTP──► Bot   GET /stream         (camera pull)
  EC2 ──HTTP──► Bot   POST /play_wav      (empathy, alarms, greetings)
  EC2 ──HTTP──► Bot   POST /servo/360     (voice-triggered spin)
```

**Your fix (stream to AWS):** Yes — bot pushes camera to EC2. That is the right design. It is **Phase 1** in [OPEN_PLAN.md](OPEN_PLAN.md) and is **not implemented yet** in `server/`.

---

## 3. What works on AWS with zero code changes

Deploy as-is with TLS + cloud API keys:

| Feature | Works on EC2? | Notes |
|---------|---------------|-------|
| Voice WebSocket `/voice-query` | **Yes** | Bot: `voice url wss://api.yourdomain.com/voice-query` |
| STT / LLM / TTS | **Yes** | ElevenLabs + Groq/OpenAI (avoid Ollama on small EC2) |
| PostgreSQL memory | **Yes** | Amazon RDS |
| Alarm scheduler (logic) | **Yes** | Playback to bot **fails** without WSS push |
| Vision / emotion | **No** | No camera feed unless you port-forward bot (not portable) |
| Face ID during voice | **No** | `_camera_identity_snapshot()` uses `camera.read()` pull |
| Web UI live camera | **No** | Same pull dependency |

### Voice-only AWS env (no server code changes)

```env
STT_PROVIDER=elevenlabs
ELEVENLABS_API_KEY=...
DATABASE_URL=postgresql://...@your-rds.amazonaws.com:5432/nino?sslmode=require
VOICE_WS_URL=wss://api.yourdomain.com/voice-query

# Do NOT set — EC2 cannot reach private bot IP:
# CAMERA_SOURCE=http://192.168.x.x/stream
# ESP_PLAY_WAV_URL=http://192.168.x.x/play_wav
```

Bot serial:

```text
voice url wss://api.yourdomain.com/voice-query
```

---

## 4. Target architecture on AWS

All **bot-initiated** connections. AWS never needs the bot’s private IP.

```mermaid
flowchart TB
    subgraph Bot["ESP32-P4 — any Wi-Fi"]
        Mic[Mic + wake word]
        Cam[UVC camera]
        Spk[Speaker + eyes]
    end

    subgraph AWS["AWS"]
        ALB[ALB / nginx TLS]
        API[FastAPI server/]
        FS[FrameStore per device]
        Hub[DeviceSessionHub WSS]
        RDS[(RDS PostgreSQL)]
    end

    Mic -->|WSS voice WAV| ALB
    Cam -->|HTTPS POST /v1/devices/id/frame| ALB
    ALB --> API
    API --> FS
    API --> Hub
    Hub -->|WSS push play_wav / servo| Spk
    API --> RDS
```

### Transport summary

| Purpose | Endpoint | Direction |
|---------|----------|-----------|
| Voice query | `wss://api.yourdomain.com/voice-query` | Bot → AWS (reply on same socket) |
| Camera frames | `POST /v1/devices/{id}/frame` | Bot → AWS |
| Empathy / alarms / greetings | WSS `{type:"play_wav"}` + binary | AWS → Bot (on open socket) |
| Servo 360 | WSS `{type:"servo_360"}` | AWS → Bot |
| Device pairing | `POST /v1/devices/register` | Bot → AWS |

---

## 5. Server changes by file

Introduce a deploy mode switch used everywhere:

```python
# deploy_mode.py
def is_aws_mode() -> bool:
    return os.environ.get("NINO_DEPLOY_MODE", "local").strip().lower() == "aws"
```

### 5.1 `app.py` — central routing

| Change | Detail |
|--------|--------|
| `NINO_DEPLOY_MODE` | `local` (default) vs `aws` |
| Conditional camera | `local`: keep `CameraStream`. `aws`: skip ESP pull; use `FrameStore` |
| `_camera_identity_snapshot(device_id)` | `aws`: decode latest JPEG from `FrameStore`, run `faces.recognize()` |
| `_mjpeg_generator()` | `aws`: build web UI stream from uploaded frames, or disable |
| WebSocket auth | Validate JWT / device token on `/voice-query` connect |
| Session hub | Track open WSS per `device_id` in `DeviceSessionHub` |
| `_delayed_esp_servo_360` | `aws`: push via hub, not HTTP POST |
| Startup | `camera.start()` only when `NINO_DEPLOY_MODE=local` |

**Branch points today:**

- Line ~145: `camera = CameraStream(DEFAULT_CAMERA_SOURCE)` — skip ESP URL in aws mode
- Line ~212: `camera.start()` — local only
- Line ~583: `_camera_identity_snapshot()` — needs `device_id` + frame source abstraction
- Line ~455: `_mjpeg_generator()` — aws loop over `FrameStore`
- Line ~708: `_voice_ws_pipeline()` — attach `device_id` from auth; pass to identity + delivery

### 5.2 `camera.py` — keep for local; bypass on AWS

| Mode | Behavior |
|------|----------|
| `local` | Unchanged — pull MJPEG / webcam |
| `aws` | Do not start `CameraStream` for ESP URL |

Add a thin adapter so callers stop using `camera.read()` directly:

```python
def get_frame(device_id: str | None = None) -> np.ndarray | None:
    if is_aws_mode():
        jpeg = frame_store.latest_jpeg(device_id)
        return decode_jpeg(jpeg) if jpeg else None
    return camera.read()
```

Replace all `camera.read()` in aws paths with `get_frame(device_id)`.

### 5.3 `esp_playback.py` — dual delivery (biggest playback change)

**Today:** only `post_wav_to_esp()` → HTTP POST to `ESP_PLAY_WAV_URL`.

**Add:**

```python
def deliver_wav_to_device(
    device_id: str,
    wav: bytes,
    *,
    eye_expression: str | None = None,
    prompt_ack: bool = False,
    prompt_ack_chime: bool = True,
) -> None:
    if is_aws_mode():
        device_hub.push_play_wav(
            device_id, wav,
            eye_expression=eye_expression,
            prompt_ack=prompt_ack,
            prompt_ack_chime=prompt_ack_chime,
        )
    else:
        post_wav_to_esp(wav, eye_expression=eye_expression, ...)
```

**Deprecate on AWS:**

- `ensure_esp_play_wav_url_configured()` — not required
- `derive_esp_base_url()` — LAN only

### 5.4 `network_util.py`

**Today:** builds `ws://LAN_IP:8000/voice-query` from guessed LAN IP.

**AWS:**

- Return `VOICE_WS_PUBLIC_URL` from env (e.g. `wss://api.yourdomain.com/voice-query`)
- Stop guessing LAN IP when `NINO_DEPLOY_MODE=aws`
- `X-Nino-Voice-Ws-Url` header (LAN `prompt_ack` path) not needed on AWS — bot already has cloud URL in NVS

### 5.5 `vision_emotion_service.py`

| Change | Detail |
|--------|--------|
| Per-device state | Accumulate emotion per `device_id`, not one global camera |
| Frame source | `process_frame(frame, results)` unchanged — caller supplies frame from upload |
| `speak_wav` callback | Route through `deliver_wav_to_device(device_id, ...)` |

**AWS background loop (in `app.py`):**

```text
for each device_id with fresh frame in FrameStore:
  frame = decode_jpeg(...)
  results = faces.recognize(frame)
  vision_emotion.process_frame(frame, results)  # per-device instance or pass device_id
```

### 5.6 `voice_service.py` + `_voice_ws_pipeline`

| Change | Detail |
|--------|--------|
| Identity | `_camera_identity_snapshot(device_id)` uses uploaded frame |
| Servo 360 | `trigger_esp_servo_360` → hub push `{type:"servo_360"}` on aws |
| Memory | Pass `device_id` + resolved face identity |

**Unchanged:** voice reply still JSON `{ eye_expression, prompt_medical_ack }` + binary WAV on same WebSocket.

### 5.7 `alarm_service.py`

| Change | Detail |
|--------|--------|
| On fire | `deliver_wav_to_device(device_id, ...)` instead of `post_wav_to_esp()` |
| Offline bot | Queue in DB; flush when device WSS reconnects |
| Scope | Alarms tied to `device_id` / household |

Current failure point:

```python
# alarm_service.py ~770
raise RuntimeError("ESP_PLAY_WAV_URL is not set")
post_wav_to_esp(tts_wav, prompt_ack=alarm.requires_ack)
```

### 5.8 `tts_service.py`

| Change | Detail |
|--------|--------|
| `_output_speech` / ESP path | Call `deliver_wav_to_device(device_id, ...)` |
| Face greetings | Need `device_id` from active frame upload context |
| `speak_to_esp()` | Thread `device_id` through from vision loop |

Current caller:

```python
# tts_service.py ~921
post_wav_to_esp(...)
```

### 5.9 `llm_service.py`

| Change | Detail |
|--------|--------|
| Ollama on EC2 CPU | Slow for Qwen — prefer `GROQ_API_KEY` or `OPENAI_API_KEY` |
| Env only | No structural change if LLM stays HTTP from server |

### 5.10 `memory_service.py`

| Change | Detail |
|--------|--------|
| RDS | `DATABASE_URL` with `?sslmode=require` |
| Schema | Unchanged |

---

## 6. New modules to add

### 6.1 `frame_store.py`

In-memory (MVP) or Redis (scale) — latest frame per device:

```python
class FrameStore:
    def put(self, device_id: str, jpeg: bytes) -> None: ...
    def latest_jpeg(self, device_id: str) -> bytes | None: ...
    def latest_timestamp(self, device_id: str) -> float: ...
    def is_stale(self, device_id: str, max_age_s: float = 4.0) -> bool: ...
    def device_ids_with_fresh_frames(self, max_age_s: float = 4.0) -> list[str]: ...
```

### 6.2 `device_auth.py`

- Validate `Authorization: Bearer <token>` on HTTP + WSS
- Map token → `device_id`
- Pairing / register endpoints

### 6.3 `device_session_hub.py`

Track persistent WebSocket per device:

```python
class DeviceSessionHub:
    async def register(self, device_id: str, ws: WebSocket) -> None: ...
    async def unregister(self, device_id: str, ws: WebSocket) -> None: ...
    async def push_play_wav(self, device_id: str, wav: bytes, **meta) -> bool: ...
    async def push_servo_360(self, device_id: str) -> bool: ...
    def is_online(self, device_id: str) -> bool: ...
```

**Push protocol (server → bot on open WSS):**

```json
{"type": "play_wav", "eye_expression": "happy", "prompt_medical_ack": false, "wav_len": 12345}
```

→ followed by binary WebSocket frame (WAV bytes)

```json
{"type": "servo_360"}
```

### 6.4 `device_registry.py` — HTTP routes

| Method | Path | Caller |
|--------|------|--------|
| POST | `/v1/pairing/create` | Mobile app |
| POST | `/v1/devices/register` | Bot (pairing code → tokens) |
| POST | `/v1/devices/token/refresh` | Bot |
| POST | `/v1/devices/{id}/frame` | Bot (JPEG body) |
| GET | `/v1/devices/{id}/status` | App / admin |

### 6.5 `deploy_mode.py`

Single helper — use everywhere instead of scattered env checks (see §5 intro).

---

## 7. Environment variables on AWS

### Full AWS mode (`NINO_DEPLOY_MODE=aws`)

```env
# Mode
NINO_DEPLOY_MODE=aws

# Public URLs (Route53 + ALB / nginx)
VOICE_WS_PUBLIC_URL=wss://api.yourdomain.com/voice-query
API_PUBLIC_BASE=https://api.yourdomain.com

# Auth
JWT_SECRET=...
DEVICE_TOKEN_TTL_SECONDS=86400

# STT / TTS — avoid running Whisper on small EC2
STT_PROVIDER=elevenlabs
ELEVENLABS_API_KEY=...
TTS_PROVIDER=elevenlabs

# LLM — prefer API over Ollama on t3.medium
GROQ_API_KEY=...
# OPENAI_API_KEY=...

# Database
DATABASE_URL=postgresql://user:pass@your-rds.region.rds.amazonaws.com:5432/nino?sslmode=require

# Vision
VISION_EMOTION_ENABLED=1
EMOTION_BACKEND=ferplus
```

### Do NOT set on AWS

```env
# These assume server can reach bot LAN IP — wrong for portable product:
CAMERA_SOURCE=http://192.168.x.x/stream
ESP_PLAY_WAV_URL=http://192.168.x.x/play_wav
NINO_SERVER_LAN_HOST=...
```

### Local dev (unchanged)

```env
NINO_DEPLOY_MODE=local
CAMERA_SOURCE=http://192.168.0.89/stream
ESP_PLAY_WAV_URL=http://192.168.0.89/play_wav
OLLAMA_URL=http://127.0.0.1:11435/api/generate
```

---

## 8. AWS infrastructure checklist

| Component | AWS service | Notes |
|-----------|-------------|-------|
| FastAPI app | EC2 **t3.large** (4 GB) or ECS Fargate 2 vCPU / 4 GB | OpenCV + ONNX need RAM |
| TLS + WSS | ALB or nginx on EC2 | WSS needs sticky sessions or single instance initially |
| DNS | Route53 | `api.yourdomain.com` |
| PostgreSQL | RDS **db.t3.micro** | Same schema as local |
| Secrets | Secrets Manager or SSM | API keys, JWT secret |
| Logs | CloudWatch | Container stdout / `/var/log/nino` |
| LLM | Groq / OpenAI API | Avoid GPU EC2 for MVP |
| Frame store (later) | ElastiCache Redis | When scaling beyond one EC2 |

**Security group:**

- Inbound: **443** from `0.0.0.0/0` (HTTPS + WSS)
- Outbound: all (STT / TTS / LLM APIs)
- **Do not** expose bot port 80 to the internet

**Docker (recommended):**

```dockerfile
FROM python:3.12-slim-bookworm
RUN apt-get update && apt-get install -y libgl1 libglib2.0-0 && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
EXPOSE 8000
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
```

---

## 9. Implementation order

Do these in order so you can test after each step.

### Step A — Voice on AWS (1–2 weeks, minimal server change)

- [ ] Dockerize `server/`
- [ ] EC2 + nginx/ALB + TLS + Route53
- [ ] Set `VOICE_WS_PUBLIC_URL`, ElevenLabs, Groq/OpenAI
- [ ] RDS for memory (optional)
- [ ] Bot: `voice url wss://api.yourdomain.com/voice-query`

**Result:** Voice from any Wi‑Fi. No vision / passive empathy / alarm playback on bot.

### Step B — Frame upload (2–3 weeks)

- [ ] Add `frame_store.py`
- [ ] Add `POST /v1/devices/{id}/frame` + `device_auth.py`
- [ ] Add `get_frame(device_id)` abstraction
- [ ] Branch `_camera_identity_snapshot` + vision loop to use uploads
- [ ] Firmware: JPEG POST task (separate work in `main/`)

**Result:** Face ID during voice + emotion pipeline on AWS.

### Step C — WSS push playback (2–3 weeks)

- [ ] Add `device_session_hub.py`
- [ ] Refactor `esp_playback.py` → `deliver_wav_to_device`
- [ ] Update `tts_service.py`, `alarm_service.py`, `vision_emotion_service.py`
- [ ] Persistent WSS + inbound dispatch on firmware

**Result:** Alarms, empathy TTS, servo — **full NiNO on AWS**.

### Step D — Product hardening

- [ ] Device registry + pairing API
- [ ] Rate limits
- [ ] CloudWatch alarms
- [ ] Multi-instance / Redis session hub (later)

---

## 10. Firmware dependencies (not server, but required)

Server changes alone are not enough. The bot must:

| Item | Purpose |
|------|---------|
| `wss://` + CA bundle | TLS to AWS |
| Cloud URL in NVS | `voice url wss://api.yourdomain.com/voice-query` or pairing |
| `Authorization: Bearer` on WSS + frame POST | Device auth |
| JPEG upload task | Vision on AWS (`POST /v1/devices/{id}/frame`) |
| Persistent WSS + inbound handler | Alarms / empathy push |

See [OPEN_PLAN.md](OPEN_PLAN.md) §7 and [FIRMWARE_ARCHITECTURE.md](FIRMWARE_ARCHITECTURE.md).

---

## 11. Local vs AWS quick reference

| Concern | `local` | `aws` |
|---------|---------|-------|
| Camera ingest | `CameraStream` pull | Bot `POST /frame` |
| Face during voice | `camera.read()` | `FrameStore` |
| Vision emotion | `_mjpeg_generator` loop | Per-device loop on uploads |
| TTS to speaker | `POST /play_wav` | WSS push `play_wav` |
| Alarms | `post_wav_to_esp` | WSS push |
| Servo 360 | HTTP POST | WSS push |
| Voice reply | WSS return | WSS return (same) |
| `CAMERA_SOURCE` | Required (ESP URL) | **Unset** |
| `ESP_PLAY_WAV_URL` | Required | **Unset** |
| `VOICE_WS_PUBLIC_URL` | Optional | **Required** |

---

## Summary

| Question | Answer |
|----------|--------|
| Can I put `server/` on EC2? | **Yes** |
| Does everything work immediately? | **No** — voice yes; vision + alarms need server changes |
| Can bot stream to AWS? | **Yes** — that is the fix; add frame upload (Step B) |
| Minimum server work for full NiNO | Steps A + B + C above |
| Keep local dev working? | Yes — `NINO_DEPLOY_MODE=local` preserves current behavior |

---

*Document: aws init.md — AWS server-side migration guide, July 2026.*

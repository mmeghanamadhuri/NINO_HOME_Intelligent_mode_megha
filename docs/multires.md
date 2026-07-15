# Multires — One Local Server, Many Robots

Can today’s NiNO `server/` on a **PC (same Wi‑Fi)** be the main brain for **multiple ESP32 robots**?

**Short answer:** Voice WebSockets can accept many clients, but the rest of the stack is built for **one bot**. Full multi-robot on LAN needs a **device registry** and **per-device routing** for camera, playback, identity, alarms, and volume/servo.

This doc is **local / same-LAN only**. Do multi-robot here first; cloud migration is a later step and is out of scope.

Pair with [SERVER_ARCHITECTURE.md](SERVER_ARCHITECTURE.md) for how the single-bot stack works today.

---

## Table of contents

- [1. Verdict](#1-verdict)
- [2. What works today with 2+ bots](#2-what-works-today-with-2-bots)
- [3. What breaks today](#3-what-breaks-today)
- [4. Target model (local LAN)](#4-target-model-local-lan)
- [5. What to build (server)](#5-what-to-build-server)
- [6. What to change (firmware)](#6-what-to-change-firmware)
- [7. Example `devices.json`](#7-example-devicesjson)
- [8. Implementation order](#8-implementation-order)
- [9. Checklist](#9-checklist)

---

## 1. Verdict

| Capability | Today (one PC server) | Needed for multi-robot on LAN |
|------------|----------------------|-------------------------------|
| Bot opens `/voice-query` WebSocket | Yes — concurrent sockets OK | Tag each socket with `device_id` |
| Voice reply WAV back on same socket | Yes — already per-connection | Keep |
| Camera / face during voice | **No** — one `CAMERA_SOURCE` | One stream URL (or reader) per device |
| Greetings / emotion TTS / alarms | **No** — one `ESP_PLAY_WAV_URL` | POST `/play_wav` to **that** robot’s IP |
| Volume / servo HTTP | **No** — derived from one ESP host | Per-device base URL |
| Memory / faces | Person-keyed (OK to share) | Alarms must store `device_id` so they fire on the right bot |

**Bottom line:** The server is a **single-robot LAN brain** today. It is **not** ready as a multi-robot hub until device identity + routing exist.

---

## 2. What works today with 2+ bots

If two robots both point at `ws://<PC_IP>:8000/voice-query`:

1. FastAPI accepts **both** WebSockets.
2. Each wake → STT → LLM → TTS can return audio **on that bot’s own socket**.

So **voice-only** (ignore vision and proactive HTTP) can roughly work. Shared Whisper/Ollama will slow down if both wake at once.

```text
Robot A ──WS──► PC server ──WAV──► Robot A   ✅
Robot B ──WS──► PC server ──WAV──► Robot B   ✅
```

---

## 3. What breaks today

Same Wi‑Fi does **not** fix multi-robot by itself. The server still has **one** camera URL and **one** play URL:

```text
Robot A camera ◄── GET /stream ── server   ← only this in CAMERA_SOURCE
Robot B camera     (ignored)

server ──POST /play_wav──► Robot A only   ← only ESP_PLAY_WAV_URL
Robot B                    (no alarms / greetings / volume)
```

| Area | Why it fails with 2+ robots |
|------|-----------------------------|
| **Camera** | One `CameraStream` + one `CAMERA_SOURCE` (`server/camera.py`, `app.py`) |
| **Playback** | One `ESP_PLAY_WAV_URL` (`server/esp_playback.py`) |
| **Identity snapshot** | `_camera_identity_snapshot()` reads the single global camera |
| **Voice viewer TTL** | Global `_voice_viewer_name` — bots crosstalk |
| **Face registration** | One FSM / one “who’s in front” session |
| **Alarms** | Fire via `post_wav_to_esp()` — always the one URL; no `device_id` on alarm rows |
| **Servo / volume** | HTTP against the single derived ESP host |
| **Web UI MJPEG** | One live stream for one camera |

There is **no `device_id` in `server/*.py` today**.

---

## 4. Target model (local LAN)

One PC runs `server/` on the LAN. Each robot has a **stable id** and known **LAN HTTP base** (server can still reach `192.168.x.x`).

```text
        Same Wi-Fi
  ┌──────────────────────────────────┐
  │  Robot A (device_id=nino-01)     │
  │    IP 192.168.1.10               │
  │    WS → PC :8000/voice-query     │
  │    PC ← GET /stream              │
  │    PC → POST /play_wav           │
  └──────────────────────────────────┘
  ┌──────────────────────────────────┐
  │  Robot B (device_id=nino-02)     │
  │    IP 192.168.1.11               │
  │    same pattern, own URLs        │
  └──────────────────────────────────┘
                    │
                    ▼
         ┌─────────────────────┐
         │  PC — FastAPI :8000 │
         │  DeviceRegistry     │
         │  CameraPool (pull)  │
         │  Shared faces/DB    │
         └─────────────────────┘
```

**Rule:** Every action that hits a board (read camera, play WAV, servo, alarm fire, face-for-this-turn) must carry a **`device_id`**.

**Shared across robots (OK on one household server):**

- Face gallery / `person_id`
- PostgreSQL memory for users
- Whisper / LLM / TTS

**Isolated per robot (required):**

- Camera pull URL / latest frames
- Open voice WebSocket (already per connection)
- `POST /play_wav`, volume, servo host
- Alarm delivery target
- Emotion / vision loop state
- Face-registration session (or enroll one device at a time on purpose)

---

## 5. What to build (server)

### 5.1 Device registry

New module, e.g. `server/device_registry.py`, loaded from `server/data/devices.json`:

```python
# Conceptual — not implemented yet
@dataclass
class DeviceRecord:
    device_id: str
    display_name: str = ""
    camera_url: str = ""       # http://192.168.1.10/stream
    play_wav_url: str = ""     # http://192.168.1.10/play_wav
    base_url: str = ""         # http://192.168.1.10  (derive volume/servo)
```

**Stop relying on a single pair of env vars as the only config:**

```env
CAMERA_SOURCE=http://ONE_IP/stream
ESP_PLAY_WAV_URL=http://ONE_IP/play_wav
```

Keep them only as a **legacy fallback** when no `devices.json` exists (one default robot).

### 5.2 Attach `device_id` on `/voice-query`

On WebSocket connect, require one of:

- Query: `ws://<PC>:8000/voice-query?device_id=nino-01`
- Header: e.g. `X-Nino-Device-Id: nino-01`
- First text frame: `{ "type": "hello", "device_id": "nino-01" }`

Then:

```text
_voice_ws_pipeline(websocket, device_id)
  → identity from THAT device’s camera
  → alarm / volume / servo for THAT device
  → reply WAV still on THIS websocket
```

### 5.3 Per-device camera (LAN pull)

Preferred for local first: keep today’s pull pattern, but **N instances**:

```text
device_id → CameraStream(camera_url)
```

```python
def get_frame(device_id: str) -> np.ndarray | None:
    return camera_pool.read(device_id)
```

Replace bare `camera.read()` in voice identity and vision emotion with `get_frame(device_id)`.

Throttle FPS per stream if CPU gets hot with many bots.

### 5.4 Per-device playback

Replace every bare `post_wav_to_esp(wav)` with:

```python
def deliver_wav_to_device(device_id: str, wav: bytes, ...) -> None:
    url = registry.get(device_id).play_wav_url
    # same HTTP POST body/headers as today’s post_wav_to_esp, different URL
```

Same idea for:

- `/speaker/volume`
- `/servo/360`

Look up host from `DeviceRecord.base_url` (or parse from `play_wav_url`).

### 5.5 Alarms

- When an alarm is set from voice on robot A, store **`device_id=nino-01`**.
- On fire: `deliver_wav_to_device(alarm.device_id, ...)`.
- Do not use the global `ESP_PLAY_WAV_URL`.

### 5.6 Strip global session crosstalk

Key by `device_id`:

- `_voice_viewer_name` → `_voice_viewer_by_device[device_id]`
- Face registration FSM per device (or intentionally single-device enroll)
- Vision emotion accumulator per device

### 5.7 Web UI

- Device picker: `?device_id=nino-01` (or a dropdown)
- MJPEG / status for the selected robot only
- Do not assume one `CAMERA_SOURCE`

### 5.8 Capacity (after correctness)

| Resource | Note |
|----------|------|
| Whisper | Shared model; overlapping wakes queue or slow each other |
| Ollama / LLM | One local LLM is the usual bottleneck with several bots |
| OpenCV face | One recognizer; cost grows with N streams × FPS — throttle |

---

## 6. What to change (firmware)

Minimum for local multi-robot:

1. **Stable `device_id`** in NVS (name, serial, or MAC-based string).
2. Send it on **every** voice WebSocket connect (query or header is enough).
3. Keep existing LAN HTTP: `/stream`, `/play_wav`, volume, servo — **no protocol change** required for local v1.
4. Point each bot’s voice URL at the **same** PC: `voice connect <PC_LAN_IP> 8000` (plus `device_id`).

Lab shortcut: put IPs only in `devices.json` on the PC; firmware only needs to send `device_id`. The PC still must not use a single `ESP_PLAY_WAV_URL` for everyone.

---

## 7. Example `devices.json`

```json
{
  "devices": [
    {
      "device_id": "nino-01",
      "display_name": "Living room",
      "camera_url": "http://192.168.1.10/stream",
      "play_wav_url": "http://192.168.1.10/play_wav",
      "base_url": "http://192.168.1.10"
    },
    {
      "device_id": "nino-02",
      "display_name": "Desk",
      "camera_url": "http://192.168.1.11/stream",
      "play_wav_url": "http://192.168.1.11/play_wav",
      "base_url": "http://192.168.1.11"
    }
  ]
}
```

Suggested path: `server/data/devices.json`. Reload on start (hot-reload optional later).

If a robot’s DHCP IP changes, update this file (or add mDNS/`GET /status` refresh later).

---

## 8. Implementation order

Do this locally in order; each step should leave a working system.

### Step 1 — Voice `device_id` only

- [ ] Accept `device_id` on `/voice-query`
- [ ] Pass it through `_voice_ws_pipeline` (log it; routing not required yet)
- [ ] Two bots, two ids — both get voice replies on their own sockets

### Step 2 — Registry + playback route (core)

- [ ] `server/data/devices.json` + `device_registry.py`
- [ ] `deliver_wav_to_device(device_id, ...)`
- [ ] Wire alarms, greetings, volume, servo through it
- [ ] Keep single-bot `.env` as fallback when registry is empty

**Result:** Two robots talk; alarms/greetings play on the **correct** speaker.

### Step 3 — Per-device camera / face

- [ ] `CameraPool`: `device_id → CameraStream`
- [ ] `_camera_identity_snapshot(device_id)` / `get_frame(device_id)`
- [ ] Per-device `_voice_viewer_*`

**Result:** Face ID / memory follow the person in front of **that** robot.

### Step 4 — Vision emotion + UI

- [ ] Emotion loop per device
- [ ] Web UI device selector

---

## 9. Checklist

**Today**

- [x] Many voice WebSockets (transport)
- [ ] Per-device camera pull
- [ ] Per-device playback / alarms / servo / volume
- [ ] Per-device identity session state
- [ ] Device registry (`devices.json`)
- [ ] Multi-camera web UI

**Do next**

1. Step 1 + Step 2 — without them, every proactive action still hits **one** robot.
2. Step 3 — faces/memory match the correct camera.
3. Step 4 — emotion + UI per device.

---

## Summary

| Question | Answer |
|----------|--------|
| Can this local server respond to multiple robots? | **Voice replies:** mostly yes. **Full NiNO (camera, alarms, greetings, motion):** no. |
| Why? | One `CAMERA_SOURCE`, one `ESP_PLAY_WAV_URL`, one identity session. |
| What to do (local)? | Add `device_id` + `devices.json` registry + `deliver_wav_to_device` / per-device `CameraStream`, then wire alarms and vision through them. |

---

*Document: multires.md — local multi-robot server plan, July 2026.*

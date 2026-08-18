# NiNO Home Bot — Team Report (Server, Models, Latency)

**For:** Engineering / product team  
**Date:** 2026-08-10  
**Scope:** PC FastAPI server, speech AI stack, ESP32-P4 integration, measured latency  
**Repo path:** `ESP-P4-UK-Demo Dont Open`  
**Also see:** `docs/SERVER_ARCHITECTURE.md`, `docs/SYSTEM_OVERVIEW.md`, `server/README.md`

---

## 1. Executive summary

NiNO is a **LAN home-robot demo**:

- **ESP32-P4** = wake word, mic, camera HTTP, speaker, eyes, servos  
- **PC Python FastAPI server** (`server/app.py`, port **8000**) = faces, ASR, LLM, TTS, memory, alarms  

There is **no NiNO cloud backend** in this repo. Cloud = optional APIs (**ElevenLabs** STT/TTS, AWS emotion, weather/football). The **LLM is local** (Ollama **`qwen2.5:1.5b`** on GPU when available).

| Layer | Current stack |
|-------|----------------|
| Board | ESP32-P4 — camera, USB 4-mic, speaker, touch, servos, OLED eyes |
| Server | FastAPI on PC (`0.0.0.0:8000`) |
| **ASR** | ElevenLabs **`scribe_v1`** → fallback Whisper **`tiny`** |
| **LLM** | Ollama **`qwen2.5:1.5b`** (GPU `:11435`) |
| **TTS** | ElevenLabs **`eleven_flash_v2_5`** → Piper → espeak |
| Faces | YuNet + SFace (local ONNX) |
| Emotion | AWS Rekognition + local DrGM |
| **Latency** | Typically **~2–3 s** server-side per voice reply (see §7) |

---

## 2. System block diagram

```text
┌────────────────────────────────────────────────────────────────────────────┐
│                         ESP32-P4 (on-device)                                 │
│  USB 4-mic → WakeNet "Hi ESP" → VAD → 16 kHz WAV                           │
│  Camera → HTTP /snapshot.jpg                                                │
│  Speaker ← /play_wav   Eyes ← /eye/expression   Servo ← /servo/360         │
│  mDNS _nino._tcp + UDP discover                                             │
└───────────────┬──────────────────────────────▲─────────────────────────────┘
                │ WS WAV / HTTP camera           │ WAV + HTTP commands
                ▼                                │
┌────────────────────────────────────────────────────────────────────────────┐
│                    PC Server — FastAPI (app.py :8000)                       │
│                                                                            │
│  Discovery → devices.json → Camera poll → Face (YuNet/SFace) → Web UI      │
│                                                                            │
│  Voice WS /voice-query:                                                    │
│      WAV → ASR → text → ROUTER → reply text → TTS → WAV                    │
│                       ├─ volume / alarm / weather / football / face-reg    │
│                       └─ open chat → local LLM (Ollama)                    │
│                                                                            │
│  Memory (PostgreSQL) · Alarms · latency_log.json                           │
└────────────────────────────────────────────────────────────────────────────┘
         │                        │                         │
         ▼                        ▼                         ▼
   ElevenLabs API          Ollama (local)            Open-Meteo /
   (STT + TTS)             qwen2.5:1.5b              API-Football
                                                   AWS Rekognition
```

**Voice path (simple):**

```text
Mic WAV → ASR → Router → (shortcut OR LLM) → TTS → Speaker WAV
```

---

## 3. How a voice turn works (step by step)

1. User says **“Hi ESP”** (wake word on USB 4-mic — firmware).  
2. ESP beeps, runs VAD, builds **16 kHz mono WAV**.  
3. ESP opens WebSocket to PC: `ws://<PC_IP>:8000/voice-query`.  
4. **ASR** turns WAV → text (Scribe, or Whisper if cloud STT fails).  
5. **Router** chooses: volume / alarm / weather / football / face-reg / servo / identity… **or LLM**.  
6. **LLM** (if needed): Ollama `qwen2.5:1.5b` writes a short reply.  
7. **TTS** turns reply → 16 kHz WAV (ElevenLabs Flash, or Piper/espeak).  
8. Server returns JSON + WAV; ESP plays on speaker.  
9. Timings written to `server/data/latency_log.json`.

USB mic never talks to the server directly — only WAV over WebSocket.

---

## 4. Models in use

### 4.1 ASR (speech → text)

| Role | Engine | Model |
|------|--------|--------|
| Primary | ElevenLabs (cloud) | **`scribe_v1`** |
| Fallback | faster-whisper (local) | **`tiny`** (English) |

**Fallback trigger:** ElevenLabs STT **errors** only (network, timeout, HTTP 4xx/5xx, missing key, empty-text exception) — not “bad transcript quality”.  
Log: `ElevenLabs STT failed (...); falling back to Whisper.`  
Field: `stt_engine` = `elevenlabs` | `whisper`.

### 4.2 LLM (text → reply)

| Role | Engine | Model |
|------|--------|--------|
| Primary | Ollama local | **`qwen2.5:1.5b`** |
| Host | Prefer GPU | `http://127.0.0.1:11435` |
| LLM fallback | None | — |

Not every turn uses the LLM (alarms, weather, volume, etc. are deterministic).

### 4.3 TTS (text → speech)

| Role | Engine | Model / voice |
|------|--------|----------------|
| Primary | ElevenLabs (cloud) | **`eleven_flash_v2_5`**, voice `f1K8uOKtx0TAmtXBiLqx`, `pcm_16000` |
| Fallback 1 | Piper (local) | **`en_GB-southern_english_female-low.onnx`** |
| Fallback 2 | espeak-ng | system TTS |

### 4.4 Vision

| Task | Model |
|------|--------|
| Face detect | YuNet ONNX |
| Face match | SFace ONNX → `data/face_embeddings.json` |
| Emotion | AWS Rekognition / local DrGM |

---

## 5. Cloud vs local

| Piece | Where |
|-------|--------|
| LLM Qwen | **Local** Ollama |
| Whisper / Piper / espeak | **Local** |
| Faces | **Local** |
| ElevenLabs STT/TTS | **Cloud** |
| AWS Rekognition | **Cloud** (optional) |
| API-Football / Open-Meteo | **Cloud** |
| ESP ↔ PC | **LAN only** |
| `aws-ec2-instance/` | Ops helper only — not the app |

Fully local speech: `STT_PROVIDER=whisper`, `TTS_PROVIDER=piper`.

---

## 6. Connection time

| Step | Typical |
|------|---------|
| ESP joins Wi‑Fi | a few seconds |
| `voice connect <PC_IP> 8000` | near-instant |
| Discovery `GET /status` | ~1–2 s on LAN |
| Voice WebSocket open | milliseconds (usually **per utterance**) |

After Wi‑Fi + voice URL are set, the bot is ready. There is no long “session connect” before every demo — each question opens a short WS, gets a reply, closes.

Serial setup:

```text
voice connect <PC_LAN_IP> 8000
voice wake on
```

---

## 7. Latency (measured)

Source: `server/data/latency_log.json`  
Analysis date: **2026-08-10**  
Sample: **562** voice queries with timing fields.

### 7.1 Overall server processing (all reply paths)

| Metric | Min | **p50 (median)** | p90 | Max | Mean |
|--------|-----|------------------|-----|-----|------|
| **process_total_seconds** | 0.86 s | **1.91 s** | 4.81 s | 52.95 s | 2.72 s |
| server_total_seconds | 0.91 s | **2.09 s** | 4.83 s | 53.38 s | 2.89 s |
| **stt_seconds** | 0.75 s | **1.06 s** | 2.91 s | 33.11 s | 1.73 s |
| **reply_seconds** (LLM/route) | 0.00 s | **0.35 s** | 0.91 s | 45.52 s | 0.68 s |
| **tts_seconds** | 0.03 s | **0.20 s** | 0.58 s | 6.24 s | 0.30 s |

**Takeaway:** typical answer is about **2 seconds** on the server; **STT is usually the largest slice (~1.1 s)**.

### 7.2 LLM chat path only (`reply_path = llm`, n=407)

| Stage | Median (p50) | p90 |
|-------|--------------|-----|
| STT | **1.10 s** | 2.98 s |
| LLM / reply | **0.37 s** | 0.86 s |
| TTS | **0.23 s** | 0.61 s |
| **Total** | **2.01 s** | 4.07 s |

### 7.3 Docs baseline (target)

From `docs/context.md` / system overview (GPU Ollama + ElevenLabs STT):

| Stage | Typical target |
|-------|----------------|
| STT | ~0.9 s |
| LLM short | ~0.2–0.4 s |
| TTS | ~0.2–1.2 s |
| **Server total** | **~2–3 s** (with memory prompt: aim ≤ ~4.5 s) |

### 7.4 Engines observed in the latency log

| Field | Counts (timed queries) |
|-------|-------------------------|
| `stt_engine` | elevenlabs **445**, whisper **117** |
| `tts_provider` | piper **494**, elevenlabs **68** |
| Top `reply_path` | llm 407, goodbye 68, face_registration 31, … |

Historical runs often used **Piper TTS** even when STT was ElevenLabs (depends on `.env` / fallbacks at the time).

### 7.5 Example recent rows

| Timestamp | Heard | Total | STT | Reply | TTS |
|-----------|-------|-------|-----|-------|-----|
| 2026-08-10 20:28:57 | What is two plus two? | **1.96 s** | 1.29 s (elevenlabs) | 0.26 s | 0.41 s (elevenlabs) |
| 2026-08-10 20:28:38 | What is two plus two? | 7.60 s | 1.06 s | 0.30 s | **6.24 s** (TTS spike) |
| 2026-08-05 18:58:33 | Goodbye | 1.48 s | 0.98 s | 0.00 s | 0.50 s |

### 7.6 Latency diagram

```text
WAV arrives at server
   │
   ├─ STT     ~1.1 s  ████████████
   ├─ Reply   ~0.4 s  ████        (LLM or shortcut)
   └─ TTS     ~0.2 s  ██
   ─────────────────────────────
   Server total ~2.0 s median

User also waits for: wake beep + speaking + VAD + playback start
```

### 7.7 Connection vs latency (do not confuse)

- **Connect** = Wi‑Fi + `voice connect` → ready in seconds (Wi‑Fi-bound).  
- **Latency** = time from WAV on server → reply WAV ready ≈ **~2–3 s**.

---

## 8. Other server features (brief)

### Discovery
mDNS `_nino._tcp` + UDP → `GET /status` → `data/devices.json` → camera poll.

### Vision
Snapshot poll → YuNet/SFace → `/video_feed` → greetings via TTS → `POST /play_wav`.

### Alarms
Voice parse → `alarms.json` → scheduler → TTS → board; medical ack via mic or web.

### Memory (optional)
PostgreSQL when `DATABASE_URL` set — conversations, facts, daily summaries.

### Main APIs
| Path | Role |
|------|------|
| `GET /` | Web UI |
| `GET /video_feed` | Live annotated MJPEG |
| `GET /api/status` | Health JSON |
| `GET /api/devices` | Registry |
| `WS /voice-query` | Voice in/out |
| `POST /api/register` | Face enroll |

---

## 9. Key files

| Path | Role |
|------|------|
| `server/app.py` | Entry, HTTP/WS |
| `server/voice_service.py` | STT → route → TTS |
| `server/llm_service.py` | Ollama |
| `server/tts_service.py` | TTS + greetings |
| `server/face_service.py` | Faces |
| `server/data/latency_log.json` | Measured timings |
| `server/.env` | STT/TTS/LLM keys & providers |

---

## 10. How to run

```bash
cd server
source .venv/bin/activate
python app.py --host 0.0.0.0 --port 8000
```

Open: **http://localhost:8000**  
Status: **http://localhost:8000/api/status**

### Test without ESP
- Unit tests: `python -m unittest discover -v -p 'test_*.py'`  
- LLM smoke: `ollama_generate("single prompt string")`  
- Voice: send WAV to `ws://127.0.0.1:8000/voice-query`, play reply on PC  

### LLM smoke (correct API — one prompt string)

```bash
cd server && source .venv/bin/activate
python - <<'PY'
from llm_service import ollama_generate
print(ollama_generate(
    "You are NiNO. Reply in one short sentence.\n\nUser: Who are you?\nNiNO:"
))
PY
```

---

## 11. One-page quick reference

```text
ESP (wake + mic + cam + speaker)
        │
        ▼
PC FastAPI :8000
   ASR:  scribe_v1  →  Whisper tiny
   Brain: Router + qwen2.5:1.5b (local GPU)
   TTS:  eleven_flash_v2_5  →  Piper  →  espeak
   Vision: YuNet + SFace
        │
        ▼
Reply on speaker

Connect:  Wi‑Fi + voice connect  (seconds)
Latency:  ~2 s median server  |  ~2–3 s typical  |  STT ~1.1 s largest slice
```

---

## 12. Sharing this doc

**Primary copy (in repo):**

`docs/NINO_SERVER_TEAM_REPORT.md`

**Desktop copy (easy to attach/email):**

`~/Desktop/NINO_SERVER_TEAM_REPORT.md`

You can paste into Google Docs / Notion / email as-is.

---

*End of report.*

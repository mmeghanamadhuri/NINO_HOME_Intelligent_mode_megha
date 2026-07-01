# NiNO Server — Implemented Architecture vs `context.md`

This document describes **what is running today** on the Python server, how it maps to the target design in `[context.md](context.md)`, and what is still missing for the full **Vision + Voice Memory Workflow**.

---

## Executive summary


| Area                 | `context.md` target                                    | Current implementation                                                    |
| -------------------- | ------------------------------------------------------ | ------------------------------------------------------------------------- |
| Face recognition     | SFace                                                  | **Implemented** — SFace + YuNet on PC                                     |
| STT                  | ElevenLabs                                             | **Implemented** — ElevenLabs Scribe + Whisper fallback                    |
| LLM                  | Qwen                                                   | **Implemented** — Qwen via **Ollama** (`qwen2.5:1.5b`)                    |
| TTS                  | ElevenLabs                                             | **Partial** — ElevenLabs when keyed; else SAPI (Windows) / espeak (Linux) |
| Database             | PostgreSQL (users, conversations, memories, summaries) | **Not implemented** — JSON files + in-memory session only                 |
| Long-term memory     | Extract, score, retrieve memories                      | **Not implemented**                                                       |
| Conversation history | Persist every exchange                                 | **Not implemented** (latency log only)                                    |
| Daily summaries      | Nightly per-user rollup                                | **Not implemented**                                                       |
| Context injection    | Memories + history + summary → Qwen prompt             | **Partial** — name only, session-scoped                                   |
| Device               | ESP32-P4                                               | **Implemented** — voice WS, `/play_wav`, camera stream, servo             |


Most of the **real-time pipeline** (camera → face → voice → LLM → TTS → ESP) is up. The **persistent memory layer** from `context.md` has not been built yet.

---

## Current server architecture (as implemented)

```text
ESP32-P4 (firmware)
  ├── UVC camera ──────────────────────────► GET /stream  (MJPEG)
  ├── Wake word + VAD + mic ───────────────► WS /voice-query  (16 kHz WAV in/out)
  ├── Speaker ◄──────────────────────────── POST /play_wav  (TTS, alarms, greetings)
  └── Servo ◄────────────────────────────── POST /servo/360  (voice-triggered spin)

PC — FastAPI server (server/app.py)
  │
  ├── CameraStream (camera.py)
  │     └── HTTP MJPEG from ESP or local webcam
  │
  ├── FaceService (face_service.py)
  │     ├── YuNet face detection + SFace 128-D embeddings
  │     ├── Storage: server/data/faces/*.jpg, face_embeddings.json
  │     └── Session hold (~90 s) for primary viewer identity
  │
  ├── TTSService (tts_service.py)  [background worker thread]
  │     ├── Vision greetings when face recognized (Ollama greeting_for_face)
  │     └── POST synthesized WAV → ESP /play_wav
  │
  ├── Voice WebSocket pipeline (app.py → voice_service.py)
  │     ├── STT: ElevenLabs Scribe OR faster-whisper
  │     ├── Routing: volume / alarm / servo / identity / general LLM
  │     ├── LLM: Ollama answer_voice_query / answer_identity_question
  │     ├── TTS: synthesize_sapi_wav_bytes → resample 16 kHz
  │     └── Return WAV over WebSocket to ESP
  │
  ├── AlarmService (alarm_service.py + alarm_*.py)
  │     ├── Voice-set reminders (regex + Ollama NLP fallback)
  │     ├── Storage: server/data/alarms.json
  │     └── Fire at scheduled time → TTS (+ beep) → ESP /play_wav
  │
  ├── LLMService (llm_service.py)
  │     └── Ollama HTTP API (GPU :11435 preferred on Linux)
  │
  └── Observability
        └── server/data/latency_log.json (STT/LLM/TTS timings, not conversation DB)
```

### Request flow — voice assistant (live path)

```text
User says "Hi ESP" on board
        │
        ▼
ESP captures WAV → WebSocket /voice-query → app._voice_ws_pipeline()
        │
        ├── Resolve viewer: live camera + latest_results + TTS state + 15 min TTL memory
        ├── Camera identity snapshot (5-frame vote for "who am I?")
        │
        ▼
voice_service.process_voice_wav()
        │
        ├── transcribe_wav()          → ElevenLabs or Whisper
        ├── apply_volume_command()    → ESP /speaker/volume (no LLM)
        ├── handle_alarm_voice()      → alarm_voice.py (set/list/cancel/ack)
        ├── is_servo_360_command()    → fixed TTS + delayed POST /servo/360
        ├── is_identity_question()    → answer_identity_question() + camera context
        └── else                      → answer_voice_query() (~18% name personalization)
        │
        ▼
synthesize_sapi_wav_bytes() → resample 16 kHz → send_bytes() to ESP
        │
        └── append latency_log.json record
```

### Request flow — vision greeting (parallel path)

```text
MJPEG /video_feed loop
        │
        ▼
faces.annotate(frame) → recognized primary viewer
        │
        ▼
tts.update_face_state() → enqueue greeting job
        │
        ▼
greeting_for_face() via Ollama → TTS → POST ESP /play_wav
```

This path is **session-only**: it knows “return visitor this session” via `_known_seen_once`, not “yesterday we discussed Mars.”

---

## Component mapping: `context.md` → this repo

### Face recognition — **implemented (with differences)**


| `context.md`                           | Implemented                                                                                                            |
| -------------------------------------- | ---------------------------------------------------------------------------------------------------------------------- |
| SFace recognition                      | `face_service.py` — OpenCV Zoo SFace (`face_recognition_sface_2021dec.onnx`)                                           |
| `face_id` in PostgreSQL                | **person_id** derived from display name; stored in `face_embeddings.json` + JPEG crops under `data/faces/<person_id>/` |
| User table (`first_seen`, `last_seen`) | **Not persisted** — runtime session fields only (`_session_primary_name`, TTS `_known_seen_once`)                      |


Recognition runs on the **PC server**, not on the ESP. The ESP only streams video and plays audio.

### Speech-to-text — **implemented (with differences)**


| `context.md`    | Implemented                                                                                                               |
| --------------- | ------------------------------------------------------------------------------------------------------------------------- |
| ElevenLabs STT  | `voice_service._transcribe_elevenlabs()` — Scribe `scribe_v1`                                                             |
| Single provider | **Dual**: ElevenLabs default when API key present; **Whisper** (`faster-whisper`) as fallback or `--stt-provider whisper` |


### LLM — **implemented (with differences)**


| `context.md`            | Implemented                                                                   |
| ----------------------- | ----------------------------------------------------------------------------- |
| Qwen                    | **Qwen 2.5 1.5B** via Ollama (`OLLAMA_MODEL=qwen2.5:1.5b`)                    |
| Direct Qwen API         | **Ollama** `/api/generate` — local, GPU-accelerated on DGX (`llm_service.py`) |
| Memory-enriched prompts | **Not yet** — prompts include viewer name and camera identity only            |


### Text-to-speech — **partially aligned**


| `context.md`        | Implemented                                                                  |
| ------------------- | ---------------------------------------------------------------------------- |
| ElevenLabs TTS only | ElevenLabs when `ELEVENLABS_API_KEY` set                                     |
| Stream to ESP32-P4  | WAV bytes over WebSocket (voice) or HTTP POST `/play_wav` (greetings/alarms) |
| —                   | **Also**: Windows SAPI, Linux espeak-ng fallback (`tts_service.py`)          |


Voice replies and vision greetings both target **16 kHz mono PCM** for the ESP speaker path.

### Database / persistence — **not implemented**

`context.md` defines four PostgreSQL tables. **None exist in the current server.**


| Table           | Purpose in `context.md`                | Current equivalent                                                            |
| --------------- | -------------------------------------- | ----------------------------------------------------------------------------- |
| `users`         | Registered people, face_id, timestamps | `face_embeddings.json` + `data/faces/` (no timestamps, no face_id column)     |
| `conversations` | Every user/assistant exchange          | **Missing** — only debug fields in `latency_log.json` (`heard`, `reply_text`) |
| `memories`      | Long-term facts + importance           | **Missing**                                                                   |
| `summaries`     | Daily conversation rollup              | **Missing**                                                                   |


Existing JSON persistence:

```text
server/data/
  face_embeddings.json   # SFace vectors per person
  faces/<id>/*.jpg       # Registration crops
  alarms.json            # Scheduled reminders (separate feature)
  latency_log.json       # Performance telemetry (not a memory store)
```

### Context builder & memory injection — **not implemented**

`context.md` Step 4 loads recent conversations, important memories, and latest daily summary, then builds a structured prompt.

**Today**, LLM prompts are built in `llm_service.py`:

- `answer_voice_query()` — optional viewer name (~18% of replies via `VOICE_PERSONALIZE_PROB`)
- `answer_identity_question()` — live camera recognition state
- `greeting_for_face()` — first vs return visitor **this session only**

There is **no** second LLM call for memory extraction, **no** importance scoring, **no** daily summary job, **no** pgvector search.

---

## Features in production **beyond** `context.md`

The live server includes capabilities not described in the memory workflow doc:


| Feature                      | Module(s)                                            | Notes                                              |
| ---------------------------- | ---------------------------------------------------- | -------------------------------------------------- |
| Alarm scheduler              | `alarm_service.py`, `alarm_voice.py`, `alarm_nlp.py` | Voice + web UI; medical P0 with yes/no ack         |
| Servo 360 voice command      | `voice_service.py`                                   | Fixed TTS → `POST /servo/360`                      |
| Speaker volume voice control | `voice_service.py`                                   | ESP `/speaker/volume`                              |
| Latency observability        | `app.py`                                             | `GET /api/latency-log`                             |
| GPU Ollama auto-select       | `llm_service.py`, `scripts/start_ollama_gpu.sh`      | Port 11435 on Linux                                |
| Web UI                       | `templates/`, `static/`                              | Face register/retrain, alarm management, live feed |
| Touch-priority audio         | ESP firmware                                         | Not server-side; documented in README              |


---

## Development phases: `context.md` vs status


| Phase | `context.md`                    | Status                                             |
| ----- | ------------------------------- | -------------------------------------------------- |
| 1     | Create PostgreSQL tables        | **Not started**                                    |
| 2     | Store all conversations         | **Not started** (latency log ≠ conversation store) |
| 3     | Memory extraction               | **Not started**                                    |
| 4     | Daily summaries                 | **Not started**                                    |
| 5     | Context retrieval service       | **Not started**                                    |
| 6     | Inject memory into Qwen prompts | **Partial** — name + session greeting only         |
| 7     | pgvector semantic search        | **Not started**                                    |


### What *is* done (foundation for memory work)

- End-to-end voice pipeline (ESP ↔ server ↔ Ollama ↔ TTS)
- Stable user identification via SFace (display name as logical user key)
- Viewer resolution chain for voice (`_viewer_for_voice_query()`)
- LLM abstraction ready for richer system prompts (`llm_service.ollama_generate`)
- Operational logging to extend into conversation persistence

---

## Gap analysis — what to build next

To reach the **Expected End Result** in `context.md` (“Welcome back Karthik. Yesterday we discussed Mars…”), implement in roughly this order:

### 1. PostgreSQL layer

Add a `memory_service.py` (or similar) with:

- Connection pool / env vars (`DATABASE_URL`)
- Migrations for `users`, `conversations`, `memories`, `summaries`
- Map SFace `person_id` / display name → `users.face_id` on first recognition

### 2. Conversation logging

After each successful `process_voice_wav()` LLM path:

```sql
INSERT INTO conversations (user_id, user_text, assistant_text) ...
```

Resolve `user_id` from recognized viewer name; skip or use anonymous row when unknown.

### 3. Context retrieval + prompt injection

Before `answer_voice_query()` / `greeting_for_face()`:

- Load last N conversations, top memories by importance, latest summary
- Append to prompt template (as in `context.md` Context Builder section)
- Wire through `voice_service.process_voice_wav()` and optionally vision greetings

### 4. Memory extraction (async)

Post-reply background task:

- Second Ollama call with extraction prompt → JSON memories
- Filter by importance threshold (e.g. ≥ 5)
- `INSERT INTO memories`

### 5. Daily summary cron

Once per day per active user:

- Aggregate that day’s `conversations`
- Ollama summarize → `INSERT INTO summaries`

### 6. (Later) pgvector

Add embedding column to `memories`; semantic retrieval when user query does not mention stored keywords.

---

## Configuration reference (implemented server)

Key files and env vars today:


| Item        | Location / default                                                                      |
| ----------- | --------------------------------------------------------------------------------------- |
| Entry point | `server/app.py`                                                                         |
| Config file | `server/server_config.json` (`camera_source`, `esp_play_wav_url`, `elevenlabs_api_key`) |
| Face data   | `server/data/face_embeddings.json`, `server/data/faces/`                                |
| Alarms      | `server/data/alarms.json`                                                               |
| Latency     | `server/data/latency_log.json`                                                          |
| Ollama      | `OLLAMA_URL` (auto → `:11435` GPU), `OLLAMA_MODEL=qwen2.5:1.5b`                         |
| STT         | `STT_PROVIDER`, `ELEVENLABS_API_KEY`, `WHISPER_MODEL`                                   |
| TTS         | `TTS_PROVIDER`, ElevenLabs voice settings                                               |
| Voice WS    | `/ws/voice`, `/voice-query`                                                             |


Full runbook: see [README.md](README.md).

---

## Side-by-side workflow comparison

### `context.md` — target final workflow

```text
Camera → SFace → Identify User → Load Memories → Load Recent Conversations
  → Load Daily Summary → Build Context → Qwen → TTS → ESP
  → Store Conversation → Extract Memories → Update PostgreSQL → Daily Summary
```

### Current — production workflow

```text
Camera → SFace → Session viewer name → (optional name in prompt)
  → Ollama → TTS → ESP
  → latency_log.json only

Parallel: Vision greet (session return-visitor flag, no DB)
Parallel: Alarms → alarms.json → scheduled TTS to ESP
```

---

## Summary

The NiNO server **already implements** the real-time vision + voice stack described at a high level in `context.md`: SFace on the PC, ElevenLabs/Whisper STT, Qwen via Ollama, TTS to ESP32-P4, and reliable user identification for personalization and identity questions.

The **memory workflow** — PostgreSQL, conversation history, long-term memory extraction, daily summaries, and rich context injection — remains **design-only** in `context.md` and is the main delta to close for a persistent companion experience across days.

Use `**context.md`** as the product spec for memory. Use `**context_main.md**` (this file) as the map of what is live today and what to implement next.
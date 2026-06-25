# Emotion Recognition — Implementation Summary

This document describes **what was built** for camera-based emotion recognition on the NiNO Python server (June 2026). It complements:

- [`emotion-detect.md`](emotion-detect.md) — original design vs present server comparison
- [`serverP.md`](serverP.md) — overall server architecture
- [`Eye states.md`](Eye%20states.md) — ESP OLED eye states

---

## Goal

When a **registered person** stands in front of the camera:

1. Detect their facial emotion (FER+ CNN on face crop).
2. Stabilize the reading for **~2.0–2.5 seconds**.
3. Generate an **empathetic spoken reply** via Ollama (e.g. *“You look a bit down today, Chakri”*).
4. Send **TTS + eye expression tag** to the ESP speaker/OLED.
5. Never collide with the **voice WebSocket pipeline** (voice = P0, vision emotion = P1).

---

## Architecture

```mermaid
flowchart TB
    subgraph P1["P1 — Vision emotion (passive)"]
        CAM[ESP snapshot / MJPEG loop]
        FACE[YuNet + SFace face recognition]
        FER[emotion_service.py FER+ ONNX]
        ACC[vision_emotion_service.py accumulate 2–2.5 s]
        LLM[llm_service.empathy_for_detected_emotion]
        TTS[tts_service.speak_to_esp]
        ESP1[POST /play_wav + X-Nino-Eye-Expression]
        CAM --> FACE --> FER --> ACC --> LLM --> TTS --> ESP1
    end

    subgraph P0["P0 — Voice query (always wins)"]
        WS[/ws/voice]
        STT --> VOICE --> EYE2[eye_expression.py from reply text]
        WS --> STT
        EYE2 --> ESP2[WebSocket JSON eye_expression + WAV]
    end

    PRI[pipeline_priority.py] -.->|blocks P1 during/after voice| ACC
```

| Priority | Pipeline | Trigger | Eye tag delivery |
|----------|----------|---------|------------------|
| **P0** | Voice WebSocket | User speaks to ESP | JSON `eye_expression` + binary WAV |
| **P1** | Vision emotion | Registered face + stable emotion in frame | HTTP `POST /play_wav` header `X-Nino-Eye-Expression` |

---

## Files added or changed

| File | Role |
|------|------|
| `server/emotion_service.py` | **New** — FER+ model load, face crop, inference, neutral suppression, eye-tag mapping |
| `server/vision_emotion_service.py` | **New** — 2–2.5 s accumulation, empathy job queue, MJPEG overlay state |
| `server/pipeline_priority.py` | **New** — P0/P1 coordination; voice blocks vision during query + cooldown |
| `server/llm_service.py` | Added `empathy_for_detected_emotion()` |
| `server/esp_playback.py` | Added optional `eye_expression` → `X-Nino-Eye-Expression` header |
| `server/tts_service.py` | `speak_to_esp()`, `is_speaking()`; disables generic greetings when vision emotion on |
| `server/app.py` | Wires services; MJPEG overlay; voice priority hooks; `/api/status` fields |
| `server/test_vision_emotion.py` | **New** — unit tests |
| `server/requirements.txt` | Added `onnxruntime` |
| `server/.env.example` | Vision emotion tuning vars documented |
| `server/static/app.js` | Stream auto-reconnect on `/video_feed` error |
| `server/data/models/emotion-ferplus-8.onnx` | Auto-downloaded FER+ model (~35 MB) |
| `docs/emotion-detect.md` | **New** — design vs implementation diff |
| `docs/EMOTION_RECOGNITION.md` | **This file** |

---

## Emotion model

| Item | Detail |
|------|--------|
| Model | **FER+** `emotion-ferplus-8.onnx` (ONNX Model Zoo) |
| Input | 64×64 grayscale (design doc showed 48×48; FER+ standard is 64×64) |
| Classes | `neutral`, `happy`, `surprise`, `sad`, `anger`, `disgust`, `fear`, `contempt` |
| Runtime | **ONNX Runtime** (`CPUExecutionProvider` on aarch64 DGX; CUDA if available) |
| Preprocessing | Square padded face crop, CLAHE contrast, softmax on logits |

### Speakable vs ignored

**Speakable** (can trigger empathy + eyes): `happy`, `surprise`, `sad`, `anger`, `fear`

**Ignored for empathy** (overlay only): `neutral`, `disgust`, `contempt`

### Neutral suppression

FER+ often returns **~75% neutral** on the ESP camera feed. When a speakable class is strong enough (e.g. sad 21% vs neutral 75%), the effective label is promoted from neutral to that emotion. Tunables:

- `EMOTION_NEUTRAL_SUPPRESS_RATIO` (default `0.22`)
- `EMOTION_SPEAKABLE_MIN` (default `0.12`)

### Eye expression mapping

| Detected | ESP eye tag |
|----------|-------------|
| happy | `happy` |
| surprise | `surprised` |
| sad | `sad` |
| anger | `sad` |
| fear | `surprised` |
| neutral / disgust / contempt | none (idle) |

Uses the same six tags as `eye_expression.py` for voice replies.

---

## Vision emotion workflow (step by step)

1. **MJPEG loop** (`app.py` → `_mjpeg_generator`) reads frames from ESP snapshot polling.
2. **Face recognition** (`face_service.py`) — YuNet + SFace; primary face selected.
3. **Emotion inference** every frame on primary face bbox (`emotion_service.detect_overlay` / `detect`).
4. **Accumulation** — same recognized person must stay in frame:
   - Min **2.0 s** (`VISION_EMOTION_WINDOW_MIN_S`)
   - Max **2.5 s** (`VISION_EMOTION_WINDOW_MAX_S`)
   - Dominant speakable emotion must reach **35%** of frames (`VISION_EMOTION_DOMINANCE`)
5. **Job queued** → background worker calls Ollama empathy prompt.
6. **TTS** → `POST ESP_PLAY_WAV_URL` with optional eye header.
7. **Cooldown** — same person not spoken to again for **120 s** (`VISION_EMOTION_COOLDOWN_S`).

Face gating accepts **stabilized** recognition or **strong candidate** (score ≥ soft threshold) so emotion runs even before full stabilization.

---

## LLM empathy prompt

Function: `empathy_for_detected_emotion()` in `llm_service.py`

Internal context sent to Ollama:

> *person detected is {name} and emotion is {emotion_spoken}*

Output: 1–2 short spoken sentences, empathetic, uses the person’s name (e.g. *“You look a bit down today, Chakri”*).

---

## Priority / no collision

`pipeline_priority.py`:

| Event | Effect on P1 |
|-------|----------------|
| Voice WebSocket utterance starts | `begin_voice_query()` — P1 blocked |
| Voice reply sent (or failed) | `end_voice_query()` — starts cooldown |
| After voice | `VISION_EMOTION_AFTER_VOICE_SECONDS` (default **90 s**) — P1 blocked |
| TTS busy | `tts.is_speaking()` — P1 deferred |

Generic face **greetings** are disabled when `VISION_EMOTION_ENABLED=1` (default) so vision empathy replaces them.

---

## MJPEG overlay (live feed)

Pink text at bottom of `/video_feed` (example):

```text
sad 21% raw=neutral 6.8s n:75% s:21% h:2%
```

| Part | Meaning |
|------|---------|
| `sad` | Effective emotion (after neutral suppression) |
| `21%` | Confidence of effective emotion |
| `raw=neutral` | Raw FER+ top class |
| `6.8s` | Seconds this face has been accumulated |
| `n:75% s:21% h:2%` | Top-3 class scores (neutral, sad, happy) |

---

## Configuration

All `*_S` variables are **seconds**, not milliseconds.

| Variable | Default | Purpose |
|----------|---------|---------|
| `VISION_EMOTION_ENABLED` | `1` | Master switch |
| `EMOTION_DEVICE` | `auto` | `auto` / `cuda` / `cpu` |
| `VISION_EMOTION_WINDOW_MIN_S` | `2.0` | Min time in frame before empathy |
| `VISION_EMOTION_WINDOW_MAX_S` | `2.5` | Max wait for stable emotion |
| `VISION_EMOTION_COOLDOWN_S` | `120` | Seconds before same person gets empathy again |
| `VISION_EMOTION_AFTER_VOICE_SECONDS` | `90` | Pause P1 after voice query |
| `VISION_EMOTION_DOMINANCE` | `0.35` | Fraction of frames that must agree |
| `EMOTION_MIN_CONFIDENCE` | `0.12` | Min confidence for speakable class |
| `EMOTION_NEUTRAL_SUPPRESS_RATIO` | `0.22` | Sad/etc. must reach this fraction of neutral to win |
| `EMOTION_SPEAKABLE_MIN` | `0.12` | Absolute min score for speakable class |

Copy relevant lines into `server/.env` and restart the server.

---

## API / monitoring

`GET /api/status` includes:

```json
{
  "emotion": { "available": true, "provider": "CPUExecutionProvider", "model": "emotion-ferplus-8.onnx" },
  "vision_emotion": { "enabled": true, "latest_overlay": { "name": "Chakri", "emotion": "sad", ... } },
  "pipeline_priority": { "voice_active": false, "vision_blocked": false }
}
```

---

## GPU note

On **aarch64** (DGX Spark), `onnxruntime-gpu` is not available via pip. Emotion inference runs on **CPU** — acceptable for 64×64 FER+. Ollama LLM still uses GPU separately.

`EMOTION_DEVICE=auto` will use CUDA if a CUDA-enabled ORT build is installed later.

OpenCV DNN CUDA fallback exists if ORT is unavailable.

---

## Firmware status

| Path | Eye animation |
|------|---------------|
| Voice WebSocket | **Works** — `eye_expression` in JSON metadata |
| Vision empathy `POST /play_wav` | **Needs firmware** — server sends `X-Nino-Eye-Expression: sad` but ESP `play_wav_handler` in `main/main.c` currently ignores it and plays with idle eyes |

**Required ESP change:** read `X-Nino-Eye-Expression` in `play_wav_handler`, map with `nino_eye_state_from_name()`, pass to `nino_audio_queue_wav()`.

Audio for vision empathy works today; OLED eyes stay idle until firmware is updated.

---

## Bugs fixed during implementation

| Issue | Cause | Fix |
|-------|-------|-----|
| Live feed black / broken image | `TypeError` in `emotion_service.detect()` (`float` vs `"0.12"` string) crashed `/video_feed` on first face | Fixed `_env_float` defaults; wrapped emotion in try/except in MJPEG loop |
| Bot never spoke | FER+ always ~75% neutral; neutral not speakable | Neutral suppression + lower `EMOTION_MIN_CONFIDENCE` |
| Emotion never accumulated | Required fully stabilized face only | Accept strong `candidate_name` from face matcher |
| Overlay stuck on neutral | Showed raw top class only | Overlay shows effective emotion + raw + score breakdown |
| Stream disconnect | MJPEG generator died on emotion error | try/except + `app.js` stream reconnect |

---

## Tests

```bash
cd server
.venv/bin/python -m unittest test_vision_emotion.py -v
```

Covers: FER+ label mapping, neutral suppression, primary-face selection, pipeline priority, accumulation → empathy job.

---

## Related docs

| Doc | Content |
|-----|---------|
| [`emotion-detect.md`](emotion-detect.md) | Original flowchart vs what we built / gaps |
| [`nino_emotion_flow.png`](nino_emotion_flow.png) | Original design diagram |
| [`Eye states.md`](Eye%20states.md) | All 9 OLED eye states on ESP |

---

## Not implemented (future)

- Camera emotion **database** / persistence (design had “Camera Emotion DB”)
- Passive **continuous eye mirroring** without speech (eyes idle between empathy clips)
- Combined single ESP command for WAV + tag (today: WebSocket vs HTTP split)
- `onnxruntime-gpu` on aarch64 when NVIDIA publishes a wheel
- Firmware `X-Nino-Eye-Expression` handler on ESP

---

*Last updated: June 2026 — vision emotion P1 pipeline on Python server.*

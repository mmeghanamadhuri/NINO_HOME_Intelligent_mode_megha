# ADVA — Adaptive End-of-Utterance for NiNO

**Adaptive VAD / Adaptive Endpointer** — does the idea fit our stack, and what should we build without overcomplicating?

Detailed build plan (Option A): **[ADVA_PLAN.md](ADVA_PLAN.md)**

---

## Verdict

**Yes, it fits — but only as a thin layer on top of our existing energy VAD.**

| Question | Answer |
|----------|--------|
| Do we need a new VAD / VADNet / P4 hardware VAD? | **No** |
| Do we need Whisper-partial / grammar endpointer now? | **No** (later, optional) |
| What should we build? | **Adaptive trailing-silence timeout** inside `voice_assist.c` |
| Where does it live? | Firmware only — same capture → WAV → WebSocket path |

Recommended name for the feature: **Adaptive End-of-Utterance (EOU)** — not “new VAD.”  
VAD still answers “is this frame speech?” EOU answers “is the whole utterance done?”

---

## How our architecture works today

```text
USB 4-mic (16 kHz mono)
        │
        ▼
┌───────────────────┐
│ voice_wake.cpp    │  WakeNet "Hi ESP" via AFE (vad_init = false)
└─────────┬─────────┘
          │ after wake: pause wake feed, exclusive mic
          ▼
┌───────────────────┐
│ voice_assist.c    │  Custom energy VAD → WAV in RAM
│                   │  Fixed: VAD_TRAILING_SILENCE_MS = 450
└─────────┬─────────┘
          │ WebSocket binary WAV
          ▼
┌───────────────────┐
│ PC FastAPI        │  STT (ElevenLabs / Whisper) → LLM → TTS
└───────────────────┘
```

Important facts from the current code/docs:

1. **Query capture is not ESP-SR VADNet.**  
   Wake AFE is wake-only (`afe_cfg->vad_init = false`). Capture uses a hand-rolled energy VAD in `nino_voice_capture_vad_wav()`.

2. **End-of-speech is a fixed 450 ms trailing silence.**  
   That is aggressive for people who pause mid-sentence (“can you… [pause] …tell me the weather”).

3. **The board never sees text during capture.**  
   STT runs on the PC *after* the full WAV is sent. Partial-transcript endpointer is not available on-device without a redesign.

4. **Mic is exclusive during VAD.**  
   Wake feed is paused; one reader. Any EOU logic must stay inside the existing `voice_assist.c` loop — do not add a second audio consumer.

5. **Same VAD path serves medical ack / prompt-ack re-listen.**  
   Changes must stay simple and shared.

So: adaptive *endpointer* on the existing energy VAD is a natural fit. Replacing VAD with VADNet or adding streaming STT for grammar would fight the current design.

---

## Options compared

### Option A — Adaptive trailing silence (recommended)

Keep energy thresholds as they are. Only change **how long** silence must last before we stop recording.

```text
speech frame?  →  existing energy VAD (unchanged)
silence frames →  compare against adaptive_timeout_ms
                     (clamped, e.g. 500–1500 ms)
```

**Session adaptation (simple):**

- While recording, every time we see `speech → silence → speech`, record that pause length.
- After a few intra-utterance pauses, raise the end timeout toward those pauses + a margin.
- Clamp hard: never below ~500 ms, never above ~1500–1800 ms.

**Why this wins for NiNO**

- Touches **one file** (`voice_assist.c`) and a few `#define`s.
- No AFE / ESP-SR / server / WebSocket changes.
- Same WAV → WS → STT pipeline.
- Solves the real pain: mid-sentence gaps cutting off early.
- Hard clamps prevent “robot waits forever.”

**Effort:** small. **Risk:** low. **Complexity:** low.

---

### Option B — Switch capture to ESP-SR VADNet / P4 HW VAD

Use neural or hardware VAD for speech/noise, then still need an endpointer for “utterance done.”

**Why it does not suit us now**

- Wake already owns AFE in wake-only mode; capture is a separate USB-mic energy path.
- Would require feeding capture audio through AFE VAD, retuning, and careful exclusivity with wake.
- VADNet still needs a silence/end policy — it does not magically fix pause-y speakers by itself.
- More moving parts for little gain over Option A.

**Verdict:** skip for now. Revisit only if energy VAD is unreliable in noise after USB 4-mic tuning.

---

### Option C — Persistent speaker pause profile

Across sessions: store `avg_pause_ms`, `max_pause_ms` in NVS / per face identity.

**Why not first**

- Needs identity link (face) or shared global profile — wrong for multi-user home until identity is always correct.
- Easy to overfit one user’s style onto another.
- Extra state + reset/tuning UX for little benefit over **session-only** adaptation.

**Verdict:** Phase 2, optional. Prefer session EMA first.

---

### Option D — Whisper / STT partial + “incomplete sentence”

Keep listening if partial text looks unfinished (“Can you tell me the…”).

**Why not now**

- Our board sends **one complete WAV** after EOU; STT is on the PC afterward.
- Needs streaming STT (or chunked upload + early partials), more WS protocol, and more RAM/latency complexity.
- Powerful later; wrong as the first step.

**Verdict:** Phase 3 / future. Only after Option A is stable.

---

## Recommended design (Option A)

### Pipeline (unchanged shape)

```text
USB mic
  → energy VAD (speech / not speech)     ← keep
  → Adaptive EOU (timeout from pauses)   ← add
  → WAV
  → WebSocket
  → STT / LLM / TTS
```

### State machine (conceptual)

```text
LISTEN
  │ energy ≥ start_th for N frames
  ▼
SPEECH
  │ energy drops below end_th
  ▼
PAUSE
  ├─ speech resumes before timeout
  │     → record pause_ms into session stats
  │     → maybe raise adaptive_timeout
  │     → back to SPEECH
  │
  └─ silence ≥ adaptive_timeout
        AND min speech length met
        → END → finalize WAV
```

### Suggested constants (starting point)

| Parameter | Suggested | Notes |
|-----------|-----------|--------|
| `EOU_MIN_MS` | 500 | Floor — still snappy for short commands |
| `EOU_MAX_MS` | 1500 | Ceiling — robot never feels stuck |
| `EOU_DEFAULT_MS` | 700–800 | Replace today’s 450 as baseline (450 cuts pause-y speech) |
| `EOU_MARGIN_MS` | 300–400 | Added on top of observed intra-pauses |
| Max capture | keep ~10 s | Existing `VOICE_QUERY_VAD_MAX_SEC` |

**Adaptive rule (simple):**

```text
adaptive_timeout =
  clamp(
    max(EOU_DEFAULT_MS, recent_max_intra_pause_ms + EOU_MARGIN_MS),
    EOU_MIN_MS,
    EOU_MAX_MS
  )
```

Use only pauses **inside** the current utterance (`speech → silence → speech`). Do not grow timeout from the final silence.

### What not to change

- WakeNet / AFE wake path  
- USB mic ring / exclusivity (`mic_capture_hold`)  
- WebSocket WAV contract  
- Server STT / LLM  
- Peak-ratio silence energy logic (`VAD_SILENCE_PEAK_RATIO_PCT`) — that decides *frame is silence*; EOU only decides *how many silence frames*

---

## Fit checklist

| Architecture constraint | Option A | B VADNet | D STT-partial |
|-------------------------|----------|----------|---------------|
| Fits exclusive USB mic + wake pause | Yes | Harder | Yes (server-heavy) |
| Fits “WAV then WS” contract | Yes | Yes | Needs protocol change |
| Fits medical / prompt-ack re-listen | Yes (same fn) | Extra work | N/A |
| Complexity | Low | Medium–high | High |
| Fixes mid-sentence gaps | Yes | Partial | Best long-term |

---

## Implementation sketch (firmware only)

All logic stays in `main/voice_assist.c` inside `nino_voice_capture_vad_wav()`:

1. Replace fixed `VAD_SILENCE_STOP_FRAMES` check with `silence_streak * 20 >= adaptive_timeout_ms`.
2. Track `in_pause`, `pause_started_ms`, `max_intra_pause_ms` for the current capture.
3. On silence → speech resume: update `max_intra_pause_ms`, recompute `adaptive_timeout_ms`.
4. Log: `EOU adaptive=%u ms intra_max=%u` so we can tune on device.
5. Keep max duration and min speech length as hard stops.

No new tasks, no new modules, no server PR required for v1.

---

## Rollout plan

| Phase | What | When |
|-------|------|------|
| **v1 (now)** | Option A — session adaptive timeout + clamps; bump default from 450 → ~700–800 | First change |
| **v1.1** | Tune min/max from real users (fast talkers vs pause-y) | After a few days of logs |
| **v2 (optional)** | Per-face pause profile if identity is stable | Only if v1 still cuts people off |
| **v3 (optional)** | Streaming STT / incomplete-sentence EOU | Only if product needs near-human turn-taking |

---

## Summary

| | |
|--|--|
| **Best option** | **Option A — Adaptive EOU on existing energy VAD** |
| **Why** | Matches current `voice_assist.c` capture; no AFE/server redesign; solves pause cutoffs |
| **Avoid for now** | VADNet swap, persistent profiles, Whisper-grammar endpointer |
| **One-line goal** | Keep detecting speech the same way; only adapt *when* silence means “I’m done.” |

When ready to implement, change `VAD_TRAILING_SILENCE_MS` / silence stop logic in `voice_assist.c` first; leave wake and WebSocket alone.

**Status (2026-08-13):** Option A is implemented in `main/voice_assist.c`. NS/AEC deferred — see [PORT_3MIC_FEAT.md](PORT_3MIC_FEAT.md).

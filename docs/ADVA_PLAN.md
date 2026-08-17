# ADVA Plan — Adaptive End-of-Utterance (Option A)

Detailed implementation plan for **session-adaptive trailing silence** on NiNO’s existing energy VAD.

Parent decision doc: [ADVA.md](ADVA.md)

---

## 1. Goal

Stop cutting off users who pause mid-sentence, without making the robot feel slow for short commands.

| Today | After |
|-------|--------|
| Fixed **450 ms** trailing silence ends capture | Silence must last **adaptive_timeout_ms** |
| Same timeout for fast talkers and pause-y speakers | Timeout rises only after observed **intra-utterance** pauses |
| Energy VAD decides speech vs silence | Unchanged — EOU only decides *how long* silence means “done” |

**One-line goal:** keep detecting speech the same way; only adapt when silence means “I’m finished.”

---

## 2. Scope

### In scope (v1)

- Firmware only: `main/voice_assist.c` (`nino_voice_capture_vad_wav`)
- Replace fixed `VAD_TRAILING_SILENCE_MS` / `VAD_SILENCE_STOP_FRAMES` with adaptive timeout
- Session-scoped stats (one capture = one wake / one medical-ack listen)
- Hard min/max clamps
- Logging for tuning

### Out of scope (v1)

| Item | Why deferred |
|------|----------------|
| ESP-SR VADNet / P4 hardware VAD | Wake AFE is wake-only; capture is separate energy path |
| Server / WebSocket / STT changes | WAV-in contract stays identical |
| Persistent NVS / per-face pause profile | Multi-user home; identity not always correct |
| Streaming / partial-transcript EOU | Needs protocol + STT redesign |
| New FreeRTOS task or module | Keep logic inside existing capture loop |
| Changing energy thresholds / peak-ratio | Those decide “frame is silence”; leave alone unless logs show misclassification |

### Call sites that automatically get the behavior

| Caller | Function | Notes |
|--------|----------|--------|
| After “Hi ESP” | `nino_voice_assist_run_query_only()` → `run_ws_and_queue(10 s)` | Main conversational path |
| Medical ack | `nino_voice_assist_prompt_medical_ack()` → `run_ws_and_queue(8 s)` | Short yes/no — clamps keep it snappy if no mid-pauses |
| Prompt-ack re-listen | Same `nino_voice_capture_vad_wav` | Same EOU logic |

No API change in `voice_assist.h` for v1.

---

## 3. Current behavior (baseline)

File: `main/voice_assist.c`

| Constant | Value | Role |
|----------|-------|------|
| `VAD_FRAME_MS` | 20 | Frame size |
| `VAD_TRAILING_SILENCE_MS` | **450** | Fixed end-of-speech silence |
| `VAD_SILENCE_STOP_FRAMES` | 450/20 = **22** | End when `silence_streak` reaches this |
| `VAD_MIN_SPEECH_MS` | 300 | Don’t end before this much speech |
| `VAD_LISTEN_TIMEOUT_MS` | 6000 | No speech after arm → timeout |
| `VAD_PRE_ROLL_MS` | 200 | PCM before speech start |
| `VOICE_QUERY_VAD_MAX_SEC` | 10 | Hard max query length |
| `MED_ACK_VAD_MAX_SEC` | 8 | Hard max ack listen |

**Recording end condition today:**

```c
if (silence_streak >= VAD_SILENCE_STOP_FRAMES &&
    (pcm_samples / VAD_FRAME_SAMPLES) >= VAD_MIN_SPEECH_FRAMES) {
  /* end */
}
```

**Speech vs silence frame** (unchanged by this plan):

- Start: energy ≥ `vad_start_threshold(noise_floor)` for 3 consecutive frames
- While recording: “still speaking” if energy ≥ `vad_silence_end_threshold(peak, noise)` (peak × 30% vs noise-based floor)
- Else: silence frame → `silence_streak++`

**Pipeline (unchanged shape):**

```text
USB 4-mic → energy VAD → [EOU timeout] → WAV → WebSocket → STT → LLM → TTS
```

---

## 4. Target design

### 4.1 Two layers

```text
┌─────────────────────────────────────────┐
│ Layer 1 — Energy VAD (existing)         │
│   Frame → speech or silence             │
└──────────────────┬──────────────────────┘
                   │
┌──────────────────▼──────────────────────┐
│ Layer 2 — Adaptive EOU (new)            │
│   How long must silence last to END?    │
│   adaptive_timeout_ms ∈ [MIN, MAX]      │
└─────────────────────────────────────────┘
```

### 4.2 Adaptive rule

```text
adaptive_timeout_ms =
  clamp(
    max(EOU_DEFAULT_MS, max_intra_pause_ms + EOU_MARGIN_MS),
    EOU_MIN_MS,
    EOU_MAX_MS
  )
```

| Symbol | Meaning |
|--------|---------|
| `max_intra_pause_ms` | Longest pause *inside* this utterance that was followed by more speech |
| `EOU_MARGIN_MS` | Extra headroom so the *final* silence can exceed typical mid-pauses |
| Clamp | Prevents runaway waiting |

**Critical:** only update from `speech → silence → speech` resumes.  
Never grow the timeout from the final silence that ends the utterance (that would ratchet forever).

### 4.3 Constants (v1 starting point)

| `#define` | Value | Rationale |
|-----------|-------|-----------|
| `EOU_DEFAULT_MS` | **750** | Replaces 450; slightly more forgiving baseline |
| `EOU_MIN_MS` | **500** | Floor for snappy “set alarm” / “yes” |
| `EOU_MAX_MS` | **1500** | Ceiling — bot never waits >1.5 s after true end |
| `EOU_MARGIN_MS` | **350** | Final silence should beat mid-pauses |
| Keep | `VAD_MIN_SPEECH_MS` 300 | Unchanged |
| Keep | max 10 s / 8 s | Unchanged |

Optional later: bump `EOU_MAX_MS` to 1800 if logs show frequent 1.4–1.5 s mid-pauses still cutting off.

Deprecate / remove use of fixed `VAD_TRAILING_SILENCE_MS` as the stop condition (can keep as alias of `EOU_DEFAULT_MS` for a short transition, or delete).

### 4.4 Worked examples

**Fast speaker — short command**

```text
"What time is it?"
  speech … ~200 ms gaps … speech … 800 ms silence
  max_intra_pause ≈ 200
  adaptive = max(750, 200+350) = 750
  → END at 750 ms silence  ✓ snappy enough
```

**Pause-y speaker — mid gaps**

```text
"Can you tell me… [1000 ms] …the weather… [1200 ms] …tomorrow?"
  after first resume:  adaptive = max(750, 1000+350) = 1350 → clamp 1350
  after second resume: adaptive = max(750, 1200+350) = 1550 → clamp 1500
  final silence 1600 ms
  → END at 1500 ms  ✓ full sentence captured
```

**Without adaptation (today)**

```text
same utterance, 450 ms stop
  → cuts after first pause → STT gets "Can you tell me"  ✗
```

**Clamp safety**

```text
User somehow pauses 2.0 s mid-sentence then continues:
  max_intra = 2000 → 2000+350 → clamp → 1500
  If next gap is 1600 ms of true end → END
  If next gap is another 1400 ms mid-pause then speech → may still cut
  → accept for v1; raise MAX only with evidence
```

---

## 5. State machine

### 5.1 States

| State | Meaning |
|-------|---------|
| `LISTEN` | Armed, not recording yet (pre-roll, wait for speech start) |
| `SPEECH` | Recording; last frame(s) classified as speech |
| `PAUSE` | Recording; consecutive silence frames; waiting to END or resume |
| `END` | Finalize WAV and return |

`LISTEN` / speech-start logic stays as today. EOU only affects `SPEECH` ↔ `PAUSE` → `END`.

### 5.2 Diagram

```text
                    arm mic
                       │
                       ▼
                  ┌─────────┐
                  │ LISTEN  │◄──── silence / low energy
                  └────┬────┘
                       │ ≥3 speech frames
                       ▼
                  ┌─────────┐
           ┌─────►│ SPEECH  │◄────────────┐
           │      └────┬────┘             │
           │           │ silence frame    │ speech frame
           │           ▼                  │
           │      ┌─────────┐             │
           │      │  PAUSE  │─────────────┘
           │      └────┬────┘
           │           │
           │           │ silence_ms ≥ adaptive_timeout
           │           │ AND min speech met
           │           ▼
           │      ┌─────────┐
           │      │   END   │
           │      └─────────┘
           │
           └── (on PAUSE→SPEECH: record pause_ms, raise adaptive)
```

### 5.3 Transitions (recording phase only)

| From | Event | Action | To |
|------|-------|--------|-----|
| SPEECH | speech frame | `silence_streak = 0` | SPEECH |
| SPEECH | silence frame | `silence_streak = 1`; enter pause tracking | PAUSE |
| PAUSE | silence frame | `silence_streak++` | PAUSE |
| PAUSE | speech frame | `pause_ms = silence_streak * 20`; update `max_intra_pause_ms`; recompute `adaptive_timeout_ms`; `silence_streak = 0` | SPEECH |
| PAUSE | `silence_streak * 20 >= adaptive` AND min speech | log EOU end | END |
| any | PCM hits max_seconds | log max duration | END |

---

## 6. Data / variables (per capture)

Add locals (or fields on `vad_cap_t`) inside `nino_voice_capture_vad_wav()` — **reset every call**:

| Variable | Type | Init | Role |
|----------|------|------|------|
| `adaptive_timeout_ms` | `uint32_t` | `EOU_DEFAULT_MS` | Current end silence requirement |
| `max_intra_pause_ms` | `uint32_t` | `0` | Longest completed mid-utterance pause |
| `intra_pause_count` | `uint32_t` | `0` | How many speech→silence→speech events |
| `silence_streak` | `uint32_t` | `0` | Existing — consecutive silence frames |
| `recording` | `bool` | `false` | Existing |

Optional helper:

```c
static uint32_t eou_clamp_ms(uint32_t ms) {
  if (ms < EOU_MIN_MS) return EOU_MIN_MS;
  if (ms > EOU_MAX_MS) return EOU_MAX_MS;
  return ms;
}

static uint32_t eou_recompute(uint32_t max_intra_pause_ms) {
  uint32_t t = EOU_DEFAULT_MS;
  if (max_intra_pause_ms > 0) {
    uint32_t candidate = max_intra_pause_ms + EOU_MARGIN_MS;
    if (candidate > t) t = candidate;
  }
  return eou_clamp_ms(t);
}
```

On pause → speech resume:

```c
uint32_t pause_ms = silence_streak * VAD_FRAME_MS;
if (pause_ms > max_intra_pause_ms) {
  max_intra_pause_ms = pause_ms;
}
intra_pause_count++;
adaptive_timeout_ms = eou_recompute(max_intra_pause_ms);
ESP_LOGI(TAG, "EOU resume pause=%" PRIu32 "ms intra_max=%" PRIu32
              " adaptive=%" PRIu32 "ms count=%" PRIu32,
         pause_ms, max_intra_pause_ms, adaptive_timeout_ms, intra_pause_count);
```

End condition (replace fixed frame count):

```c
const uint32_t silence_ms = silence_streak * VAD_FRAME_MS;
if (silence_ms >= adaptive_timeout_ms &&
    (pcm_samples / VAD_FRAME_SAMPLES) >= VAD_MIN_SPEECH_FRAMES) {
  ESP_LOGI(TAG, "EOU end silence=%" PRIu32 "ms adaptive=%" PRIu32
                " intra_max=%" PRIu32 " peak=%" PRIu32,
           silence_ms, adaptive_timeout_ms, max_intra_pause_ms, cap.peak_energy);
  break;
}
```

---

## 7. Exact code change plan

### Step 1 — Constants

In `voice_assist.c` near existing VAD `#define`s:

1. Add `EOU_DEFAULT_MS`, `EOU_MIN_MS`, `EOU_MAX_MS`, `EOU_MARGIN_MS`.
2. Stop using `VAD_SILENCE_STOP_FRAMES` for the end check (delete or leave unused).
3. Either remove `VAD_TRAILING_SILENCE_MS` or set it equal to `EOU_DEFAULT_MS` and comment “legacy alias.”

### Step 2 — Helpers

Add `eou_clamp_ms` + `eou_recompute` as `static` functions in the same file (no new `.c`).

### Step 3 — Capture loop

In `nino_voice_capture_vad_wav()` recording branch:

1. Init `adaptive_timeout_ms = EOU_DEFAULT_MS`, `max_intra_pause_ms = 0`, `intra_pause_count = 0`.
2. Detect transition into pause (was speaking, now silence) — already implied by `silence_streak` going from 0 → 1.
3. On transition out of pause (silence → speech while `silence_streak > 0`): update stats + recompute.
4. Replace `silence_streak >= VAD_SILENCE_STOP_FRAMES` with `silence_ms >= adaptive_timeout_ms`.
5. Extend periodic trailing log to print `adaptive=` and `intra_max=`.

### Step 4 — Docs / comments

- Update comment on `nino_voice_capture_vad_wav` in `voice_assist.h` to mention adaptive trailing silence.
- Touch [USB-4MIC-INTEGRATION.md](USB-4MIC-INTEGRATION.md) “450 ms trailing silence” lines when implementing (point to ADVA).
- Keep [ADVA.md](ADVA.md) as the decision summary; this file as the build plan.

### Step 5 — What we will not touch

- `voice_wake.cpp` / AFE / `vad_init`
- `usb_mic.c` / mutex / `mic_capture_hold`
- `voice_ws_client.c` / server STT
- Energy threshold helpers (`vad_start_threshold`, `vad_silence_end_threshold`, peak ratio)

---

## 8. Pseudocode (full recording loop sketch)

```text
adaptive_timeout_ms = EOU_DEFAULT_MS
max_intra_pause_ms  = 0
silence_streak      = 0
recording           = false

while true:
  frame = mic_read(20ms)
  energy = mean_abs(frame)

  if not recording:
    # existing LISTEN + preroll + speech start — unchanged
    continue

  append_frame(frame)

  still_speaking = (energy >= silence_end_threshold(...))

  if still_speaking:
    if silence_streak > 0:
      # resume after pause
      pause_ms = silence_streak * 20
      max_intra_pause_ms = max(max_intra_pause_ms, pause_ms)
      adaptive_timeout_ms = eou_recompute(max_intra_pause_ms)
      log resume
    silence_streak = 0
  else:
    silence_streak++

  silence_ms = silence_streak * 20
  if silence_ms >= adaptive_timeout_ms and speech_long_enough:
    log EOU end
    break

  if hit_max_duration:
    break

finalize_wav()
```

---

## 9. Edge cases

| Case | Expected behavior |
|------|-------------------|
| Short “yes” / “no” (medical ack) | No intra-pause → timeout stays ~750 ms → still responsive |
| Single long pause then finish | After resume, timeout raised; final silence must exceed new timeout |
| Many small pauses (< default) | `max_intra + margin` may stay below default → timeout stays DEFAULT |
| Ambient noise flickers “speech” | Existing energy/peak logic; EOU does not fix false speech — tune energy separately if needed |
| Never resumes (true end) | First long silence ends at DEFAULT (or raised if prior resumes existed) |
| Hits 10 s max | Still hard-stop; EOU does not extend max duration |
| No speech in 6 s | Existing listen timeout unchanged |
| Very long mid-pause (> MAX) | Clamp; may still cut if mid-pause ≥ MAX — document; raise MAX in v1.1 if common |
| Two users back-to-back | Each capture resets stats — no cross-talk of profiles |

---

## 10. Logging & metrics

### Required logs

| Event | Example |
|-------|---------|
| Speech start | existing `VAD speech start` |
| Pause resume | `EOU resume pause=1000ms intra_max=1000 adaptive=1350ms count=1` |
| EOU end | `EOU end silence=1500ms adaptive=1500 intra_max=1200 peak=...` |
| Periodic trailing | include `adaptive=` and `silence=` (already has silence) |

### Tuning from serial

After a few days, collect:

1. Distribution of `intra_max` on successful long utterances  
2. How often `adaptive` hits `EOU_MAX_MS`  
3. Complaints of early cut vs “bot waits too long”  
4. Medical-ack false delays (if any)

Adjust `DEFAULT` / `MIN` / `MAX` / `MARGIN` only — no architecture change.

---

## 11. Test plan

### Bench (serial + headphones / known phrases)

| # | Test | Pass criteria |
|---|------|----------------|
| T1 | Short: “What time is it?” | Ends ~0.5–0.9 s after last word; STT correct |
| T2 | Pause-y: “Can you… [~1 s] …tell me the weather tomorrow?” | Full phrase in one WAV; log shows adaptive raised |
| T3 | Double pause: two mid-gaps ~1.0–1.2 s | Full capture; adaptive ≤ 1500 |
| T4 | Medical-style “yes” | Ends quickly; no stuck listen |
| T5 | No speech after wake | 6 s listen timeout still works |
| T6 | Talk until ~10 s | Max duration still stops |
| T7 | Whisper/quiet speech | Energy VAD still starts; EOU doesn’t regress |
| T8 | Far-field USB 4-mic (2–3 m) | Same as T1–T2 at distance |

### Product feel

| Feel | If wrong | Knob |
|------|----------|------|
| Cuts mid-sentence often | Intra pauses ≥ end timeout | Raise `EOU_MAX` or `EOU_MARGIN` |
| Feels sluggish after short commands | Default too high | Lower `EOU_DEFAULT` toward 600–700 |
| Never ends for pause-y user | MAX too high or mid-pause ≈ MAX | Cap is working; user must leave longer final gap, or later Phase 2/3 |

---

## 12. Risks & mitigations

| Risk | Mitigation |
|------|------------|
| Timeout creeps upward within one utterance | Only update on resume; clamp MAX |
| False “speech” after true end delays stop | Unchanged energy end_th; don’t loosen energy for EOU |
| Medical ack feels slower | No mid-pauses → stays near DEFAULT; MIN=500 |
| Docs still say “450 ms” | Update USB-4MIC + FIRMWARE_ARCHITECTURE when shipping |
| Confusing “adaptive VAD” naming | Call it **EOU** in logs and docs |

---

## 13. Implementation checklist

- [x] Add EOU `#define`s in `voice_assist.c`
- [x] Add `eou_clamp_ms` / `eou_recompute`
- [x] Wire adaptive end condition in capture loop
- [x] Wire pause-resume update path
- [x] Add / update ESP_LOGI lines
- [x] Update `voice_assist.h` comment
- [x] Update USB-4MIC doc trailing-silence references
- [ ] Flash + run T1–T8
- [ ] Tune constants from serial if needed (v1.1)

**Status:** v1 code landed in `main/voice_assist.c` (2026-08-13). NS/AEC deferred — see [PORT_3MIC_FEAT.md](PORT_3MIC_FEAT.md).

**Estimated effort:** ~1 focused firmware change + half day of listen testing.  
**Server PR:** none.  
**New files:** none required (docs only: this plan + ADVA).

---

## 14. Phased rollout

| Phase | Deliverable | Exit criteria |
|-------|-------------|----------------|
| **v1** | Option A in `voice_assist.c` | **Code done** — flash and run T1–T8 |
| **v1.1** | Constant tuning only | Logs show few MAX hits; few “too slow” complaints |
| **v2** | Optional per-face / NVS profile | Only if v1 still fails identifiable pause-y users |
| **v3** | Streaming STT incomplete-sentence EOU | Only if product needs near-human turn-taking |

---

## 15. Success criteria

1. Pause-y multi-clause questions arrive as **one** WAV / one STT string.  
2. Short commands still feel responsive (end silence typically ≤ ~800 ms when no mid-pauses).  
3. No change to wake word, WebSocket, or server pipeline.  
4. No unbounded wait (`EOU_MAX_MS` hard ceiling).  
5. Medical / prompt-ack re-listen still works without extra beeps or mic lock bugs.

---

## 16. Reference map

| Doc / file | Role |
|------------|------|
| [ADVA.md](ADVA.md) | Decision: Option A vs B/C/D |
| **This file** | Detailed build plan for Option A |
| [USB-4MIC-INTEGRATION.md](USB-4MIC-INTEGRATION.md) | Current mic + VAD flow (update silence ms when shipping) |
| `main/voice_assist.c` | Implementation target |
| `main/voice_wake.cpp` | Do not change for v1 |
| `main/voice_ws_client.c` | Unchanged WAV contract |

---

## 17. Summary

Implement **Adaptive EOU** as a small state extension inside the existing energy VAD:

1. Default trailing silence **750 ms** (not 450).  
2. When the user pauses and continues, raise timeout to `intra_pause + 350`, clamped **500–1500 ms**.  
3. Ship, log, tune — nothing else.

That is the simplest plan that matches NiNO’s architecture without complicating wake, AFE, or the PC stack.

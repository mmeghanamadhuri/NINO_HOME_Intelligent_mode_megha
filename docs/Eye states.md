# NINO Eye States

This document describes the **9 eye states** driven by the NINO eye animation engine
(`nino_eye.c` / `nino_eye.h`) and rendered on the dual Waveshare 1.27" SSD1351 OLEDs
(128 × 96, mirrored across both eyes).

## All States

1. idle
2. happy
3. tired
4. thinking
5. curious
6. sad
7. surprised
8. listening
9. recalling

## Emotion States

Excluding the functional states (idle, thinking, listening), there are emotion states:

1. happy
2. tired
3. curiousśś
4. sad
5. surprisedśś
6. recalling

## Overview

- Background is **white**; the eye/symbol is drawn in the per-state colour (black for every
  state except *happy*, which is a red heart).
- A global downward shift (`NINO_VOFFSET = 8`) and a clip window keep all drawing inside the
  central oval region so only the eye changes — the surrounding background never flashes.
- State changes are **instant and non-blocking**: the running animation switches to the new one
  on its next frame.

### How to trigger a state

| Method | Example |
|--------|---------|
| Code-driven trigger | `nino_eye_happy();`, `nino_eye_listening();`, … |
| Generic setter | `nino_eye_set_state(NINO_EYE_HAPPY);` |
| Serial monitor (when started via `nino_eye_start()`) | type the index `0`–`8` or the name (`idle`, `happy`, …) |

## State Reference

| # | Enum | Name | Render mode | Eye size (rx × ry) | Colour | Behaviour summary |
|---|------|------|-------------|--------------------|--------|-------------------|
| 0 | `NINO_EYE_IDLE` | idle | Blink | 24 × 30 | Black | Neutral eye, slow ~5 s blink |
| 1 | `NINO_EYE_HAPPY` | happy | Heart | scale 20 | Red `(255,40,70)` | Static red heart symbol, no blink |
| 2 | `NINO_EYE_TIRED` | tired | Lidded blink | 24 × 30 | Black | Heavy lowered lid (bottom sliver), slow blink |
| 3 | `NINO_EYE_THINKING` | thinking | Static / rolling | 24 × 30 | Black | Solid eye slowly rolls around the top, no blink |
| 4 | `NINO_EYE_CURIOUS_QUIZ` | curious | Blink + move | 28 × 33 | Black | Wide eye tilts up-to-a-side, blinks across to the other side |
| 5 | `NINO_EYE_SAD` | sad | Lidded blink | 24 × 30 | Black | Heavy upper lid covers top ~40 %, slow ~6 s blink |
| 6 | `NINO_EYE_SURPRISED` | surprised | Static / snap | 27 × 36 | Black | Widest/tallest eye, fast snap-open on entry then holds |
| 7 | `NINO_EYE_LISTENING` | listening | Blink | 30 × 36 | Black | Wide centered eye, blinks in place (~3 s cycle) |
| 8 | `NINO_EYE_RECALLING` | recalling | Blink + move | 24 × 28 | Black | Soft eye drifts upward through memory-gaze points, slow blink between |

---

## Detailed Descriptions

### 0 — Idle (`NINO_EYE_IDLE`)
Neutral, half-open eye with a normal pupil and a slow blink (~4–7 s, tuned to ~5 s). This is the
default state the engine starts in.

- Size: `rx 24`, `ry 30`
- Hold: 10000 ms · Closed hold: 240 ms · Blink step/ms: 3 / 45
- Trigger: `nino_eye_idle()` · token `0` / `idle`

### 1 — Happy (`NINO_EYE_HAPPY`)
A single static **red heart** symbol (no eyelid, pupil or blink). The only coloured state.

- Heart scale: 20 (min = max, so no pulse) · State hold: 900 ms
- Colour: `(255, 40, 70)`
- Trigger: `nino_eye_happy()` · token `1` / `happy`

### 2 — Tired (`NINO_EYE_TIRED`)
Low eye with a heavy lowered lid — only a bottom sliver is visible. Lids close from both edges
toward the window mid-row, hold, then reopen. Slow blink.

- Size: `rx 24`, `ry 30` · Lidded window: `top = EYE_CY + 4`, `bottom = EYE_CY + 30`
- Hold: 4500 ms · Closed hold: 300 ms · Blink step/ms: 2 / 45
- Trigger: `nino_eye_tired()` · token `2` / `tired`

### 3 — Thinking (`NINO_EYE_THINKING`)
A normal solid eye (like idle) that slowly rolls around the top — up-left → up → up-right → up …
to convey pondering. The whole eye moves; there is **no blink**.

- Size: `rx 24`, `ry 30` · Per-gaze dwell: ~2800 ms
- Gaze path (dx, dy): `{0,-10} {0,-22} {-14,-16} {0,-22} {14,-16}`
- Trigger: `nino_eye_thinking()` · token `3` / `thinking`

### 4 — Curious / Quiz (`NINO_EYE_CURIOUS_QUIZ`)
A wide, enlarged eye that tilts up-and-to-a-side and holds that inquisitive look, then blinks
across to the other side and holds again (head-tilt feel).

- Size: `rx 28`, `ry 33`
- Hold: 2500 ms · Closed hold: 120 ms · Blink step/ms: 4 / 20
- Look-points (dx, dy): up-left `(-16,-10)` → up-right `(16,-10)`
- Trigger: `nino_eye_curious()` · token `4` / `curious` (or `quiz`)

### 5 — Sad (`NINO_EYE_SAD`)
Heavy upper lid covering the top ~40 % of the eye, with a slow (~6 s) lidded blink.

- Size: `rx 24`, `ry 30` · Lidded window: `top = EYE_CY - 6`, `bottom = EYE_CY + 30`
- Hold: 6000 ms · Closed hold: 300 ms · Blink step/ms: 3 / 45
- Trigger: `nino_eye_sad()` · token `5` / `sad`

### 6 — Surprised (`NINO_EYE_SURPRISED`)
The widest/tallest eye. One fast **snap-open** on entry, then holds wide (no frantic blink).
The blink step/ms tune the entry snap only.

- Size: `rx 27`, `ry 36`
- Hold: 5000 ms · Snap step/ms: 8 / 12
- Trigger: `nino_eye_surprised()` · token `6` / `surprised`

### 7 — Listening (`NINO_EYE_LISTENING`)
The same wide, enlarged eye as curious, but **centered** — it blinks in place (no left/right
tilt). ~3 s blink cycle.

- Size: `rx 30`, `ry 36`
- Hold: 6000 ms · Closed hold: 120 ms · Blink step/ms: 4 / 20
- Trigger: `nino_eye_listening()` · token `7` / `listening`

### 8 — Recalling (`NINO_EYE_RECALLING`)
A softer normal eye drifting upward through memory-gaze points (centre → up-left → up → up-right
→ centre). Holds at each point, then a slow blink while shifting to the next — introspective,
not frantic (calmer than thinking's roll).

- Size: `rx 24`, `ry 28`
- Hold: 3600 ms · Closed hold: 280 ms · Blink step/ms: 3 / 45
- Gaze path (dx, dy): `{0,-4} {-12,-12} {0,-16} {12,-12} {0,-6}`
- Trigger: `nino_eye_recalling()` · token `8` / `recalling`

---

## Render Modes

The engine dispatches each state to one of these renderers:

- **Blink** (`NINO_RENDER_BLINK`) — draw once, then repeatedly blink (and optionally move) the eye.
- **Static** (`NINO_RENDER_STATIC`) — draw a steady image and hold (used by tired/sad lidded blink,
  thinking roll, and surprised snap, each with a custom routine).
- **Heart** (`NINO_RENDER_HEART`) — draw the red heart symbol (happy only).

## Integration

```c
ssd1351_init();      // bring up the displays (once)
nino_eye_begin();    // start the eye animator (once, non-blocking)
// ...
nino_eye_happy();    // trigger any emotion from anywhere
nino_eye_listening();
```

Use `nino_eye_start()` instead of `nino_eye_begin()` to also enable the serial-monitor command
listener for quick testing (type `0`–`8` or a state name).

> Source of truth: `main/nino_eye.c` (`s_profiles[]` table) and `main/nino_eye.h` (`nino_eye_state_t` enum).

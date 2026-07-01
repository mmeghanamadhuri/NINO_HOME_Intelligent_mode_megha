# Face Tracking Implementation (On-Board / Edge)

## Objective

Implement real-time, on-board face tracking on ESP32-P4 so the head pan servo follows a detected face:

- Face moves right -> servo pans right
- Face moves left -> servo pans left
- Runs directly on device (no cloud round-trip for tracking loop)
- Keeps existing camera streaming to server active

## Current System Baseline

- USB UVC camera already streams MJPEG via `/stream` and `/snapshot.jpg`.
- Dynamixel servo bus is already managed in firmware:
  - ID1 = tilt
  - ID2 = pan
- Existing voice/audio/head-motion subsystems can temporarily own servo motion.

## Implemented Architecture

The tracking system is added as a **second consumer** of the same camera frames:

1. Camera callback stores latest MJPEG frame in shared buffer.
2. HTTP streaming path keeps using this buffer (unchanged behavior).
3. A dedicated `face_track_task` is notified when a new frame arrives.
4. Task copies latest frame, runs face detection, computes center error.
5. Tracker maps error to pan servo command with smoothing, deadzone, and limits.

This keeps the UVC callback lightweight and avoids blocking stream delivery.

## Modules Added

### 1) `main/face_detect.hpp` + `main/face_detect.cpp`

Responsibilities:

- Initialize ESP-DL face detector (`HumanFaceDetect`)
- Decode MJPEG frame to RGB
- Run inference and return best face result:
  - `found`
  - `cx`, `cy`
  - `frame_w`, `frame_h`
  - confidence score

### 2) `main/face_tracker.h` + `main/face_tracker.c`

Responsibilities:

- Tracking state machine (`enabled`, `paused`, acquire/lost counters)
- Pan control law:
  - error = frame center - smoothed face center
  - deadzone to suppress jitter
  - proportional step with clamp
  - pan goal clamp to servo range
- Pause servo writes when:
  - audio motion is active
  - spin/track-hon action is active
  - servo bus is not ready

## Runtime Integration in `main/main.c`

Added:

- Face tracking task configuration constants:
  - `FACE_TRACK_TASK_STACK_SIZE`
  - `FACE_TRACK_INFERENCE_INTERVAL_MS`
  - `FACE_TRACK_REUSE_LAST_FACE_MS`
- `s_face_track_task_handle`
- New frame notification from `latest_frame_store()`
- `face_track_task()` worker:
  - throttled inference
  - best-effort latest-frame processing
  - short reuse of last valid face during frame hiccups
- `track` CLI command:
  - `track on`
  - `track off`
  - `track status`
- Startup sequence:
  - `nino_face_tracker_init()`
  - create `face_track_task`

## Build / Dependency Updates

### `main/CMakeLists.txt`

Added new sources:

- `face_detect.cpp`
- `face_tracker.c`

### `main/idf_component.yml`

Added dependencies:

- `espressif/esp-dl`
- `espressif/human_face_detect`

## Control Logic (Pan Tracking)

Current tracker is intentionally **pan-first** for stable behavior:

- Servo used: ID2 (pan)
- Center reference: `512`
- Deadzone: `12 px`
- Proportional gain: `0.10`
- Max step per update: `120`
- Smoothing factor: `0.55`
- Acquire hits before motion: `3`
- Lost-face decay: keep state for short window, then reset filter

This prevents oscillation and avoids noisy micro-movements.

## Coexistence Rules

Tracking is a soft owner of servos and yields to higher-priority motion:

- If `servo_motion` is active -> tracking pauses.
- If `spin_360` / `track_hon` is active -> tracking pauses.
- If bus/servos are not ready -> tracking pauses.

So face tracking does not fight other features.

## How to Use

From ESP console:

- `track on` -> enable pan face tracking
- `track status` -> inspect detector/servo pause state and face lock info
- `track off` -> stop tracking

## Verification Checklist

1. Build succeeds with new deps.
2. Camera stream remains stable on `/stream`.
3. Run `track on`.
4. Move face left/right in frame.
5. Confirm ID2 pan follows direction correctly.
6. Trigger audio motion or 360 spin and verify tracking pauses.

## Next Steps (Optional)

- Add tilt tracking (ID1) after pan tuning is stable.
- Add optional auto-enable at boot via config/NVS.
- Add HTTP status endpoint for tracker telemetry.
- Add center-hold/return-home policy when face is lost for long duration.

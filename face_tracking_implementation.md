# Face Tracking Implementation Plan For This Project

## Goal

Add local face tracking on the ESP32-P4 so the head follows a detected face using the existing UVC camera and Dynamixel pan/tilt servos, without breaking the current voice, touch, and playback loop.

This note uses [face_track.md](./face_track.md) only as a logic reference. The design below is adapted to the current project structure in `main/`.

## What We Already Have

### Camera path

- `main/main.c` already owns the USB camera through `uvc_host`.
- Frames are received in `frame_callback()` and copied into `s_latest_frame` by `latest_frame_store()`.
- The project already exposes:
  - `/snapshot.jpg`
  - `/stream`
- Important detail: the current code keeps only the latest MJPEG frame for HTTP use. There is no local vision-processing task yet.

### Servo path

- `main/servo_dxl.c` already manages U2D2 discovery, Dynamixel ping, position writes, and present-position reads.
- `main/servo_dxl.h` already exposes the functions we need:
  - `nino_servo_dxl_is_ready()`
  - `nino_servo_dxl_go_neutral()`
  - `nino_servo_dxl_set_pan_tilt()`
  - `nino_servo_dxl_get_present_position()`
- Servo mapping is already correct for this robot:
  - ID1 = tilt
  - ID2 = pan

### Motion path

- `main/servo_motion.c` currently generates animation poses for audio playback.
- That means face tracking cannot blindly send pan/tilt commands all the time, or it will fight with the existing motion task.

## Recommended Architecture

Yes, streaming and face tracking should run together.

The right model here is a **dual-consumer pipeline**:

1. One camera producer receives UVC MJPEG frames.
2. HTTP streaming keeps serving the latest frame to the browser/PC path.
3. A separate tracking worker also consumes frames for local face detection.
4. Both consumers run concurrently, but neither blocks the UVC callback.

Use the same 4-stage tracking loop as the reference:

1. Acquire a camera frame copy.
2. Run face detection.
3. Convert face center into pan/tilt error from frame center.
4. Send bounded servo updates through `nino_servo_dxl_set_pan_tilt()`.

For this project, the cleanest structure is:

- `face_detect.[ch/cpp]`
  - Load ESP-DL face model once.
  - Decode MJPEG to RGB.
  - Return best face center and frame size.

- `face_tracker.[ch]`
  - Hold tracker state.
  - Smooth face coordinates.
  - Apply deadzone, proportional gain, step clamp, and soft limits.
  - Own enable/disable state.

- `face_track_task.[ch]` or a small task section inside `main.c`
  - waits for new frames
  - copies or receives a tracking frame
  - runs detector
  - feeds the tracker

- `track` CLI command
  - `track on`
  - `track off`
  - `track status`

## Best Integration Point In This Repo

Do not run detection inside `frame_callback()`.

That callback is part of the UVC streaming path and should stay lightweight. Heavy JPEG decode and face detection there would increase frame drops and can disturb the camera stream.

Instead:

- Keep `frame_callback()` and `latest_frame_store()` as they are.
- Add a separate FreeRTOS task that:
  - receives notification that a newer frame exists
  - copies the MJPEG frame under mutex
  - releases the mutex quickly
  - runs decode + face detection outside the critical section
  - updates tracker state

This matches the current design much better than the reference app, because this repo already has a central latest-frame buffer.

## Proposed Data Flow

### Concurrent streaming + tracking layout

The architecture should look like this:

`USB camera -> frame_callback() -> latest_frame_store() -> { HTTP stream consumer, face tracking consumer }`

That means:

- camera capture is still single-source
- HTTP streaming continues exactly as now
- local tracking runs from the same stored frame stream
- tracking must drop old frames when behind instead of trying to process every frame

This is important: the tracking path should be **best effort**, not real-time guaranteed per frame. If it falls behind, it should always jump to the newest frame.

### 1. Latest frame copy helper

Add a helper in `main.c` or a new camera helper module:

- `bool latest_frame_copy(uint8_t **jpeg, size_t *len, uint16_t *w, uint16_t *h, uint32_t *seq);`

Behavior:

- lock `s_frame_mutex`
- if no frame is ready, return `false`
- allocate/copy the latest JPEG buffer
- unlock fast
- caller frees the copied buffer after detection

This avoids holding the frame mutex during model inference.

### 2. Frame notification for the tracking task

Add a lightweight notification mechanism so tracking runs only when a new frame arrives.

Two simple options:

- task notification to the face-tracking task
- queue with depth `1` holding only the newest frame sequence number

Recommended:

- keep `s_latest_frame`
- add a `uint32_t` sequence counter if not already enough
- after `latest_frame_store(frame)`, notify the tracking task

The tracking task then:

- wakes up on notification
- checks whether a newer frame sequence exists
- copies only the newest frame
- ignores older missed notifications

This gives streaming and tracking concurrency without backpressure on the camera callback.

### 3. Face detector module

Port the detector structure from `face_track.md`:

- use `espressif/esp-dl`
- use `espressif/human_face_detect`
- decode MJPEG with `dl::image::hw_decode_jpeg(...)` on P4
- select the best face by confidence

Return:

- `found`
- `cx`, `cy`
- `frame_w`, `frame_h`
- optional `score`

### 4. Tracking control module

The tracker should be stateful and independent from streaming.

It should own:

- enabled/disabled state
- smoothed face center
- current pan goal
- current tilt goal
- acquire-hit counter
- lost-face counter

Streaming should not know anything about servo logic.

## Tracking Logic To Reuse

The reference logic is a good fit here:

- error from frame center
- proportional response
- deadzone near center
- max step per update
- EMA smoothing on face center
- small acquire delay before moving
- lost-face timeout

Suggested formulas:

- `err_x = (frame_w / 2) - face_cx`
- `err_y = (frame_h / 2) - face_cy`
- `delta_pan  = clamp(err_x * kp_pan,  -max_step, max_step)`
- `delta_tilt = clamp(err_y * kp_tilt, -max_step, max_step)`

Then:

- `pan_goal  = pan_goal  + pan_sign  * delta_pan`
- `tilt_goal = tilt_goal + tilt_sign * delta_tilt`

Finally clamp goals to servo-safe limits.

## Servo Ownership Rules

This is the main repo-specific concern.

Today there are already multiple possible servo writers:

- `servo_motion.c` during playback
- `nino_servo_dxl_spin_360()` for the special spin action
- future face tracking loop

So we should define ownership clearly.

### Recommended rule

Face tracking should command the servos only when all of these are true:

- tracking is enabled
- `nino_servo_dxl_is_ready() == true`
- `nino_servo_dxl_spin_is_active() == false`
- `nino_servo_motion_is_active() == false`

If audio motion starts, tracking should temporarily pause and stop sending updates.

This prevents "servo fighting" between tracking and playback gestures.

## File Changes Recommended

### `main/idf_component.yml`

Add dependencies:

- `espressif/esp-dl`
- `espressif/human_face_detect`

This is already shown in the reference and will likely be required here too.

### `main/CMakeLists.txt`

Add new source files, for example:

- `face_detect.cpp`
- `face_tracker.c`
- `track_console.c`

If C++ is used for the detector wrapper, keep the tracker itself in C if you want minimal impact on the rest of the project.

### `main/main.c`

Add:

- latest-frame copy helper
- tracking-task notification hook after storing a frame
- face tracking task startup
- tracker init
- CLI command registration

Do not replace the current HTTP camera path. Face tracking should consume the same camera frames already being captured.

### `main/servo_motion.c`

No large rewrite is needed, but the tracker will need to check `nino_servo_motion_is_active()` before sending servo goals.

## Proper Integration Flow

This is the practical answer to:

`How do we integrate face tracking into the current architecture without disturbing the present framework?`

The safest approach is to integrate it in layers, keeping the current system working at every step.

### Integration principle

We should **attach** face tracking to the existing camera and servo framework, not replace any part of it.

That means:

- do not change the current UVC ownership model
- do not break HTTP streaming
- do not break voice flow
- do not break touch-triggered audio
- do not let tracking take permanent ownership of the servos

Face tracking should behave like an additional feature module that listens to the current frame pipeline and only commands the head when allowed.

### Phase 0: Preserve the current framework

Before adding any tracking logic, keep these parts untouched:

- `frame_callback()` remains lightweight
- `latest_frame_store()` remains the central frame-storage point
- HTTP `/snapshot.jpg` and `/stream` stay unchanged
- `nino_servo_dxl_start()` and current servo bring-up stay unchanged
- `servo_motion.c` stays the current owner during playback motions

This is important because these are already proven working paths.

### Phase 1: Add tracking hooks, not tracking behavior

First add the architecture hooks only.

What to add:

- a latest-frame copy helper
- a tracking task handle
- a frame notification path from `latest_frame_store()` or just after it
- a tracking enable flag

What not to add yet:

- no face detection
- no servo movement
- no tuning logic

Goal of this phase:

- confirm that the tracking task can wake up on new frames
- confirm that streaming still works exactly the same
- confirm that no added mutex/queue logic causes frame instability

### Phase 2: Add detector in observation-only mode

Next integrate face detection, but keep it read-only.

Behavior in this phase:

- tracking task copies newest JPEG frame
- detector runs on that frame
- logs face found / not found and center coordinates
- no servo commands yet

Goal of this phase:

- prove that local face detection can run while streaming continues
- measure whether FPS or responsiveness drops too much
- verify MJPEG decode + model inference are stable on the board

This phase is very valuable because it isolates vision load from motion behavior.

### Phase 3: Add tracker state, still without moving hardware

After detection works, add tracker math:

- smoothing
- deadzone
- proportional gain
- max step clamp
- acquire/lost counters

But still do not send servo writes. Just log:

- raw face center
- smoothed center
- computed pan delta
- computed tilt delta
- intended pan/tilt goal

Goal of this phase:

- tune logic safely before hardware starts moving
- catch sign mistakes early
- confirm the tracker math is stable with real detections

### Phase 4: Enable pan-only movement

Once the math is good, allow the tracker to control pan only.

Rules:

- only when `track on`
- only when servos are ready
- only when no 360 spin is active
- only when `servo_motion` is not active

Goal of this phase:

- verify real face-follow behavior with minimal instability
- reduce camera shake while tuning
- confirm tracking can coexist with the rest of the system

This is the first real integration milestone.

### Phase 5: Add tilt carefully

After pan is stable, enable tilt using the same control rules.

Start with:

- lower tilt gain than pan
- tighter tilt limits
- conservative deadzone

Goal of this phase:

- complete full face tracking
- avoid aggressive vertical oscillation

### Phase 6: Add coexistence rules with the present framework

This is where we make the feature production-safe.

Tracking must cooperate with:

- audio playback motion
- touch-triggered voice clips
- 360 spin action
- voice-driven activities that may also want neutral pose

Recommended behavior:

- if `servo_motion` starts, tracking pauses
- if 360 spin starts, tracking pauses
- when external motion ends, tracking resumes automatically if still enabled
- after long face loss, tracker clears its smoothing state

This keeps the existing framework as the primary behavior system and makes tracking a well-behaved participant inside it.

### Phase 7: Optional CLI and diagnostics polish

After stable motion, add user controls:

- `track on`
- `track off`
- `track status`

Optional status output:

- tracker enabled/disabled
- last frame sequence processed
- face found / not found
- pan goal
- tilt goal
- paused because of motion ownership

This makes testing much easier without changing the rest of the application flow.

## Non-Disturbing Integration Rules

To keep the present framework stable, follow these rules strictly.

### Rule 1: Keep camera callback lightweight

Do not place JPEG decode, face inference, or servo logic in `frame_callback()`.

That callback should remain capture-only.

### Rule 2: Use shared frame storage

Do not create a second camera path for tracking.

Tracking should consume the same latest-frame data already used by HTTP streaming.

### Rule 3: Process newest frame only

Do not try to process every frame in sequence.

If tracking lags, drop old frames and process only the newest one. This prevents backpressure on the current streaming framework.

### Rule 4: Tracking is a soft owner of the servos

Tracking may control the servos only when no higher-priority motion is active.

In this project, higher-priority motion includes:

- playback gestures
- explicit spin action
- any future forced pose action

### Rule 5: Add feature flags first

Keep tracking disabled by default until each phase is verified.

That way the current product flow remains unchanged unless tracking is explicitly enabled.

### Rule 6: Validate in observation mode before motion mode

Always verify:

- frame copying
- face detection
- tracker math

before allowing real servo writes.

That prevents debugging three problems at once.

## Recommended Step-By-Step Build Order

Here is the clean implementation order for this repo:

1. Add frame-copy helper and tracking task notification.
2. Start a no-op tracking task and verify stream is unaffected.
3. Add face detection and log-only mode.
4. Add tracker math and log intended servo outputs.
5. Add `track on/off/status`.
6. Enable pan-only servo control.
7. Add pause/resume behavior around `servo_motion` and `spin_360`.
8. Enable tilt with conservative tuning.
9. Tune gains and limits on real hardware.

If we follow this order, every stage can be tested without destabilizing the current application.

## Definition Of Success

We can say the integration is correct when all of these are true:

- browser/PC streaming still works as before
- voice loop still works as before
- touch warning still works as before
- audio playback head motion still works as before
- face tracking can be enabled separately
- tracking pauses cleanly when another motion owns the servos
- no major frame starvation appears after detector integration

If any one of those breaks, the integration is too invasive and needs to be pulled back one phase.

## Suggested Runtime Behavior

### On boot

- camera starts as it does today
- U2D2/servos start as they do today
- face tracker task starts and waits for frame notifications
- HTTP streaming remains available as normal

### When `track on`

- tracker resets smoothing state
- optional: send head to neutral once
- tracker begins consuming the newest available frames and commanding pan/tilt
- HTTP stream continues in parallel

### When `track off`

- tracker stops commanding servos
- optional: return to neutral
- HTTP stream is unaffected

### When no face is found

- keep last pose briefly
- after N missed frames, clear smoothing state
- optionally return to neutral after a longer timeout

## Suggested Initial Tuning Values

Start conservative:

- `TRACK_DEADZONE_PX = 12`
- `TRACK_KP_PAN = 0.08f to 0.12f`
- `TRACK_KP_TILT = 0.08f to 0.12f`
- `TRACK_MAX_STEP = 40 to 80`
- `TRACK_FACE_SMOOTHING = 0.45f to 0.65f`
- `TRACK_FACE_ACQUIRE_HITS = 2 or 3`
- `TRACK_FACE_LOST_FRAMES = 10 to 20`

Compared with the reference, I would begin with smaller `TRACK_MAX_STEP` here because this robot is already doing other head motions and camera shake will affect detection quality.

## One Important Design Choice

There are two possible ways to add face tracking:

### Option A: On-device tracking on the ESP32-P4

Pros:

- self-contained
- no PC round-trip
- works even if the external face server is down

Cons:

- more RAM and CPU load on the ESP
- must tune carefully to avoid disturbing UVC streaming

### Option B: PC sends face center back to the board

Pros:

- lighter on the ESP
- can reuse your existing PC face-recognition pipeline

Cons:

- depends on network latency
- tracking breaks if PC app or socket path is unavailable
- adds protocol work between PC and board

For this repo, I recommend **Option A first**, because your reference is already based on local detection and the board already owns the camera frames directly.

## Preferred Concurrent Architecture

If your requirement is specifically:

- stream live camera
- track face at the same time
- avoid disturbing the existing camera pipeline

then this is the architecture I recommend:

1. `frame_callback()` only pushes frames into the existing storage path.
2. `latest_frame_store()` updates one shared latest-frame buffer and increments sequence.
3. HTTP endpoints read from that shared latest-frame buffer.
4. Face-tracking task is notified that a new frame arrived.
5. Face-tracking task copies only the newest JPEG frame and processes it asynchronously.
6. Tracker sends servo commands only when servo ownership is free.

This gives you:

- one camera stream
- one shared frame source
- concurrent streaming and tracking
- minimal change to the existing UVC callback
- no extra USB camera session

## What We Should Not Do

Avoid these patterns:

- opening a second independent camera pipeline for tracking
- doing face detection inside `frame_callback()`
- trying to process every frame in order
- letting tracking block HTTP or vice versa

Those approaches will make the system more fragile.

## Implementation Sequence

1. Add detector dependencies and source files.
2. Add latest-frame copy helper plus tracking-task notification.
3. Implement `face_detect` wrapper and verify one-frame detection only.
4. Start a face-tracking worker that processes newest-frame-only.
5. Implement `face_tracker` state machine without tilt at first.
6. Add `track on/off/status` CLI.
7. Enable pan-only tracking first.
8. After pan is stable, add tilt.
9. Add pause rules while `servo_motion` or `spin_360` is active.
10. Tune gains and limits on real hardware.

## Recommended Phase 1 Scope

For the first implementation, keep it intentionally narrow:

- local face detection
- pan-only tracking first
- tracker disabled by default
- CLI-controlled enable/disable
- concurrent HTTP stream kept unchanged
- no eye-display changes yet
- no face box overlay logic

This will give you a stable foundation before adding tilt and behavior polish.

## Risks To Watch

- JPEG decode + face detect may reduce camera throughput if the task runs every frame.
- If tracking updates too quickly, the head motion can shake the camera and make detection worse.
- Servo commands from playback gestures and tracking can conflict unless ownership is enforced.
- Returning to neutral too aggressively on a missed frame can create visible twitching.

## Final Recommendation

Yes, face tracking fits this project well, and it should run concurrently with streaming.

The clean path is to keep the current camera streaming pipeline untouched, use it as the shared frame producer, add a separate face-detection task that consumes copies of the latest MJPEG frame, and place a tracker layer between detection results and `nino_servo_dxl_set_pan_tilt()`.

If we implement it in phases, the safest first milestone is:

- pan-only
- CLI enabled
- paused during audio servo motion
- on-device face detection using the ESP-DL flow from the reference
- HTTP streaming still running in parallel

After that is stable on hardware, we can add tilt and refine the behavior.

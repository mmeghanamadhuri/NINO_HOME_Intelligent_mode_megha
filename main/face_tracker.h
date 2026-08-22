#pragma once

#include <stdbool.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef struct {
  bool enabled;
  bool detector_ready;
  bool paused_for_motion;
  bool paused_for_spin;
  bool paused_for_servo;
  bool face_found;
  uint32_t last_frame_sequence;
  int pan_goal;
  int tilt_goal;
  int last_face_cx;
  int last_face_cy;
  int last_frame_w;
  int last_frame_h;
} nino_face_tracker_status_t;

void nino_face_tracker_init(void);
void nino_face_tracker_set_enabled(bool enabled);
bool nino_face_tracker_is_enabled(void);
void nino_face_tracker_set_detector_ready(bool ready);
void nino_face_tracker_update(bool face_found, int face_cx, int face_cy,
                              int frame_w, int frame_h,
                              uint32_t frame_sequence);
void nino_face_tracker_get_status(nino_face_tracker_status_t *out);

/** Latest detector hit (updated even when pan/tilt tracking is off). */
bool nino_face_tracker_face_seen(void);

/**
 * Pause pan/tilt tracking for scripted motion (hunt / look-scan) without
 * changing the user's enabled preference. Nested: each true needs a false.
 */
void nino_face_tracker_pause_scripted(bool pause);

/**
 * Slow pan/tilt search until timeout. Camera session must already be streaming.
 * When @p skip_if_visible is true and a face is already in frame, do not sweep.
 * Ends on the last pose where a face was seen so the server can identify them.
 */
bool nino_face_hunt_for_person(uint32_t timeout_ms, bool skip_if_visible);

/**
 * After TTS: start a background hunt only if the user is not in frame,
 * tracking is off (tracker owns the head), and a hunt is not already running.
 * Does not block the caller — listen can start immediately.
 */
bool nino_face_hunt_start_if_needed(uint32_t timeout_ms);

/** Same as nino_face_hunt_start_if_needed (post-TTS entry point). */
bool nino_face_hunt_after_tts(uint32_t timeout_ms);

bool nino_face_hunt_is_running(void);

/** Stop a background hunt so look-scan / STT can take the head. No-op if idle. */
void nino_face_hunt_cancel(void);

/** Wait until a background hunt finishes or @p timeout_ms elapses. */
void nino_face_hunt_wait_idle(uint32_t timeout_ms);

/**
 * Pan to @p pan_goal, wait for travel, then hold so overlay/YOLO can settle.
 * Tilt stays center. Caller should pause scripted tracking around this.
 */
void nino_face_look_hold_pan(int pan_goal, uint32_t hold_ms);

/** Same as nino_face_look_hold_pan, with an explicit Dynamixel moving speed. */
void nino_face_look_hold_pan_at_speed(int pan_goal, uint32_t hold_ms, int speed);

/**
 * Continuous pan to @p pan_goal (no intermediate steps, no camera hold).
 * Waits until present position is near the goal or @p timeout_ms elapses.
 */
void nino_face_pan_glide(int pan_goal, int speed, uint32_t timeout_ms);

#ifdef __cplusplus
}
#endif

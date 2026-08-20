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
 * Slow pan/tilt search until timeout. Camera session must already be streaming.
 * When @p skip_if_visible is true and a face is already in frame, do not sweep.
 * Ends on the last pose where a face was seen so the server can identify them.
 */
bool nino_face_hunt_for_person(uint32_t timeout_ms, bool skip_if_visible);

#ifdef __cplusplus
}
#endif

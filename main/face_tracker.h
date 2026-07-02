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

#ifdef __cplusplus
}
#endif

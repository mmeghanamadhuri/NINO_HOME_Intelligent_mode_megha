#include "face_tracker.h"

#include <math.h>
#include <string.h>

#include "esp_log.h"

#include "servo_dxl.h"
#include "servo_motion.h"

static const char *TAG = "face_tracker";

#define FACE_TRACK_PAN_SERVO_ID 2
#define FACE_TRACK_PAN_CENTER 512
#define FACE_TRACK_PAN_MIN 0
#define FACE_TRACK_PAN_MAX 1023
#define FACE_TRACK_DEADZONE_PX 12
#define FACE_TRACK_KP_PAN 0.14f
#define FACE_TRACK_MAX_STEP 150
#define FACE_TRACK_FACE_SMOOTHING 0.60f
#define FACE_TRACK_FACE_ACQUIRE_HITS 3
#define FACE_TRACK_FACE_LOST_FRAMES 45
#define FACE_TRACK_PAN_SIGN (1)

static bool s_enabled;
static bool s_detector_ready;
static bool s_paused_for_motion;
static bool s_paused_for_spin;
static bool s_paused_for_servo;
static bool s_face_found;
static uint32_t s_last_frame_sequence;
static int s_pan_goal = FACE_TRACK_PAN_CENTER;
static int s_last_face_cx;
static int s_last_frame_w;
static float s_smooth_x = -1.0f;
static int s_face_hits;
static int s_face_misses;

static int clampi(int value, int lo, int hi) {
  if (value < lo) {
    return lo;
  }
  if (value > hi) {
    return hi;
  }
  return value;
}

static void reset_track_state(void) {
  s_face_found = false;
  s_last_face_cx = 0;
  s_last_frame_w = 0;
  s_smooth_x = -1.0f;
  s_face_hits = 0;
  s_face_misses = 0;
}

static void update_pause_flags(void) {
  s_paused_for_motion = nino_servo_motion_is_active();
  s_paused_for_spin = nino_servo_dxl_spin_is_active() ||
                      nino_servo_dxl_track_hon_is_active();
  s_paused_for_servo = !nino_servo_dxl_is_ready();
}

void nino_face_tracker_init(void) {
  s_enabled = false;
  s_detector_ready = false;
  s_pan_goal = FACE_TRACK_PAN_CENTER;
  s_last_frame_sequence = 0;
  reset_track_state();
  update_pause_flags();
  ESP_LOGI(TAG, "Pan tracker ready on servo ID %d", FACE_TRACK_PAN_SERVO_ID);
}

void nino_face_tracker_set_enabled(bool enabled) {
  if (s_enabled == enabled) {
    return;
  }

  s_enabled = enabled;
  reset_track_state();
  update_pause_flags();

  if (enabled) {
    s_pan_goal = FACE_TRACK_PAN_CENTER;
    if (!s_paused_for_motion && !s_paused_for_spin && !s_paused_for_servo) {
      nino_servo_dxl_set_servo_goal(FACE_TRACK_PAN_SERVO_ID, s_pan_goal);
    }
    ESP_LOGI(TAG, "Pan tracking enabled");
  } else {
    if (!s_paused_for_motion && !s_paused_for_spin && !s_paused_for_servo) {
      s_pan_goal = FACE_TRACK_PAN_CENTER;
      nino_servo_dxl_set_servo_goal(FACE_TRACK_PAN_SERVO_ID, s_pan_goal);
    }
    ESP_LOGI(TAG, "Pan tracking disabled");
  }
}

bool nino_face_tracker_is_enabled(void) { return s_enabled; }

void nino_face_tracker_set_detector_ready(bool ready) { s_detector_ready = ready; }

void nino_face_tracker_update(bool face_found, int face_cx, int face_cy,
                              int frame_w, int frame_h,
                              uint32_t frame_sequence) {
  (void)face_cy;
  (void)frame_h;

  s_last_frame_sequence = frame_sequence;
  s_face_found = face_found;
  if (face_found) {
    s_last_face_cx = face_cx;
    s_last_frame_w = frame_w;
  }

  if (!s_enabled) {
    return;
  }

  update_pause_flags();
  if (s_paused_for_motion || s_paused_for_spin || s_paused_for_servo) {
    return;
  }

  if (!face_found || frame_w <= 0) {
    s_face_hits = 0;
    s_face_misses++;
    if (s_face_misses >= FACE_TRACK_FACE_LOST_FRAMES) {
      s_smooth_x = -1.0f;
    }
    return;
  }

  s_face_misses = 0;
  if (s_face_hits < FACE_TRACK_FACE_ACQUIRE_HITS) {
    s_face_hits++;
    s_smooth_x = (float)face_cx;
    return;
  }

  if (s_smooth_x < 0.0f) {
    s_smooth_x = (float)face_cx;
  } else {
    s_smooth_x += ((float)face_cx - s_smooth_x) * FACE_TRACK_FACE_SMOOTHING;
  }

  float err_x = (frame_w / 2.0f) - s_smooth_x;
  if (fabsf(err_x) <= FACE_TRACK_DEADZONE_PX) {
    return;
  }

  int delta = (int)lroundf(err_x * FACE_TRACK_KP_PAN);
  if (delta == 0) {
    delta = (err_x > 0.0f) ? 1 : -1;
  }
  delta = clampi(delta, -FACE_TRACK_MAX_STEP, FACE_TRACK_MAX_STEP);

  int new_pan = clampi(s_pan_goal + FACE_TRACK_PAN_SIGN * delta,
                       FACE_TRACK_PAN_MIN, FACE_TRACK_PAN_MAX);
  if (new_pan == s_pan_goal) {
    return;
  }

  s_pan_goal = new_pan;
  nino_servo_dxl_set_servo_goal(FACE_TRACK_PAN_SERVO_ID, s_pan_goal);
}

void nino_face_tracker_get_status(nino_face_tracker_status_t *out) {
  if (out == NULL) {
    return;
  }

  update_pause_flags();
  memset(out, 0, sizeof(*out));
  out->enabled = s_enabled;
  out->detector_ready = s_detector_ready;
  out->paused_for_motion = s_paused_for_motion;
  out->paused_for_spin = s_paused_for_spin;
  out->paused_for_servo = s_paused_for_servo;
  out->face_found = s_face_found;
  out->last_frame_sequence = s_last_frame_sequence;
  out->pan_goal = s_pan_goal;
  out->last_face_cx = s_last_face_cx;
  out->last_frame_w = s_last_frame_w;
}

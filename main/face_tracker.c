#include "face_tracker.h"

#include <math.h>
#include <string.h>

#include "esp_log.h"

#include "servo_dxl.h"
#include "servo_motion.h"
#include "servo_recplay.h"

static const char *TAG = "face_tracker";

#define FACE_TRACK_TILT_SERVO_ID 1
#define FACE_TRACK_PAN_SERVO_ID 2
#define FACE_TRACK_AXIS_CENTER 512
#define FACE_TRACK_PAN_MIN 0
#define FACE_TRACK_PAN_MAX 1023
/** AX 0–1023 scale: 28 units ~ 8° (same as servo_motion.c DXL_POSE_DELTA). */
#define FACE_TRACK_DXL_DEG_TO_UNITS(deg) (((deg) * 28 + 4) / 8)
/** AX moving-speed value: ~47°/s, responsive without aggressive overshoot. */
#define FACE_TRACK_POSITION_SPEED 70
/** Restore the regular head-motion speed when face tracking is disabled. */
#define FACE_TRACK_NORMAL_POSITION_SPEED 22
/** Tilt ID1 on this head: higher goal = physical up, lower goal = physical down. */
#define FACE_TRACK_TILT_UP_LIMIT_DEG 15
#define FACE_TRACK_TILT_DOWN_LIMIT_DEG 10
#define FACE_TRACK_TILT_MIN \
  (FACE_TRACK_AXIS_CENTER - FACE_TRACK_DXL_DEG_TO_UNITS(FACE_TRACK_TILT_DOWN_LIMIT_DEG))
#define FACE_TRACK_TILT_MAX \
  (FACE_TRACK_AXIS_CENTER + FACE_TRACK_DXL_DEG_TO_UNITS(FACE_TRACK_TILT_UP_LIMIT_DEG))
#define FACE_TRACK_DEADZONE_PX 12
#define FACE_TRACK_KP_PAN 0.14f
#define FACE_TRACK_KP_TILT 0.14f
#define FACE_TRACK_MAX_STEP 400
#define FACE_TRACK_FACE_SMOOTHING 0.60f
#define FACE_TRACK_FACE_ACQUIRE_HITS 3
#define FACE_TRACK_FACE_LOST_FRAMES 45
#define FACE_TRACK_PAN_SIGN (1)
#define FACE_TRACK_TILT_SIGN (1)

static bool s_enabled;
static bool s_detector_ready;
static bool s_paused_for_motion;
static bool s_paused_for_spin;
static bool s_paused_for_servo;
static bool s_face_found;
static uint32_t s_last_frame_sequence;
static int s_pan_goal = FACE_TRACK_AXIS_CENTER;
static int s_tilt_goal = FACE_TRACK_AXIS_CENTER;
static int s_last_face_cx;
static int s_last_face_cy;
static int s_last_frame_w;
static int s_last_frame_h;
static float s_smooth_x = -1.0f;
static float s_smooth_y = -1.0f;
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

static int axis_step_from_error(float err, float kp) {
  if (fabsf(err) <= FACE_TRACK_DEADZONE_PX) {
    return 0;
  }

  int delta = (int)lroundf(err * kp);
  if (delta == 0) {
    delta = (err > 0.0f) ? 1 : -1;
  }
  return clampi(delta, -FACE_TRACK_MAX_STEP, FACE_TRACK_MAX_STEP);
}

static void reset_track_state(void) {
  s_face_found = false;
  s_last_face_cx = 0;
  s_last_face_cy = 0;
  s_last_frame_w = 0;
  s_last_frame_h = 0;
  s_smooth_x = -1.0f;
  s_smooth_y = -1.0f;
  s_face_hits = 0;
  s_face_misses = 0;
}

static void go_neutral_if_allowed(void) {
  if (!s_paused_for_motion && !s_paused_for_spin && !s_paused_for_servo) {
    s_pan_goal = FACE_TRACK_AXIS_CENTER;
    s_tilt_goal = FACE_TRACK_AXIS_CENTER;
    nino_servo_dxl_set_pan_tilt(s_pan_goal, s_tilt_goal);
  }
}

static void update_pause_flags(void) {
  s_paused_for_motion = nino_servo_motion_is_active();
  s_paused_for_spin = nino_servo_dxl_spin_is_active() ||
                      nino_servo_dxl_track_hon_is_active() ||
                      nino_servo_recplay_is_busy();
  s_paused_for_servo = !nino_servo_dxl_is_ready();
}

void nino_face_tracker_init(void) {
  s_enabled = false;
  s_detector_ready = false;
  s_pan_goal = FACE_TRACK_AXIS_CENTER;
  s_tilt_goal = FACE_TRACK_AXIS_CENTER;
  s_last_frame_sequence = 0;
  reset_track_state();
  update_pause_flags();
  ESP_LOGI(TAG, "Pan/tilt tracker ready (tilt ID %d [%d..%d], pan ID %d)",
           FACE_TRACK_TILT_SERVO_ID, FACE_TRACK_TILT_MIN, FACE_TRACK_TILT_MAX,
           FACE_TRACK_PAN_SERVO_ID);
}

void nino_face_tracker_set_enabled(bool enabled) {
  if (s_enabled == enabled) {
    return;
  }

  s_enabled = enabled;
  reset_track_state();
  update_pause_flags();

  if (enabled) {
    nino_servo_dxl_set_position_speed(FACE_TRACK_POSITION_SPEED);
    go_neutral_if_allowed();
    ESP_LOGI(TAG, "Pan/tilt tracking enabled (speed %d)",
             FACE_TRACK_POSITION_SPEED);
  } else {
    nino_servo_dxl_set_position_speed(FACE_TRACK_NORMAL_POSITION_SPEED);
    go_neutral_if_allowed();
    ESP_LOGI(TAG, "Pan/tilt tracking disabled (speed %d)",
             FACE_TRACK_NORMAL_POSITION_SPEED);
  }
}

bool nino_face_tracker_is_enabled(void) { return s_enabled; }

void nino_face_tracker_set_detector_ready(bool ready) { s_detector_ready = ready; }

void nino_face_tracker_update(bool face_found, int face_cx, int face_cy,
                              int frame_w, int frame_h,
                              uint32_t frame_sequence) {
  s_last_frame_sequence = frame_sequence;
  s_face_found = face_found;
  if (face_found) {
    s_last_face_cx = face_cx;
    s_last_face_cy = face_cy;
    s_last_frame_w = frame_w;
    s_last_frame_h = frame_h;
  }

  if (!s_enabled) {
    return;
  }

  update_pause_flags();
  if (s_paused_for_motion || s_paused_for_spin || s_paused_for_servo) {
    return;
  }

  if (!face_found || frame_w <= 0 || frame_h <= 0) {
    s_face_hits = 0;
    s_face_misses++;
    if (s_face_misses >= FACE_TRACK_FACE_LOST_FRAMES) {
      s_smooth_x = -1.0f;
      s_smooth_y = -1.0f;
    }
    return;
  }

  s_face_misses = 0;
  if (s_face_hits < FACE_TRACK_FACE_ACQUIRE_HITS) {
    s_face_hits++;
    s_smooth_x = (float)face_cx;
    s_smooth_y = (float)face_cy;
    return;
  }

  if (s_smooth_x < 0.0f) {
    s_smooth_x = (float)face_cx;
  } else {
    s_smooth_x += ((float)face_cx - s_smooth_x) * FACE_TRACK_FACE_SMOOTHING;
  }

  if (s_smooth_y < 0.0f) {
    s_smooth_y = (float)face_cy;
  } else {
    s_smooth_y += ((float)face_cy - s_smooth_y) * FACE_TRACK_FACE_SMOOTHING;
  }

  const int pan_delta =
      axis_step_from_error((frame_w / 2.0f) - s_smooth_x, FACE_TRACK_KP_PAN);
  const int tilt_delta =
      axis_step_from_error((frame_h / 2.0f) - s_smooth_y, FACE_TRACK_KP_TILT);

  const int new_pan =
      clampi(s_pan_goal + FACE_TRACK_PAN_SIGN * pan_delta, FACE_TRACK_PAN_MIN,
             FACE_TRACK_PAN_MAX);
  const int new_tilt =
      clampi(s_tilt_goal + FACE_TRACK_TILT_SIGN * tilt_delta, FACE_TRACK_TILT_MIN,
             FACE_TRACK_TILT_MAX);
  if (new_pan == s_pan_goal && new_tilt == s_tilt_goal) {
    return;
  }

  s_pan_goal = new_pan;
  s_tilt_goal = new_tilt;
  nino_servo_dxl_set_pan_tilt(s_pan_goal, s_tilt_goal);
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
  out->tilt_goal = s_tilt_goal;
  out->last_face_cx = s_last_face_cx;
  out->last_face_cy = s_last_face_cy;
  out->last_frame_w = s_last_frame_w;
  out->last_frame_h = s_last_frame_h;
}

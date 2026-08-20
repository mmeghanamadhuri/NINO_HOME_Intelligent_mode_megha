#include "face_tracker.h"

#include <math.h>
#include <string.h>

#include "esp_log.h"

#include "servo_dxl.h"
#include "servo_motion.h"
#include "servo_recplay.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

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
/** Hunt head motion: slower than tracking so the overlay can ID a known face. */
#define FACE_HUNT_POSITION_SPEED 16
#define FACE_HUNT_POSE_MS 1600
#define FACE_HUNT_FACE_SETTLE_MS 400
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

bool nino_face_tracker_face_seen(void) { return s_face_found; }

bool nino_face_hunt_for_person(uint32_t timeout_ms, bool skip_if_visible) {
  const TickType_t deadline = xTaskGetTickCount() + pdMS_TO_TICKS(timeout_ms);
  const struct {
    int pan;
    int tilt;
  } poses[] = {
      {NINO_SERVO_AXIS_CENTER, NINO_SERVO_AXIS_CENTER},
      {NINO_SERVO_PAN_LEFT, NINO_SERVO_AXIS_CENTER},
      {NINO_SERVO_PAN_RIGHT, NINO_SERVO_AXIS_CENTER},
      {NINO_SERVO_AXIS_CENTER, NINO_SERVO_AXIS_CENTER},
      {NINO_SERVO_AXIS_CENTER, NINO_SERVO_TILT_UP},
      {NINO_SERVO_AXIS_CENTER, NINO_SERVO_TILT_DOWN},
      {NINO_SERVO_AXIS_CENTER, NINO_SERVO_AXIS_CENTER},
  };
  const size_t nposes = sizeof(poses) / sizeof(poses[0]);
  size_t pose_i = 0;
  TickType_t pose_until = xTaskGetTickCount();
  bool saw_face = false;
  int last_face_pan = NINO_SERVO_AXIS_CENTER;
  int last_face_tilt = NINO_SERVO_AXIS_CENTER;
  int cur_pan = NINO_SERVO_AXIS_CENTER;
  int cur_tilt = NINO_SERVO_AXIS_CENTER;

  ESP_LOGI(TAG, "Face hunt start (timeout %u ms, slow pan/tilt)", (unsigned)timeout_ms);

  /* After TTS, keep the current person if they are still in frame. */
  vTaskDelay(pdMS_TO_TICKS(220));
  if (skip_if_visible && s_face_found) {
    ESP_LOGI(TAG, "Face hunt: person already in frame");
    return true;
  }
  if (!skip_if_visible) {
    s_face_found = false;
  }

  if (nino_servo_dxl_is_ready() && !nino_servo_recplay_is_busy()) {
    nino_servo_dxl_set_position_speed(FACE_HUNT_POSITION_SPEED);
    cur_pan = poses[0].pan;
    cur_tilt = poses[0].tilt;
    nino_servo_dxl_set_pan_tilt(cur_pan, cur_tilt);
    pose_until = xTaskGetTickCount() + pdMS_TO_TICKS(FACE_HUNT_POSE_MS);
    pose_i = 1;
  }

  while (xTaskGetTickCount() < deadline) {
    if (s_face_found) {
      saw_face = true;
      last_face_pan = cur_pan;
      last_face_tilt = cur_tilt;
    }
    if (nino_servo_dxl_is_ready() && !nino_servo_recplay_is_busy() &&
        xTaskGetTickCount() >= pose_until && pose_i < nposes) {
      cur_pan = poses[pose_i].pan;
      cur_tilt = poses[pose_i].tilt;
      nino_servo_dxl_set_pan_tilt(cur_pan, cur_tilt);
      pose_i++;
      pose_until = xTaskGetTickCount() + pdMS_TO_TICKS(FACE_HUNT_POSE_MS);
    }
    vTaskDelay(pdMS_TO_TICKS(40));
  }

  if (nino_servo_dxl_is_ready() && !nino_servo_recplay_is_busy()) {
    if (saw_face) {
      nino_servo_dxl_set_pan_tilt(last_face_pan, last_face_tilt);
      vTaskDelay(pdMS_TO_TICKS(FACE_HUNT_FACE_SETTLE_MS));
    } else {
      nino_servo_dxl_go_neutral();
    }
    nino_servo_dxl_set_position_speed(FACE_TRACK_NORMAL_POSITION_SPEED);
  }

  ESP_LOGI(TAG, "Face hunt done (found=%d)", (int)(saw_face || s_face_found));
  return saw_face || s_face_found;
}

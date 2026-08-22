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
/** Hunt head motion: quick pan/tilt so hello identity is not a 10s wait. */
#define FACE_HUNT_POSITION_SPEED 40
#define FACE_HUNT_POSE_MS 500
#define FACE_HUNT_FACE_SETTLE_MS 180
#define FACE_HUNT_TASK_STACK 8192
#define FACE_HUNT_TASK_PRIO 4
#define FACE_HUNT_DEFAULT_MS 4500
#define FACE_LOOK_ARRIVE_TIMEOUT_MS 4500
#define FACE_LOOK_ARRIVE_TOLERANCE 18
/** Faster than face-track so a calibrated wide pan can actually reach the stop. */
#define FACE_LOOK_POSITION_SPEED 180
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
static int s_paused_for_script;
static bool s_face_found;
static volatile bool s_hunt_running;
static volatile bool s_hunt_cancel;
static uint32_t s_hunt_timeout_ms = FACE_HUNT_DEFAULT_MS;
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

/** Calibrated head sweep (NVS), falling back to the full AX range. */
static int track_pan_min(void) {
  const int left = nino_servo_pan_left();
  if (left < FACE_TRACK_PAN_MIN) {
    return FACE_TRACK_PAN_MIN;
  }
  return left;
}

static int track_pan_max(void) {
  const int right = nino_servo_pan_right();
  if (right > FACE_TRACK_PAN_MAX) {
    return FACE_TRACK_PAN_MAX;
  }
  return right;
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

static bool tracker_motion_blocked(void) {
  return s_paused_for_motion || s_paused_for_spin || s_paused_for_servo ||
         s_paused_for_script > 0;
}

static void go_neutral_if_allowed(void) {
  if (!tracker_motion_blocked()) {
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
  s_paused_for_script = 0;
  s_hunt_running = false;
  s_hunt_cancel = false;
  s_hunt_timeout_ms = FACE_HUNT_DEFAULT_MS;
  s_pan_goal = FACE_TRACK_AXIS_CENTER;
  s_tilt_goal = FACE_TRACK_AXIS_CENTER;
  s_last_frame_sequence = 0;
  reset_track_state();
  update_pause_flags();
  ESP_LOGI(TAG, "Pan/tilt tracker ready (tilt ID %d [%d..%d], pan ID %d [%d..%d])",
           FACE_TRACK_TILT_SERVO_ID, FACE_TRACK_TILT_MIN, FACE_TRACK_TILT_MAX,
           FACE_TRACK_PAN_SERVO_ID, track_pan_min(), track_pan_max());
}

void nino_face_tracker_set_enabled(bool enabled) {
  if (enabled) {
    nino_face_hunt_cancel();
  }
  if (s_enabled == enabled) {
    return;
  }

  s_enabled = enabled;
  reset_track_state();
  update_pause_flags();

  if (enabled) {
    nino_servo_dxl_set_position_speed(FACE_TRACK_POSITION_SPEED);
    /* Keep the current pan/tilt so tracking can start from where the face is. */
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
  if (tracker_motion_blocked()) {
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
      clampi(s_pan_goal + FACE_TRACK_PAN_SIGN * pan_delta, track_pan_min(),
             track_pan_max());
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

void nino_face_tracker_pause_scripted(bool pause) {
  if (pause) {
    s_paused_for_script++;
  } else if (s_paused_for_script > 0) {
    s_paused_for_script--;
  }
}

bool nino_face_hunt_for_person(uint32_t timeout_ms, bool skip_if_visible) {
  const TickType_t deadline = xTaskGetTickCount() + pdMS_TO_TICKS(timeout_ms);
  const struct {
    int pan;
    int tilt;
  } poses[] = {
      {NINO_SERVO_AXIS_CENTER, NINO_SERVO_AXIS_CENTER},
      {nino_servo_pan_left(), NINO_SERVO_AXIS_CENTER},
      {nino_servo_pan_right(), NINO_SERVO_AXIS_CENTER},
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

  nino_face_tracker_pause_scripted(true);
  /* Blocking INITIAL hunt is not cancellable. Background hunt may already have
   * a cancel queued between xTaskCreate and this point — do not clear it. */
  if (!s_hunt_running) {
    s_hunt_cancel = false;
  }
  ESP_LOGI(TAG, "Face hunt start (timeout %u ms)", (unsigned)timeout_ms);

  /* Let YuNet land a frame. If the user is already visible, skip the sweep. */
  vTaskDelay(pdMS_TO_TICKS(skip_if_visible ? 350 : 150));
  if (s_hunt_cancel) {
    ESP_LOGI(TAG, "Face hunt cancelled before sweep");
    nino_face_tracker_pause_scripted(false);
    return s_face_found;
  }
  if (skip_if_visible && s_face_found) {
    ESP_LOGI(TAG, "Face hunt: person already in frame");
    nino_face_tracker_pause_scripted(false);
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
    if (s_hunt_cancel) {
      ESP_LOGI(TAG, "Face hunt cancelled");
      break;
    }
    if (s_face_found) {
      saw_face = true;
      last_face_pan = cur_pan;
      last_face_tilt = cur_tilt;
      vTaskDelay(pdMS_TO_TICKS(FACE_HUNT_FACE_SETTLE_MS));
      if (s_face_found) {
        ESP_LOGI(TAG, "Face hunt: found, stop sweep");
        break;
      }
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
    nino_servo_dxl_set_position_speed(s_enabled ? FACE_TRACK_POSITION_SPEED
                                                : FACE_TRACK_NORMAL_POSITION_SPEED);
  }

  ESP_LOGI(TAG, "Face hunt done (found=%d)", (int)(saw_face || s_face_found));
  nino_face_tracker_pause_scripted(false);
  return saw_face || s_face_found;
}

static void face_hunt_task(void *arg) {
  (void)arg;
  const uint32_t timeout_ms = s_hunt_timeout_ms;
  (void)nino_face_hunt_for_person(timeout_ms, true);
  s_hunt_running = false;
  s_hunt_cancel = false;
  vTaskDelete(NULL);
}

bool nino_face_hunt_start_if_needed(uint32_t timeout_ms) {
  /* Tracker owns the head — do not start a competing sweep. */
  if (nino_face_tracker_is_enabled()) {
    ESP_LOGI(TAG, "Face hunt skip: tracker owns the head");
    return false;
  }
  if (nino_face_tracker_face_seen()) {
    ESP_LOGI(TAG, "Face hunt skip: person already in frame");
    return false;
  }
  if (s_hunt_running) {
    ESP_LOGI(TAG, "Face hunt skip: already running");
    return false;
  }

  s_hunt_timeout_ms = timeout_ms > 0 ? timeout_ms : FACE_HUNT_DEFAULT_MS;
  s_hunt_cancel = false;
  s_hunt_running = true;
  BaseType_t ok =
      xTaskCreate(face_hunt_task, "face_hunt", FACE_HUNT_TASK_STACK, NULL,
                  FACE_HUNT_TASK_PRIO, NULL);
  if (ok != pdPASS) {
    s_hunt_running = false;
    ESP_LOGW(TAG, "Face hunt task create failed");
    return false;
  }
  ESP_LOGI(TAG, "Face hunt started in background (timeout %u ms)",
           (unsigned)s_hunt_timeout_ms);
  return true;
}

bool nino_face_hunt_after_tts(uint32_t timeout_ms) {
  return nino_face_hunt_start_if_needed(timeout_ms);
}

bool nino_face_hunt_is_running(void) { return s_hunt_running; }

void nino_face_hunt_cancel(void) {
  if (!s_hunt_running) {
    return;
  }
  s_hunt_cancel = true;
}

void nino_face_hunt_wait_idle(uint32_t timeout_ms) {
  const TickType_t deadline = xTaskGetTickCount() + pdMS_TO_TICKS(timeout_ms);
  while (s_hunt_running && xTaskGetTickCount() < deadline) {
    vTaskDelay(pdMS_TO_TICKS(50));
  }
}

static bool wait_pan_arrived(int pan_goal, uint32_t timeout_ms) {
  const TickType_t start = xTaskGetTickCount();
  const TickType_t timeout = pdMS_TO_TICKS(timeout_ms);
  while ((xTaskGetTickCount() - start) < timeout) {
    int present = 0;
    if (nino_servo_dxl_get_present_position(NINO_SERVO_PAN_ID, &present) == ESP_OK) {
      int delta = present - pan_goal;
      if (delta < 0) {
        delta = -delta;
      }
      if (delta <= FACE_LOOK_ARRIVE_TOLERANCE) {
        return true;
      }
    }
    vTaskDelay(pdMS_TO_TICKS(40));
  }
  return false;
}

static void restore_look_speed(void) {
  if (nino_servo_dxl_is_ready() && !nino_servo_recplay_is_busy()) {
    nino_servo_dxl_set_position_speed(s_enabled ? FACE_TRACK_POSITION_SPEED
                                                : FACE_TRACK_NORMAL_POSITION_SPEED);
  }
}

void nino_face_look_hold_pan(int pan_goal, uint32_t hold_ms) {
  nino_face_look_hold_pan_at_speed(pan_goal, hold_ms, FACE_LOOK_POSITION_SPEED);
}

void nino_face_look_hold_pan_at_speed(int pan_goal, uint32_t hold_ms, int speed) {
  if (speed < 1) {
    speed = 1;
  }
  if (nino_servo_dxl_is_ready() && !nino_servo_recplay_is_busy()) {
    nino_servo_dxl_set_position_speed(speed);
    nino_servo_dxl_set_pan_tilt(pan_goal, NINO_SERVO_AXIS_CENTER);
  }
  if (!wait_pan_arrived(pan_goal, FACE_LOOK_ARRIVE_TIMEOUT_MS)) {
    ESP_LOGW(TAG, "look pan=%d did not arrive within %d ms", pan_goal,
             FACE_LOOK_ARRIVE_TIMEOUT_MS);
  }
  if (hold_ms > 0) {
    vTaskDelay(pdMS_TO_TICKS(hold_ms));
  }
  restore_look_speed();
}

void nino_face_pan_glide(int pan_goal, int speed, uint32_t timeout_ms) {
  if (speed < 1) {
    speed = 1;
  }
  if (timeout_ms < 500) {
    timeout_ms = 500;
  }
  if (nino_servo_dxl_is_ready() && !nino_servo_recplay_is_busy()) {
    nino_servo_dxl_set_position_speed(speed);
    nino_servo_dxl_set_pan_tilt(pan_goal, NINO_SERVO_AXIS_CENTER);
  }
  if (!wait_pan_arrived(pan_goal, timeout_ms)) {
    ESP_LOGW(TAG, "glide pan=%d did not arrive within %u ms", pan_goal,
             (unsigned)timeout_ms);
  }
  restore_look_speed();
}

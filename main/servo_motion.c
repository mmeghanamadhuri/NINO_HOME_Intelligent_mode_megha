#include "servo_motion.h"

#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "nvs.h"

#include "battery_endurance.h"
#include "face_tracker.h"
#include "servo_dxl.h"
#include "servo_recplay.h"

static const char *TAG = "servo_motion";

#define NVS_NS "nino_servo"
#define NVS_KEY_PAN_L "pan_l"
#define NVS_KEY_PAN_R "pan_r"
#define PAN_CAPTURE_MIN_SPAN 30
#define PAN_CAL_HOLD_MS 1500

#define POSE_HOLD_MS          700
#define SWEEP_HOLD_MS         1000
#define SWEEP_SPEED           400
#define DXL_CENTER            NINO_SERVO_AXIS_CENTER
#define PAN_LEFT              NINO_SERVO_PAN_LEFT
#define PAN_RIGHT             NINO_SERVO_PAN_RIGHT
#define TILT_UP               NINO_SERVO_TILT_UP
#define TILT_DOWN             NINO_SERVO_TILT_DOWN

/* Proven track-hon pan range, plus a visible tilt sweep. */
#define SWEEP_PAN_CW          800
#define SWEEP_PAN_CCW         212
#define SWEEP_TILT_CW         700
#define SWEEP_TILT_CCW        350

#define MOTION_TASK_STACK     3072
#define MOTION_TASK_PRIO      5

static volatile bool s_motion_run;
static volatile nino_servo_motion_mode_t s_motion_mode = NINO_SERVO_MOTION_FULL;
static TaskHandle_t s_motion_task;
static SemaphoreHandle_t s_motion_done;

static int s_pan_left = NINO_SERVO_PAN_LEFT;
static int s_pan_right = NINO_SERVO_PAN_RIGHT;
static bool s_have_saved;
static bool s_capturing;
static int s_cap_min;
static int s_cap_max;
static int s_cap_samples;

static int clamp_axis(int v) {
  if (v < 0) {
    return 0;
  }
  if (v > 1023) {
    return 1023;
  }
  return v;
}

static void persist_pan_limits(int left, int right) {
  nvs_handle_t h;
  if (nvs_open(NVS_NS, NVS_READWRITE, &h) != ESP_OK) {
    return;
  }
  if (nvs_set_i32(h, NVS_KEY_PAN_L, (int32_t)left) == ESP_OK &&
      nvs_set_i32(h, NVS_KEY_PAN_R, (int32_t)right) == ESP_OK) {
    nvs_commit(h);
  }
  nvs_close(h);
}

void nino_servo_limits_init(void) {
  s_pan_left = NINO_SERVO_PAN_LEFT;
  s_pan_right = NINO_SERVO_PAN_RIGHT;
  s_have_saved = false;
  nvs_handle_t h;
  if (nvs_open(NVS_NS, NVS_READONLY, &h) != ESP_OK) {
    ESP_LOGI(TAG, "pan limits default left=%d right=%d", s_pan_left, s_pan_right);
    return;
  }
  int32_t left = 0;
  int32_t right = 0;
  const esp_err_t e1 = nvs_get_i32(h, NVS_KEY_PAN_L, &left);
  const esp_err_t e2 = nvs_get_i32(h, NVS_KEY_PAN_R, &right);
  nvs_close(h);
  if (e1 == ESP_OK && e2 == ESP_OK && right - left >= PAN_CAPTURE_MIN_SPAN) {
    s_pan_left = clamp_axis((int)left);
    s_pan_right = clamp_axis((int)right);
    s_have_saved = true;
  }
  ESP_LOGI(TAG, "pan limits %s left=%d right=%d",
           s_have_saved ? "NVS" : "default", s_pan_left, s_pan_right);
}

int nino_servo_pan_left(void) { return s_pan_left; }

int nino_servo_pan_right(void) { return s_pan_right; }

void nino_servo_limits_observe_pan(int present) {
  if (!s_capturing) {
    return;
  }
  if (present < 0 || present > 1023) {
    return;
  }
  if (s_cap_samples == 0) {
    s_cap_min = present;
    s_cap_max = present;
  } else {
    if (present < s_cap_min) {
      s_cap_min = present;
    }
    if (present > s_cap_max) {
      s_cap_max = present;
    }
  }
  s_cap_samples++;
}

void nino_servo_limits_capture_begin(void) {
  if (s_capturing) {
    return;
  }
  s_capturing = true;
  s_cap_samples = 0;
  s_cap_min = 1023;
  s_cap_max = 0;
  ESP_LOGI(TAG, "pan capture start — recording farthest left/right present position");
}

static bool persist_captured_extrema(void) {
  if (!s_capturing || s_cap_samples < 4 || s_cap_max - s_cap_min < PAN_CAPTURE_MIN_SPAN) {
    return false;
  }
  int left = s_cap_min;
  int right = s_cap_max;
  if (s_have_saved) {
    if (s_pan_left < left) {
      left = s_pan_left;
    }
    if (s_pan_right > right) {
      right = s_pan_right;
    }
  }
  if (s_have_saved && left == s_pan_left && right == s_pan_right) {
    return false;
  }
  s_pan_left = left;
  s_pan_right = right;
  s_have_saved = true;
  persist_pan_limits(left, right);
  ESP_LOGI(TAG, "pan limits saved left=%d right=%d span=%d (samples=%d)", left, right,
           right - left, s_cap_samples);
  return true;
}

bool nino_servo_limits_capture_end(void) {
  if (!s_capturing) {
    return false;
  }
  const bool saved = persist_captured_extrema();
  if (!saved && (s_cap_samples < 4 || s_cap_max - s_cap_min < PAN_CAPTURE_MIN_SPAN)) {
    ESP_LOGW(TAG, "pan capture ignored samples=%d min=%d max=%d", s_cap_samples,
             s_cap_min, s_cap_max);
  }
  s_capturing = false;
  return saved;
}

static void observe_present_pan(void) {
  int pan = 0;
  if (nino_servo_dxl_get_present_position(NINO_SERVO_PAN_ID, &pan) == ESP_OK) {
    nino_servo_limits_observe_pan(pan);
  }
}

static void hold_and_observe(uint32_t hold_ms) {
  const TickType_t until = xTaskGetTickCount() + pdMS_TO_TICKS(hold_ms);
  while (s_motion_run && xTaskGetTickCount() < until) {
    observe_present_pan();
    vTaskDelay(pdMS_TO_TICKS(40));
  }
}

esp_err_t nino_servo_pan_calibrate(void) {
  if (!nino_servo_dxl_is_ready()) {
    return ESP_ERR_INVALID_STATE;
  }
  if (nino_servo_dxl_spin_is_active() || nino_servo_dxl_track_hon_is_active() ||
      nino_servo_recplay_is_busy() || nino_servo_motion_is_active()) {
    return ESP_ERR_INVALID_STATE;
  }
  nino_face_tracker_pause_scripted(true);
  nino_servo_limits_capture_begin();
  nino_servo_dxl_set_position_speed(400);
  nino_servo_dxl_set_pan_tilt(SWEEP_PAN_CCW, DXL_CENTER);
  {
    const TickType_t until = xTaskGetTickCount() + pdMS_TO_TICKS(PAN_CAL_HOLD_MS);
    while (xTaskGetTickCount() < until) {
      observe_present_pan();
      vTaskDelay(pdMS_TO_TICKS(40));
    }
  }
  nino_servo_dxl_set_pan_tilt(SWEEP_PAN_CW, DXL_CENTER);
  {
    const TickType_t until = xTaskGetTickCount() + pdMS_TO_TICKS(PAN_CAL_HOLD_MS);
    while (xTaskGetTickCount() < until) {
      observe_present_pan();
      vTaskDelay(pdMS_TO_TICKS(40));
    }
  }
  nino_servo_dxl_go_neutral();
  const bool saved = nino_servo_limits_capture_end();
  nino_face_tracker_pause_scripted(false);
  return saved ? ESP_OK : ESP_ERR_INVALID_RESPONSE;
}

static const char *motion_mode_name(nino_servo_motion_mode_t mode) {
  switch (mode) {
  case NINO_SERVO_MOTION_NOD_LR:
    return "nod L/R";
  case NINO_SERVO_MOTION_SWEEP:
    return "CW/CCW sweep";
  default:
    return "L/R/U/D";
  }
}

static void motion_task(void *arg) {
  (void)arg;
  unsigned step = 0;
  bool warned_not_ready = false;

  while (s_motion_run) {
    if (!nino_servo_dxl_is_ready()) {
      if (!warned_not_ready) {
        ESP_LOGW(TAG,
                 "U2D2/servos not ready yet — poses queued (check J18 hub + servo power). "
                 "bus=%s",
                 nino_servo_dxl_bus_open() ? "open" : "down");
        warned_not_ready = true;
      }
    } else if (warned_not_ready) {
      ESP_LOGI(TAG, "Servos ready — sweep will start moving now");
      warned_not_ready = false;
    }

    /* Always queue goals so the Dynamixel task applies them as soon as the bus is up. */
    if (s_motion_mode == NINO_SERVO_MOTION_SWEEP) {
      nino_servo_dxl_set_position_speed(SWEEP_SPEED);
      const bool cw_phase = ((step & 1U) == 0U);
      const int pan = cw_phase ? SWEEP_PAN_CW : SWEEP_PAN_CCW;
      const int tilt = cw_phase ? SWEEP_TILT_CCW : SWEEP_TILT_CW;
      nino_servo_dxl_set_pan_tilt(pan, tilt);
      ESP_LOGI(TAG, "sweep %s  pan=%d tilt=%d  bus=%s torque=%s",
               cw_phase ? "CW" : "CCW", pan, tilt,
               nino_servo_dxl_bus_open() ? "open" : "down",
               nino_servo_dxl_is_ready() ? "on" : "off");
    } else if (s_motion_mode == NINO_SERVO_MOTION_NOD_LR) {
      if ((step & 1U) == 0U) {
        nino_servo_dxl_set_pan_tilt(PAN_LEFT, DXL_CENTER);
      } else {
        nino_servo_dxl_set_pan_tilt(PAN_RIGHT, DXL_CENTER);
      }
    } else {
      /* Return to centre after each direction so every pose starts from neutral:
       * right/up/down are ±12°, while left is deliberately wider at 15°. */
      switch (step % 8U) {
      case 0U:
        nino_servo_dxl_set_pan_tilt(PAN_RIGHT, DXL_CENTER);
        break;
      case 1U:
        nino_servo_dxl_set_pan_tilt(DXL_CENTER, DXL_CENTER);
        break;
      case 2U:
        nino_servo_dxl_set_pan_tilt(PAN_LEFT, DXL_CENTER);
        break;
      case 3U:
        nino_servo_dxl_set_pan_tilt(DXL_CENTER, DXL_CENTER);
        break;
      case 4U:
        nino_servo_dxl_set_pan_tilt(DXL_CENTER, TILT_UP);
        break;
      case 5U:
        nino_servo_dxl_set_pan_tilt(DXL_CENTER, DXL_CENTER);
        break;
      case 6U:
        nino_servo_dxl_set_pan_tilt(DXL_CENTER, TILT_DOWN);
        break;
      default:
        nino_servo_dxl_set_pan_tilt(DXL_CENTER, DXL_CENTER);
        break;
      }
    }

    step++;
    const uint32_t hold_ms =
        (s_motion_mode == NINO_SERVO_MOTION_SWEEP) ? SWEEP_HOLD_MS : POSE_HOLD_MS;
    if (s_motion_mode == NINO_SERVO_MOTION_SWEEP) {
      hold_and_observe(hold_ms);
      /* GPIO48 hwtest: first CW then CCW is one full left/right pass. */
      if (step >= 2) {
        (void)persist_captured_extrema();
      }
    } else {
      vTaskDelay(pdMS_TO_TICKS(hold_ms));
    }
  }

  if (s_motion_mode == NINO_SERVO_MOTION_SWEEP) {
    (void)nino_servo_limits_capture_end();
  }
  nino_servo_dxl_go_neutral();
  if (s_motion_done != NULL) {
    xSemaphoreGive(s_motion_done);
  }
  s_motion_task = NULL;
  vTaskDelete(NULL);
}

void nino_servo_motion_start(nino_servo_motion_mode_t mode) {
  /* Hardware test owns the SWEEP mode; block other motion while it runs. */
  if (nino_battery_endurance_is_active() && mode != NINO_SERVO_MOTION_SWEEP) {
    ESP_LOGI(TAG, "Head motion suppressed while hardware test is active");
    return;
  }
  if (nino_servo_dxl_track_hon_is_active()) {
    ESP_LOGI(TAG, "Head motion suppressed while track hon is active");
    return;
  }

  s_motion_mode = mode;

  if (s_motion_task != NULL && s_motion_run) {
    ESP_LOGD(TAG, "Motion already running — mode=%s", motion_mode_name(mode));
    return;
  }

  if (s_motion_task != NULL) {
    s_motion_task = NULL;
  }

  s_motion_run = true;
  if (mode == NINO_SERVO_MOTION_SWEEP) {
    nino_servo_limits_capture_begin();
  }
  if (s_motion_done == NULL) {
    s_motion_done = xSemaphoreCreateBinary();
  }
  if (s_motion_done != NULL) {
    xSemaphoreTake(s_motion_done, 0);
  }

  ESP_LOGI(TAG, "Head motion start (%s), U2D2 %s, servos %s",
           motion_mode_name(mode),
           nino_servo_dxl_bus_open() ? "open" : "waiting",
           nino_servo_dxl_is_ready() ? "ready" : "not ready (no PING yet?)");

  BaseType_t ok = xTaskCreate(motion_task, "servo_motion", MOTION_TASK_STACK, NULL,
                              MOTION_TASK_PRIO, &s_motion_task);
  if (ok != pdPASS) {
    ESP_LOGE(TAG, "Failed to create motion task");
    s_motion_task = NULL;
    s_motion_run = false;
  }
}

void nino_servo_motion_stop(void) {
  if (nino_battery_endurance_owns_actuators()) {
    ESP_LOGD(TAG, "Motion stop ignored — hardware test owns the motors");
    return;
  }

  if (!s_motion_run && s_motion_task == NULL) {
    nino_servo_dxl_go_neutral();
    return;
  }

  s_motion_run = false;
  if (s_motion_task != NULL && s_motion_done != NULL) {
    if (xSemaphoreTake(s_motion_done, pdMS_TO_TICKS(SWEEP_HOLD_MS * 3)) != pdTRUE) {
      ESP_LOGW(TAG, "Motion task stop timeout");
      s_motion_task = NULL;
    }
  }
  nino_servo_dxl_go_neutral();
}

bool nino_servo_motion_is_active(void) {
  return s_motion_run || (s_motion_task != NULL);
}

#include "servo_motion.h"

#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"

#include "battery_endurance.h"
#include "servo_dxl.h"

static const char *TAG = "servo_motion";

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
    vTaskDelay(pdMS_TO_TICKS(hold_ms));
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

#include "servo_motion.h"

#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"

#include "servo_dxl.h"

static const char *TAG = "servo_motion";

#define POSE_HOLD_MS          550
#define DXL_CENTER            512
/** ~10° on AX-scale 0–1023 (≈34 ticks); slightly less is fine per user. */
#define DXL_POSE_DELTA        34

#define PAN_LEFT              (DXL_CENTER - DXL_POSE_DELTA)
#define PAN_RIGHT             (DXL_CENTER + DXL_POSE_DELTA)
#define TILT_UP               (DXL_CENTER - DXL_POSE_DELTA)
#define TILT_DOWN             (DXL_CENTER + DXL_POSE_DELTA)

#define MOTION_TASK_STACK     3072
#define MOTION_TASK_PRIO      5

static volatile bool s_motion_run;
static volatile nino_servo_motion_mode_t s_motion_mode = NINO_SERVO_MOTION_FULL;
static TaskHandle_t s_motion_task;
static SemaphoreHandle_t s_motion_done;

static void motion_task(void *arg) {
  (void)arg;
  unsigned step = 0;
  bool warned_not_ready = false;

  while (s_motion_run) {
    if (!nino_servo_dxl_is_ready()) {
      if (!warned_not_ready) {
        ESP_LOGW(TAG, "U2D2/servos not ready yet — poses queued (check J18 hub + servo power)");
        warned_not_ready = true;
      }
    } else {
      warned_not_ready = false;
    }

    /* Always queue goals so the Dynamixel task applies them as soon as the bus is up. */
    if (s_motion_mode == NINO_SERVO_MOTION_NOD_LR) {
      if ((step & 1U) == 0U) {
        nino_servo_dxl_set_pan_tilt(PAN_LEFT, DXL_CENTER);
      } else {
        nino_servo_dxl_set_pan_tilt(PAN_RIGHT, DXL_CENTER);
      }
    } else {
      switch (step % 4U) {
      case 0U:
        nino_servo_dxl_set_pan_tilt(PAN_LEFT, DXL_CENTER);
        break;
      case 1U:
        nino_servo_dxl_set_pan_tilt(PAN_RIGHT, DXL_CENTER);
        break;
      case 2U:
        nino_servo_dxl_set_pan_tilt(DXL_CENTER, TILT_UP);
        break;
      default:
        nino_servo_dxl_set_pan_tilt(DXL_CENTER, TILT_DOWN);
        break;
      }
    }

    step++;
    vTaskDelay(pdMS_TO_TICKS(POSE_HOLD_MS));
  }

  nino_servo_dxl_go_neutral();
  if (s_motion_done != NULL) {
    xSemaphoreGive(s_motion_done);
  }
  s_motion_task = NULL;
  vTaskDelete(NULL);
}

void nino_servo_motion_start(nino_servo_motion_mode_t mode) {
  s_motion_mode = mode;

  if (s_motion_task != NULL && s_motion_run) {
    ESP_LOGD(TAG, "Motion already running — mode=%s",
             mode == NINO_SERVO_MOTION_NOD_LR ? "nod L/R" : "L/R/U/D");
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

  ESP_LOGI(TAG, "Head motion start (%s), servo bus %s",
           mode == NINO_SERVO_MOTION_NOD_LR ? "nod L/R" : "L/R/U/D",
           nino_servo_dxl_bus_open() ? "open" : "waiting for U2D2");

  BaseType_t ok = xTaskCreate(motion_task, "servo_motion", MOTION_TASK_STACK, NULL,
                              MOTION_TASK_PRIO, &s_motion_task);
  if (ok != pdPASS) {
    ESP_LOGE(TAG, "Failed to create motion task");
    s_motion_task = NULL;
    s_motion_run = false;
  }
}

void nino_servo_motion_stop(void) {
  if (!s_motion_run && s_motion_task == NULL) {
    nino_servo_dxl_go_neutral();
    return;
  }

  s_motion_run = false;
  if (s_motion_task != NULL && s_motion_done != NULL) {
    if (xSemaphoreTake(s_motion_done, pdMS_TO_TICKS(POSE_HOLD_MS * 4)) != pdTRUE) {
      ESP_LOGW(TAG, "Motion task stop timeout");
      s_motion_task = NULL;
    }
  }
  nino_servo_dxl_go_neutral();
}

bool nino_servo_motion_is_active(void) {
  return s_motion_run || (s_motion_task != NULL);
}

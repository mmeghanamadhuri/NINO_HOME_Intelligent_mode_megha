#include "battery_endurance.h"

#include <stdio.h>
#include <string.h>

#include "esp_console.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"

#include "camera_stream.h"
#include "face_tracker.h"
#include "nino_eye.h"
#include "rgb_led.h"
#include "servo_dxl.h"
#include "servo_motion.h"
#include "servo_recplay.h"

static const char *TAG = "hw_test";

#define LOOP_MS 50
#define RGB_STEP_MS 400
#define EYE_STEP_MS 5000
#define LOG_STEP_MS 5000
#define MILESTONE_MS 60000

#define TASK_STACK 4096
#define TASK_PRIO 4

typedef struct {
  const char *name;
  uint8_t r;
  uint8_t g;
  uint8_t b;
} soak_color_t;

typedef struct {
  nino_eye_state_t state;
  const char *name;
} soak_expr_t;

static const soak_color_t s_colors[] = {
    {"red", 255, 0, 0},       {"orange", 255, 102, 0}, {"yellow", 255, 255, 0},
    {"green", 0, 255, 0},     {"cyan", 0, 255, 255},   {"blue", 0, 0, 255},
    {"purple", 153, 0, 255},  {"magenta", 255, 0, 255}, {"pink", 255, 51, 102},
    {"white", 255, 255, 255}, {"lime", 128, 255, 0},    {"warm", 255, 153, 51},
};

static const soak_expr_t s_exprs[] = {
    {NINO_EYE_HAPPY, "happy"},         {NINO_EYE_SURPRISED, "surprised"},
    {NINO_EYE_CURIOUS_QUIZ, "curious"}, {NINO_EYE_SMILE, "smile"},
    {NINO_EYE_THINKING, "thinking"},    {NINO_EYE_SPARKLE, "sparkle"},
    {NINO_EYE_LISTENING, "listening"},  {NINO_EYE_BIGSMILE, "bigsmile"},
    {NINO_EYE_SAD, "sad"},              {NINO_EYE_ROBOT, "robot"},
    {NINO_EYE_MAD, "mad"},              {NINO_EYE_TIRED, "tired"},
};

#define COLOR_COUNT ((int)(sizeof(s_colors) / sizeof(s_colors[0])))
#define EXPR_COUNT ((int)(sizeof(s_exprs) / sizeof(s_exprs[0])))

static SemaphoreHandle_t s_lock;
static TaskHandle_t s_task;
static SemaphoreHandle_t s_done;
static volatile bool s_run;
static volatile bool s_active;

static bool s_face_track_was_on;
static int64_t s_start_us;
static bool s_warned_no_cam;
static bool s_warned_no_servo;
static int s_expr_log_left;

static void format_elapsed(int64_t elapsed_us, char *buf, size_t buflen) {
  if (elapsed_us < 0) {
    elapsed_us = 0;
  }
  const int64_t total_s = elapsed_us / 1000000LL;
  const int hours = (int)(total_s / 3600);
  const int mins = (int)((total_s % 3600) / 60);
  const int secs = (int)(total_s % 60);
  snprintf(buf, buflen, "%02d:%02d:%02d", hours, mins, secs);
}

static void apply_rgb(int index) {
  const soak_color_t *c = &s_colors[index % COLOR_COUNT];
  (void)nino_rgb_led_set_rgb(c->r, c->g, c->b);
}

static void apply_eye(int index) {
  const soak_expr_t *e = &s_exprs[index % EXPR_COUNT];
  nino_eye_set_state(e->state);
}

static const char *servo_status(void) {
  if (nino_servo_dxl_is_ready()) {
    return "READY";
  }
  if (nino_servo_dxl_bus_open()) {
    return "U2D2 OPEN (waiting PING/torque)";
  }
  return "U2D2 DOWN";
}

static const char *cam_state(uint32_t *seq_out) {
  const uint32_t seq = nino_uvc_frame_sequence();
  if (seq_out != NULL) {
    *seq_out = seq;
  }
  if (nino_uvc_camera_connected()) {
    if (nino_camera_is_streaming() && seq > 0U) {
      return "STREAMING";
    }
    return "CONNECTED (no frame yet)";
  }
  return "NOT CONNECTED";
}

static void log_heartbeat(int64_t now_us, int color_i, int expr_i,
                          uint32_t last_cam_seq) {
  char elapsed[16];
  format_elapsed(now_us - s_start_us, elapsed, sizeof(elapsed));

  uint32_t cam_seq = 0;
  const char *cam = cam_state(&cam_seq);
  const uint32_t fps_est = (cam_seq >= last_cam_seq)
                               ? (uint32_t)((cam_seq - last_cam_seq) * 1000U / LOG_STEP_MS)
                               : 0;

  ESP_LOGI(TAG, "t=%s  cam=%s #%lu ~%u fps  motors=%s  eye=%s  rgb=%s",
           elapsed, cam, (unsigned long)cam_seq, (unsigned)fps_est, servo_status(),
           s_exprs[expr_i % EXPR_COUNT].name, s_colors[color_i % COLOR_COUNT].name);

  if (!nino_uvc_camera_connected() && !s_warned_no_cam) {
    s_warned_no_cam = true;
    ESP_LOGW(TAG, "USB camera not streaming — plug UVC cam into the HOST hub");
  } else if (nino_uvc_camera_connected()) {
    s_warned_no_cam = false;
  }

  if (!nino_servo_dxl_is_ready() && !s_warned_no_servo) {
    s_warned_no_servo = true;
    ESP_LOGW(TAG, "Servos not ready — check hub + U2D2 + Dynamixel power");
  } else if (nino_servo_dxl_is_ready()) {
    s_warned_no_servo = false;
  }
}

static void log_start_banner(void) {
  uint32_t cam_seq = 0;
  const char *cam = cam_state(&cam_seq);

  ESP_LOGI(TAG, "========== HARDWARE TEST START ==========");
  ESP_LOGI(TAG, "cam=%s #%lu   servos=%s", cam, (unsigned long)cam_seq,
           servo_status());
  ESP_LOGI(TAG, "motors: continuous pan+tilt CW/CCW until the same button is pressed");
  ESP_LOGI(TAG, "camera stream  |  RGB cycle %d ms  |  TFT expr every %d s",
           RGB_STEP_MS, EYE_STEP_MS / 1000);
  ESP_LOGI(TAG, "Press GPIO48 once more (or type 'hwtest') to STOP");
}

static void log_stop_banner(void) {
  char elapsed[16];
  format_elapsed(esp_timer_get_time() - s_start_us, elapsed, sizeof(elapsed));
  ESP_LOGI(TAG, "========== HARDWARE TEST STOP ==========");
  ESP_LOGI(TAG, "ran %s  motors parked at center  LED off  eyes idle", elapsed);
}

static void restore_idle(void) {
  nino_servo_motion_stop();
  nino_servo_dxl_go_neutral();
  (void)nino_rgb_led_show(NINO_RGB_SHOW_IDLE);
  nino_eye_idle();
  nino_camera_set_session_active(false);
  if (s_face_track_was_on) {
    nino_face_tracker_set_enabled(true);
  }
}

static void soak_task(void *arg) {
  (void)arg;

  int color_i = 0;
  int expr_i = 0;
  uint32_t rgb_ms = 0;
  uint32_t eye_ms = 0;
  uint32_t log_ms = 0;
  uint32_t milestone_ms = 0;
  uint32_t last_cam_seq = nino_uvc_frame_sequence();

  (void)nino_rgb_led_show(NINO_RGB_SHOW_IDLE);
  apply_rgb(0);
  apply_eye(0);
  nino_servo_motion_start(NINO_SERVO_MOTION_SWEEP);

  while (s_run) {
    vTaskDelay(pdMS_TO_TICKS(LOOP_MS));
    rgb_ms += LOOP_MS;
    eye_ms += LOOP_MS;
    log_ms += LOOP_MS;
    milestone_ms += LOOP_MS;

    if (!nino_servo_motion_is_active()) {
      nino_servo_motion_start(NINO_SERVO_MOTION_SWEEP);
    }

    if (rgb_ms >= RGB_STEP_MS) {
      rgb_ms = 0;
      color_i = (color_i + 1) % COLOR_COUNT;
      apply_rgb(color_i);
    }

    if (eye_ms >= EYE_STEP_MS) {
      eye_ms = 0;
      expr_i = (expr_i + 1) % EXPR_COUNT;
      apply_eye(expr_i);
      if (s_expr_log_left > 0) {
        s_expr_log_left--;
        ESP_LOGI(TAG, "TFT expression -> %s", s_exprs[expr_i].name);
      }
    }

    if (log_ms >= LOG_STEP_MS) {
      log_ms = 0;
      log_heartbeat(esp_timer_get_time(), color_i, expr_i, last_cam_seq);
      last_cam_seq = nino_uvc_frame_sequence();
    }

    if (milestone_ms >= MILESTONE_MS) {
      milestone_ms = 0;
      char elapsed[16];
      format_elapsed(esp_timer_get_time() - s_start_us, elapsed, sizeof(elapsed));
      ESP_LOGI(TAG, "--- milestone %s still running ---", elapsed);
    }
  }

  restore_idle();
  log_stop_banner();
  s_active = false;
  s_task = NULL;
  if (s_done != NULL) {
    xSemaphoreGive(s_done);
  }
  vTaskDelete(NULL);
}

static bool lock_take(void) {
  if (s_lock == NULL) {
    return false;
  }
  return xSemaphoreTake(s_lock, pdMS_TO_TICKS(500)) == pdTRUE;
}

static void lock_give(void) {
  if (s_lock != NULL) {
    xSemaphoreGive(s_lock);
  }
}

esp_err_t nino_battery_endurance_init(void) {
  if (s_lock == NULL) {
    s_lock = xSemaphoreCreateMutex();
    if (s_lock == NULL) {
      return ESP_ERR_NO_MEM;
    }
  }
  if (s_done == NULL) {
    s_done = xSemaphoreCreateBinary();
    if (s_done == NULL) {
      return ESP_ERR_NO_MEM;
    }
  }
  ESP_LOGI(TAG,
           "Ready — GPIO48 single press starts/stops hardware test "
           "(motors + camera + RGB + TFT). CLI: hwtest");
  return ESP_OK;
}

bool nino_battery_endurance_is_active(void) {
  return s_active;
}

bool nino_battery_endurance_owns_actuators(void) {
  return s_run;
}

bool nino_battery_endurance_is_self(void) {
  return s_run && s_task != NULL && xTaskGetCurrentTaskHandle() == s_task;
}

esp_err_t nino_battery_endurance_start(void) {
  if (!lock_take()) {
    return ESP_ERR_INVALID_STATE;
  }

  if (s_active) {
    lock_give();
    ESP_LOGI(TAG, "Test already running — press GPIO48 once to stop");
    return ESP_OK;
  }

  if (nino_servo_recplay_is_busy()) {
    lock_give();
    ESP_LOGW(TAG, "Cannot start: servo record/play is busy");
    return ESP_ERR_INVALID_STATE;
  }

  if (nino_servo_dxl_spin_is_active() || nino_servo_dxl_track_hon_is_active()) {
    ESP_LOGI(TAG, "Stopping spin/track-hon so the test can own both motors");
    (void)nino_servo_dxl_track_hon_stop();
  }
  nino_servo_motion_stop();

  s_face_track_was_on = nino_face_tracker_is_enabled();
  if (s_face_track_was_on) {
    nino_face_tracker_set_enabled(false);
  }

  nino_camera_set_session_active(true);

  s_start_us = esp_timer_get_time();
  s_warned_no_cam = false;
  s_warned_no_servo = false;
  s_expr_log_left = 6;
  s_run = true;
  s_active = true;
  if (s_done != NULL) {
    (void)xSemaphoreTake(s_done, 0);
  }

  log_start_banner();

  BaseType_t ok = xTaskCreate(soak_task, "hw_test", TASK_STACK, NULL, TASK_PRIO,
                              &s_task);
  if (ok != pdPASS) {
    s_run = false;
    s_active = false;
    s_task = NULL;
    nino_camera_set_session_active(false);
    if (s_face_track_was_on) {
      nino_face_tracker_set_enabled(true);
    }
    lock_give();
    ESP_LOGE(TAG, "Failed to create hardware test task");
    return ESP_ERR_NO_MEM;
  }

  lock_give();
  return ESP_OK;
}

void nino_battery_endurance_stop(void) {
  if (!lock_take()) {
    return;
  }
  if (!s_active && s_task == NULL) {
    lock_give();
    ESP_LOGI(TAG, "Hardware test is not running");
    return;
  }

  ESP_LOGI(TAG, "Stop requested — winding down motors / LED / TFT");
  s_run = false;
  TaskHandle_t task = s_task;
  lock_give();

  if (task != NULL && s_done != NULL) {
    if (xSemaphoreTake(s_done, pdMS_TO_TICKS(2000)) != pdTRUE) {
      ESP_LOGW(TAG, "Hardware test task stop timeout");
    }
  }
}

void nino_battery_endurance_toggle(void) {
  if (s_active) {
    nino_battery_endurance_stop();
  } else {
    (void)nino_battery_endurance_start();
  }
}

static int cmd_hwtest(int argc, char **argv) {
  if (argc >= 2) {
    if (strcmp(argv[1], "on") == 0 || strcmp(argv[1], "start") == 0) {
      return nino_battery_endurance_start() == ESP_OK ? 0 : 1;
    }
    if (strcmp(argv[1], "off") == 0 || strcmp(argv[1], "stop") == 0) {
      nino_battery_endurance_stop();
      return 0;
    }
    if (strcmp(argv[1], "status") == 0) {
      printf("hardware test: %s\n", s_active ? "RUNNING" : "idle");
      return 0;
    }
    printf("Usage: hwtest [on|off|status]\n");
    return 1;
  }
  nino_battery_endurance_toggle();
  return 0;
}

void nino_battery_endurance_cli_register(void) {
  const esp_console_cmd_t cmd = {
      .command = "hwtest",
      .help = "hwtest | on | off | status  — GPIO48 motors+cam+RGB+TFT load test",
      .hint = NULL,
      .func = &cmd_hwtest,
      .argtable = NULL,
  };
  ESP_ERROR_CHECK(esp_console_cmd_register(&cmd));
}

#include "servo_recplay.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "esp_check.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#include "face_tracker.h"
#include "servo_dxl.h"
#include "servo_motion.h"

static const char *TAG = "servo_recplay";

#define RECPLAY_TASK_STACK 4096
#define RECPLAY_TASK_PRIO 4
#define RECPLAY_NEAR_TIMEOUT_MS 8000
#define RECPLAY_DEFAULT_SPEED 22

static nino_servo_mode_t s_mode = NINO_SERVO_MODE_IDLE;
static uint8_t s_record_id_mask;
static bool s_restore_face_track;
static bool s_face_track_was_enabled;
static TaskHandle_t s_play_task;
static volatile bool s_play_stop;
static nino_servo_play_frame_t s_play_frames[NINO_SERVO_PLAY_MAX_FRAMES];
static size_t s_play_count;
static int s_play_speed = RECPLAY_DEFAULT_SPEED;

static const char *mode_str(nino_servo_mode_t mode) {
  switch (mode) {
  case NINO_SERVO_MODE_RECORD:
    return "record";
  case NINO_SERVO_MODE_PLAY:
    return "play";
  default:
    return "idle";
  }
}

static void pause_other_owners(void) {
  nino_servo_motion_stop();
  s_face_track_was_enabled = nino_face_tracker_is_enabled();
  if (s_face_track_was_enabled) {
    nino_face_tracker_set_enabled(false);
  }
  s_restore_face_track = true;
}

static void restore_other_owners(void) {
  if (s_restore_face_track && s_face_track_was_enabled) {
    nino_face_tracker_set_enabled(true);
  }
  s_restore_face_track = false;
  s_face_track_was_enabled = false;
}

static bool id_selected(uint8_t mask, uint8_t id) {
  if (mask == 0) {
    return true;
  }
  if (id == NINO_SERVO_TILT_ID) {
    return (mask & 0x01) != 0;
  }
  if (id == NINO_SERVO_PAN_ID) {
    return (mask & 0x02) != 0;
  }
  return false;
}

static esp_err_t apply_torque_mask(uint8_t mask, bool enable) {
  esp_err_t first = ESP_OK;
  const uint8_t ids[NINO_SERVO_ID_COUNT] = {NINO_SERVO_TILT_ID, NINO_SERVO_PAN_ID};
  for (size_t i = 0; i < NINO_SERVO_ID_COUNT; i++) {
    if (!id_selected(mask, ids[i])) {
      continue;
    }
    esp_err_t err = nino_servo_dxl_set_torque(ids[i], enable);
    if (err != ESP_OK && first == ESP_OK) {
      first = err;
    }
  }
  return first;
}

static bool wait_near_or_hold(const nino_servo_play_frame_t *frame, uint32_t hold_ms) {
  const int64_t start_us = esp_timer_get_time();
  const int64_t hold_us = (int64_t)hold_ms * 1000LL;
  const int64_t max_us = (int64_t)RECPLAY_NEAR_TIMEOUT_MS * 1000LL;
  bool near = false;

  while (!s_play_stop) {
    const int64_t elapsed = esp_timer_get_time() - start_us;
    if (!near) {
      bool ok = true;
      if (frame->has_tilt) {
        int pos = 0;
        if (nino_servo_dxl_get_present_position(NINO_SERVO_TILT_ID, &pos) != ESP_OK ||
            (pos > frame->tilt ? pos - frame->tilt : frame->tilt - pos) > 15) {
          ok = false;
        }
      }
      if (frame->has_pan) {
        int pos = 0;
        if (nino_servo_dxl_get_present_position(NINO_SERVO_PAN_ID, &pos) != ESP_OK ||
            (pos > frame->pan ? pos - frame->pan : frame->pan - pos) > 15) {
          ok = false;
        }
      }
      if (ok) {
        near = true;
      }
    }

    if (near && elapsed >= hold_us) {
      return true;
    }
    if (elapsed >= max_us && elapsed >= hold_us) {
      return true;
    }
    vTaskDelay(pdMS_TO_TICKS(20));
  }
  return false;
}

static void play_task(void *arg) {
  (void)arg;
  ESP_LOGI(TAG, "Play start (%u frames, speed=%d)", (unsigned)s_play_count, s_play_speed);
  nino_servo_dxl_set_position_speed(s_play_speed);

  for (size_t i = 0; i < s_play_count && !s_play_stop; i++) {
    const nino_servo_play_frame_t *f = &s_play_frames[i];
    if (f->has_tilt && f->has_pan) {
      nino_servo_dxl_set_pan_tilt(f->pan, f->tilt);
    } else if (f->has_tilt) {
      nino_servo_dxl_set_servo_goal(NINO_SERVO_TILT_ID, f->tilt);
    } else if (f->has_pan) {
      nino_servo_dxl_set_servo_goal(NINO_SERVO_PAN_ID, f->pan);
    } else {
      continue;
    }

    uint32_t hold = f->hold_ms;
    if (i == 0 && hold == 0) {
      /* First frame: still wait briefly for arrival. */
      hold = 50;
    }
    if (!wait_near_or_hold(f, hold)) {
      break;
    }
  }

  s_mode = NINO_SERVO_MODE_IDLE;
  s_play_task = NULL;
  restore_other_owners();
  ESP_LOGI(TAG, "Play finished (stopped=%d)", (int)s_play_stop);
  s_play_stop = false;
  vTaskDelete(NULL);
}

void nino_servo_recplay_init(void) {
  s_mode = NINO_SERVO_MODE_IDLE;
  s_record_id_mask = 0;
  s_restore_face_track = false;
  s_face_track_was_enabled = false;
  s_play_task = NULL;
  s_play_stop = false;
  s_play_count = 0;
}

nino_servo_mode_t nino_servo_recplay_mode(void) { return s_mode; }

bool nino_servo_recplay_is_busy(void) {
  return s_mode == NINO_SERVO_MODE_RECORD || s_mode == NINO_SERVO_MODE_PLAY;
}

esp_err_t nino_servo_recplay_record_start(uint8_t id_mask, bool torque_off) {
  if (s_mode == NINO_SERVO_MODE_PLAY) {
    return ESP_ERR_INVALID_STATE;
  }
  if (nino_servo_dxl_spin_is_active() || nino_servo_dxl_track_hon_is_active()) {
    return ESP_ERR_INVALID_STATE;
  }
  if (!nino_servo_dxl_bus_open()) {
    return ESP_ERR_INVALID_STATE;
  }

  if (s_mode != NINO_SERVO_MODE_RECORD) {
    pause_other_owners();
  }

  s_record_id_mask = id_mask;
  s_mode = NINO_SERVO_MODE_RECORD;

  if (torque_off) {
    esp_err_t err = apply_torque_mask(id_mask, false);
    if (err != ESP_OK) {
      ESP_LOGW(TAG, "torque off failed: %s", esp_err_to_name(err));
      return err;
    }
  }

  ESP_LOGI(TAG, "Record mode on (mask=0x%02x torque_off=%d)", id_mask, (int)torque_off);
  return ESP_OK;
}

esp_err_t nino_servo_recplay_record_stop(void) {
  if (s_mode != NINO_SERVO_MODE_RECORD) {
    return ESP_OK;
  }

  (void)apply_torque_mask(s_record_id_mask, true);
  s_mode = NINO_SERVO_MODE_IDLE;
  s_record_id_mask = 0;
  restore_other_owners();
  ESP_LOGI(TAG, "Record mode off");
  return ESP_OK;
}

esp_err_t nino_servo_recplay_play(const nino_servo_play_frame_t *frames, size_t count,
                                  int speed) {
  if (frames == NULL || count == 0 || count > NINO_SERVO_PLAY_MAX_FRAMES) {
    return ESP_ERR_INVALID_ARG;
  }
  if (s_mode != NINO_SERVO_MODE_IDLE || s_play_task != NULL) {
    return ESP_ERR_INVALID_STATE;
  }
  if (nino_servo_dxl_spin_is_active() || nino_servo_dxl_track_hon_is_active()) {
    return ESP_ERR_INVALID_STATE;
  }
  if (!nino_servo_dxl_is_ready()) {
    return ESP_ERR_INVALID_STATE;
  }

  pause_other_owners();
  (void)nino_servo_dxl_set_torque(NINO_SERVO_TILT_ID, true);
  (void)nino_servo_dxl_set_torque(NINO_SERVO_PAN_ID, true);
  memcpy(s_play_frames, frames, count * sizeof(nino_servo_play_frame_t));
  s_play_count = count;
  s_play_speed = (speed > 0) ? speed : RECPLAY_DEFAULT_SPEED;
  s_play_stop = false;
  s_mode = NINO_SERVO_MODE_PLAY;

  BaseType_t ok = xTaskCreate(play_task, "servo_play", RECPLAY_TASK_STACK, NULL, RECPLAY_TASK_PRIO,
                              &s_play_task);
  if (ok != pdPASS) {
    s_play_task = NULL;
    s_mode = NINO_SERVO_MODE_IDLE;
    restore_other_owners();
    return ESP_ERR_NO_MEM;
  }
  return ESP_OK;
}

esp_err_t nino_servo_recplay_play_stop(void) {
  if (s_mode != NINO_SERVO_MODE_PLAY) {
    return ESP_OK;
  }
  s_play_stop = true;
  return ESP_OK;
}

int nino_servo_recplay_status_json(char *buf, size_t buf_sz) {
  if (buf == NULL || buf_sz == 0) {
    return -1;
  }
  return snprintf(buf, buf_sz,
                  "\"ready\":%s,\"mode\":\"%s\",\"ids_online\":[1,2]",
                  nino_servo_dxl_is_ready() ? "true" : "false", mode_str(s_mode));
}

int nino_servo_recplay_position_json(char *buf, size_t buf_sz) {
  if (buf == NULL || buf_sz == 0) {
    return -1;
  }

  int tilt = 0;
  int pan = 0;
  const bool bus = nino_servo_dxl_bus_open();
  esp_err_t e1 = bus ? nino_servo_dxl_get_present_position(NINO_SERVO_TILT_ID, &tilt)
                     : ESP_ERR_INVALID_STATE;
  esp_err_t e2 = bus ? nino_servo_dxl_get_present_position(NINO_SERVO_PAN_ID, &pan)
                     : ESP_ERR_INVALID_STATE;

  return snprintf(
      buf, buf_sz,
      "{\"ok\":true,\"ready\":%s,\"mode\":\"%s\","
      "\"servos\":["
      "{\"id\":1,\"position\":%d,\"torque\":%s,\"ok\":%s},"
      "{\"id\":2,\"position\":%d,\"torque\":%s,\"ok\":%s}"
      "]}",
      nino_servo_dxl_is_ready() ? "true" : "false", mode_str(s_mode), tilt,
      nino_servo_dxl_torque_is_on(NINO_SERVO_TILT_ID) ? "true" : "false",
      e1 == ESP_OK ? "true" : "false", pan,
      nino_servo_dxl_torque_is_on(NINO_SERVO_PAN_ID) ? "true" : "false",
      e2 == ESP_OK ? "true" : "false");
}

static void recplay_cors(httpd_req_t *req) {
  httpd_resp_set_hdr(req, "Access-Control-Allow-Origin", "*");
  httpd_resp_set_hdr(req, "Access-Control-Allow-Methods", "GET, POST, OPTIONS");
  httpd_resp_set_hdr(req, "Access-Control-Allow-Headers", "Content-Type");
}

static esp_err_t recplay_send_json(httpd_req_t *req, const char *status, const char *body) {
  httpd_resp_set_type(req, "application/json");
  recplay_cors(req);
  if (status != NULL) {
    httpd_resp_set_status(req, status);
  }
  return httpd_resp_send(req, body, HTTPD_RESP_USE_STRLEN);
}

static esp_err_t recplay_recv_body(httpd_req_t *req, char *buf, size_t buf_sz, size_t *out_len) {
  if (buf == NULL || buf_sz < 2) {
    return ESP_ERR_INVALID_ARG;
  }
  size_t total = 0;
  int remaining = req->content_len;
  if (remaining < 0) {
    remaining = 0;
  }
  if ((size_t)remaining >= buf_sz) {
    return ESP_ERR_INVALID_SIZE;
  }
  while (remaining > 0) {
    int r = httpd_req_recv(req, buf + total, remaining);
    if (r <= 0) {
      return ESP_FAIL;
    }
    total += (size_t)r;
    remaining -= r;
  }
  buf[total] = '\0';
  if (out_len) {
    *out_len = total;
  }
  return ESP_OK;
}

static bool json_has_false(const char *body, const char *key) {
  char needle[48];
  snprintf(needle, sizeof(needle), "\"%s\"", key);
  const char *p = strstr(body, needle);
  if (p == NULL) {
    return false;
  }
  p = strchr(p, ':');
  if (p == NULL) {
    return false;
  }
  return strstr(p, "false") != NULL && (strstr(p, "true") == NULL || strstr(p, "false") < strstr(p, "true"));
}

static int json_get_int_after(const char *start, const char *end, const char *key, int *out) {
  char needle[32];
  snprintf(needle, sizeof(needle), "\"%s\"", key);
  const char *p = strstr(start, needle);
  if (p == NULL || (end != NULL && p >= end)) {
    return -1;
  }
  p = strchr(p, ':');
  if (p == NULL || (end != NULL && p >= end)) {
    return -1;
  }
  p++;
  while (*p == ' ' || *p == '\t') {
    p++;
  }
  char *endp = NULL;
  long v = strtol(p, &endp, 10);
  if (endp == p) {
    return -1;
  }
  *out = (int)v;
  return 0;
}

static size_t parse_play_frames(const char *body, nino_servo_play_frame_t *frames, size_t max_frames) {
  const char *arr = strstr(body, "\"frames\"");
  if (arr == NULL) {
    return 0;
  }
  arr = strchr(arr, '[');
  if (arr == NULL) {
    return 0;
  }
  arr++;
  size_t count = 0;
  const char *p = arr;
  while (*p && count < max_frames) {
    while (*p && *p != '{') {
      if (*p == ']') {
        return count;
      }
      p++;
    }
    if (*p != '{') {
      break;
    }
    const char *obj = p;
    int depth = 0;
    const char *end = NULL;
    for (const char *q = obj; *q; q++) {
      if (*q == '{') {
        depth++;
      } else if (*q == '}') {
        depth--;
        if (depth == 0) {
          end = q + 1;
          break;
        }
      }
    }
    if (end == NULL) {
      break;
    }

    nino_servo_play_frame_t f = {};
    int hold = 500;
    (void)json_get_int_after(obj, end, "hold_ms", &hold);
    if (hold < 0) {
      hold = 0;
    }
    f.hold_ms = (uint32_t)hold;

    const char *pobj = strstr(obj, "\"p\"");
    const char *scan_from = obj;
    const char *scan_end = end;
    if (pobj != NULL && pobj < end) {
      scan_from = pobj;
    }
    int v1 = 0;
    int v2 = 0;
    if (json_get_int_after(scan_from, scan_end, "1", &v1) == 0) {
      f.has_tilt = true;
      f.tilt = v1;
    }
    if (json_get_int_after(scan_from, scan_end, "2", &v2) == 0) {
      f.has_pan = true;
      f.pan = v2;
    }
    /* Also accept "tilt" / "pan" aliases. */
    if (!f.has_tilt && json_get_int_after(obj, end, "tilt", &v1) == 0) {
      f.has_tilt = true;
      f.tilt = v1;
    }
    if (!f.has_pan && json_get_int_after(obj, end, "pan", &v2) == 0) {
      f.has_pan = true;
      f.pan = v2;
    }

    if (f.has_tilt || f.has_pan) {
      frames[count++] = f;
    }
    p = end;
  }
  return count;
}

static uint8_t parse_id_mask(const char *body) {
  uint8_t mask = 0;
  const char *ids = strstr(body, "\"ids\"");
  if (ids == NULL) {
    return 0; /* both */
  }
  const char *arr = strchr(ids, '[');
  const char *end = arr ? strchr(arr, ']') : NULL;
  if (arr == NULL || end == NULL) {
    return 0;
  }
  for (const char *p = arr; p < end; p++) {
    if (*p == '1') {
      mask |= 0x01;
    } else if (*p == '2') {
      mask |= 0x02;
    }
  }
  return mask;
}

static esp_err_t servo_position_handler(httpd_req_t *req) {
  if (req->method == HTTP_OPTIONS) {
    httpd_resp_set_status(req, "204 No Content");
    recplay_cors(req);
    return httpd_resp_send(req, NULL, 0);
  }
  if (req->method != HTTP_GET) {
    return recplay_send_json(req, "405 Method Not Allowed",
                             "{\"ok\":false,\"error\":\"GET only\"}");
  }
  char body[384];
  int n = nino_servo_recplay_position_json(body, sizeof(body));
  if (n < 0 || n >= (int)sizeof(body)) {
    return httpd_resp_send_500(req);
  }
  httpd_resp_set_type(req, "application/json");
  recplay_cors(req);
  return httpd_resp_send(req, body, n);
}

static esp_err_t servo_record_handler(httpd_req_t *req) {
  if (req->method == HTTP_OPTIONS) {
    httpd_resp_set_status(req, "204 No Content");
    recplay_cors(req);
    return httpd_resp_send(req, NULL, 0);
  }
  if (req->method != HTTP_POST) {
    return recplay_send_json(req, "405 Method Not Allowed",
                             "{\"ok\":false,\"error\":\"POST only\"}");
  }

  char body[256];
  esp_err_t recv = recplay_recv_body(req, body, sizeof(body), NULL);
  if (recv == ESP_ERR_INVALID_SIZE) {
    return recplay_send_json(req, "413 Payload Too Large",
                             "{\"ok\":false,\"error\":\"body_too_large\"}");
  }
  if (recv != ESP_OK) {
    body[0] = '\0';
  }

  const bool is_stop = strstr(body, "\"stop\"") != NULL &&
                       (strstr(body, "\"start\"") == NULL ||
                        strstr(body, "\"stop\"") < strstr(body, "\"start\""));
  if (is_stop || strstr(body, "\"action\":\"stop\"") != NULL ||
      strstr(body, "\"action\": \"stop\"") != NULL) {
    (void)nino_servo_recplay_record_stop();
    return recplay_send_json(req, NULL, "{\"ok\":true,\"mode\":\"idle\"}");
  }

  const uint8_t mask = parse_id_mask(body);
  const bool torque_off = !json_has_false(body, "torque_off");
  esp_err_t err = nino_servo_recplay_record_start(mask, torque_off);
  if (err == ESP_ERR_INVALID_STATE) {
    const char *why = nino_servo_recplay_mode() == NINO_SERVO_MODE_PLAY ? "busy_play"
                      : !nino_servo_dxl_bus_open()                      ? "servos_not_ready"
                                                                        : "busy";
    char msg[96];
    snprintf(msg, sizeof(msg), "{\"ok\":false,\"error\":\"%s\"}", why);
    return recplay_send_json(req, "409 Conflict", msg);
  }
  if (err != ESP_OK) {
    return recplay_send_json(req, "500 Internal Server Error",
                             "{\"ok\":false,\"error\":\"torque_failed\"}");
  }
  return recplay_send_json(req, NULL, "{\"ok\":true,\"mode\":\"record\"}");
}

static esp_err_t servo_goal_handler(httpd_req_t *req) {
  if (req->method == HTTP_OPTIONS) {
    httpd_resp_set_status(req, "204 No Content");
    recplay_cors(req);
    return httpd_resp_send(req, NULL, 0);
  }
  if (req->method != HTTP_POST) {
    return recplay_send_json(req, "405 Method Not Allowed",
                             "{\"ok\":false,\"error\":\"POST only\"}");
  }
  if (nino_servo_recplay_mode() == NINO_SERVO_MODE_RECORD) {
    return recplay_send_json(req, "409 Conflict",
                             "{\"ok\":false,\"error\":\"busy_record\"}");
  }

  char body[192];
  if (recplay_recv_body(req, body, sizeof(body), NULL) != ESP_OK) {
    body[0] = '\0';
  }

  int speed = 0;
  if (json_get_int_after(body, NULL, "speed", &speed) == 0 && speed > 0) {
    nino_servo_dxl_set_position_speed(speed);
  }

  int id = 0;
  int position = 0;
  int pan = 0;
  int tilt = 0;
  bool have_id = json_get_int_after(body, NULL, "id", &id) == 0 &&
                 json_get_int_after(body, NULL, "position", &position) == 0;
  bool have_pan = json_get_int_after(body, NULL, "pan", &pan) == 0;
  bool have_tilt = json_get_int_after(body, NULL, "tilt", &tilt) == 0;

  if (have_id) {
    nino_servo_dxl_set_servo_goal((uint8_t)id, position);
  } else if (have_pan || have_tilt) {
    if (!have_pan) {
      (void)nino_servo_dxl_get_present_position(NINO_SERVO_PAN_ID, &pan);
    }
    if (!have_tilt) {
      (void)nino_servo_dxl_get_present_position(NINO_SERVO_TILT_ID, &tilt);
    }
    nino_servo_dxl_set_pan_tilt(pan, tilt);
  } else {
    return recplay_send_json(req, "400 Bad Request",
                             "{\"ok\":false,\"error\":\"need id+position or pan/tilt\"}");
  }

  return recplay_send_json(req, NULL, "{\"ok\":true}");
}

static esp_err_t servo_play_handler(httpd_req_t *req) {
  if (req->method == HTTP_OPTIONS) {
    httpd_resp_set_status(req, "204 No Content");
    recplay_cors(req);
    return httpd_resp_send(req, NULL, 0);
  }
  if (req->method != HTTP_POST) {
    return recplay_send_json(req, "405 Method Not Allowed",
                             "{\"ok\":false,\"error\":\"POST only\"}");
  }

  char *body = calloc(1, 12288);
  if (body == NULL) {
    return httpd_resp_send_500(req);
  }
  esp_err_t recv = recplay_recv_body(req, body, 12288, NULL);
  if (recv == ESP_ERR_INVALID_SIZE) {
    free(body);
    return recplay_send_json(req, "413 Payload Too Large",
                             "{\"ok\":false,\"error\":\"body_too_large\"}");
  }
  if (recv != ESP_OK) {
    body[0] = '\0';
  }

  if (strstr(body, "\"action\":\"stop\"") != NULL ||
      strstr(body, "\"action\": \"stop\"") != NULL ||
      (strstr(body, "\"stop\"") != NULL && strstr(body, "\"frames\"") == NULL)) {
    (void)nino_servo_recplay_play_stop();
    free(body);
    return recplay_send_json(req, NULL, "{\"ok\":true,\"stopping\":true}");
  }

  nino_servo_play_frame_t frames[NINO_SERVO_PLAY_MAX_FRAMES];
  size_t count = parse_play_frames(body, frames, NINO_SERVO_PLAY_MAX_FRAMES);
  int speed = RECPLAY_DEFAULT_SPEED;
  (void)json_get_int_after(body, NULL, "speed", &speed);
  free(body);

  if (count == 0) {
    return recplay_send_json(req, "400 Bad Request",
                             "{\"ok\":false,\"error\":\"no_frames\"}");
  }

  esp_err_t err = nino_servo_recplay_play(frames, count, speed);
  if (err == ESP_ERR_INVALID_STATE) {
    const char *why = !nino_servo_dxl_is_ready() ? "servos_not_ready"
                      : nino_servo_recplay_is_busy() ? "busy"
                                                     : "busy";
    char msg[96];
    snprintf(msg, sizeof(msg), "{\"ok\":false,\"error\":\"%s\"}", why);
    return recplay_send_json(req, "409 Conflict", msg);
  }
  if (err != ESP_OK) {
    return recplay_send_json(req, "500 Internal Server Error",
                             "{\"ok\":false,\"error\":\"play_failed\"}");
  }

  char ok[80];
  snprintf(ok, sizeof(ok), "{\"ok\":true,\"started\":true,\"frames\":%u}", (unsigned)count);
  return recplay_send_json(req, NULL, ok);
}

esp_err_t nino_servo_recplay_register_http(httpd_handle_t server) {
  if (server == NULL) {
    return ESP_ERR_INVALID_ARG;
  }

  const httpd_uri_t position_get = {
      .uri = "/servo/position",
      .method = HTTP_GET,
      .handler = servo_position_handler,
  };
  const httpd_uri_t position_opts = {
      .uri = "/servo/position",
      .method = HTTP_OPTIONS,
      .handler = servo_position_handler,
  };
  const httpd_uri_t record_post = {
      .uri = "/servo/record",
      .method = HTTP_POST,
      .handler = servo_record_handler,
  };
  const httpd_uri_t record_opts = {
      .uri = "/servo/record",
      .method = HTTP_OPTIONS,
      .handler = servo_record_handler,
  };
  const httpd_uri_t goal_post = {
      .uri = "/servo/goal",
      .method = HTTP_POST,
      .handler = servo_goal_handler,
  };
  const httpd_uri_t goal_opts = {
      .uri = "/servo/goal",
      .method = HTTP_OPTIONS,
      .handler = servo_goal_handler,
  };
  const httpd_uri_t play_post = {
      .uri = "/servo/play",
      .method = HTTP_POST,
      .handler = servo_play_handler,
  };
  const httpd_uri_t play_opts = {
      .uri = "/servo/play",
      .method = HTTP_OPTIONS,
      .handler = servo_play_handler,
  };

  ESP_RETURN_ON_ERROR(httpd_register_uri_handler(server, &position_get), TAG, "pos get");
  ESP_RETURN_ON_ERROR(httpd_register_uri_handler(server, &position_opts), TAG, "pos opts");
  ESP_RETURN_ON_ERROR(httpd_register_uri_handler(server, &record_post), TAG, "record post");
  ESP_RETURN_ON_ERROR(httpd_register_uri_handler(server, &record_opts), TAG, "record opts");
  ESP_RETURN_ON_ERROR(httpd_register_uri_handler(server, &goal_post), TAG, "goal post");
  ESP_RETURN_ON_ERROR(httpd_register_uri_handler(server, &goal_opts), TAG, "goal opts");
  ESP_RETURN_ON_ERROR(httpd_register_uri_handler(server, &play_post), TAG, "play post");
  ESP_RETURN_ON_ERROR(httpd_register_uri_handler(server, &play_opts), TAG, "play opts");
  return ESP_OK;
}

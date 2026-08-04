#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "esp_err.h"
#include "esp_http_server.h"

#ifdef __cplusplus
extern "C" {
#endif

typedef enum {
  NINO_SERVO_MODE_IDLE = 0,
  NINO_SERVO_MODE_RECORD = 1,
  NINO_SERVO_MODE_PLAY = 2,
} nino_servo_mode_t;

#define NINO_SERVO_PLAY_MAX_FRAMES 64

typedef struct {
  uint32_t hold_ms;
  bool has_tilt; /* ID1 */
  bool has_pan;  /* ID2 */
  int tilt;
  int pan;
} nino_servo_play_frame_t;

void nino_servo_recplay_init(void);

nino_servo_mode_t nino_servo_recplay_mode(void);

/** True while record or play owns the servos. */
bool nino_servo_recplay_is_busy(void);

/**
 * Enter record/edit mode. Stops audio head motion, remembers face-track state,
 * optionally torque-off selected IDs (bit0=ID1, bit1=ID2; 0 means both).
 */
esp_err_t nino_servo_recplay_record_start(uint8_t id_mask, bool torque_off);

/** Leave record mode; restore torque on selected IDs and face-track preference. */
esp_err_t nino_servo_recplay_record_stop(void);

/**
 * Play a joined frame list. Copies frames and runs a background task.
 * Returns ESP_ERR_INVALID_STATE if busy / bus not ready / spin active.
 */
esp_err_t nino_servo_recplay_play(const nino_servo_play_frame_t *frames, size_t count,
                                  int speed);

esp_err_t nino_servo_recplay_play_stop(void);

/** Fill status JSON fragment fields for embedding in /status. */
int nino_servo_recplay_status_json(char *buf, size_t buf_sz);

/** Full GET /servo/position payload. */
int nino_servo_recplay_position_json(char *buf, size_t buf_sz);

/** Register /servo/position|record|goal|play HTTP handlers (+ OPTIONS). */
esp_err_t nino_servo_recplay_register_http(httpd_handle_t server);

#ifdef __cplusplus
}
#endif

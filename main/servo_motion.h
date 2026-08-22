#pragma once

#include <stdbool.h>

#include "esp_err.h"

#define NINO_SERVO_AXIS_CENTER 512
/** Match servo_motion.c pose deltas — do not exceed these in scripted motion. */
#define NINO_SERVO_PAN_DELTA_LEFT 53
#define NINO_SERVO_PAN_DELTA_RIGHT 42
#define NINO_SERVO_TILT_DELTA 28
#define NINO_SERVO_PAN_LEFT (NINO_SERVO_AXIS_CENTER - NINO_SERVO_PAN_DELTA_LEFT)
#define NINO_SERVO_PAN_RIGHT (NINO_SERVO_AXIS_CENTER + NINO_SERVO_PAN_DELTA_RIGHT)
#define NINO_SERVO_TILT_UP (NINO_SERVO_AXIS_CENTER - NINO_SERVO_TILT_DELTA)
#define NINO_SERVO_TILT_DOWN (NINO_SERVO_AXIS_CENTER + NINO_SERVO_TILT_DELTA)

typedef enum {
  NINO_SERVO_MOTION_FULL = 0,
  NINO_SERVO_MOTION_NOD_LR = 1,
  /** Wide pan+tilt CW/CCW sweep for the GPIO48 hardware load test. */
  NINO_SERVO_MOTION_SWEEP = 2,
} nino_servo_motion_mode_t;

/** Start cyclic head motion (runs until nino_servo_motion_stop). */
void nino_servo_motion_start(nino_servo_motion_mode_t mode);

/** Stop motion and return servos to neutral 512. Blocks briefly until neutral is queued. */
void nino_servo_motion_stop(void);

bool nino_servo_motion_is_active(void);

/** Load calibrated pan extrema from NVS (defaults to NINO_SERVO_PAN_LEFT/RIGHT). */
void nino_servo_limits_init(void);

/** Last saved farthest-left / farthest-right pan (Dynamixel 0–1023). */
int nino_servo_pan_left(void);
int nino_servo_pan_right(void);

/** Sample a present pan position while capturing a left/right sweep. */
void nino_servo_limits_observe_pan(int present);

/** Begin recording extrema (resets the in-progress min/max). */
void nino_servo_limits_capture_begin(void);

/**
 * Commit extrema if the captured span is valid. Extends any previously saved
 * left/right stops; a narrower sweep does not shrink them.
 * Returns true when NVS was updated.
 */
bool nino_servo_limits_capture_end(void);

/**
 * Drive pan to the proven wide goals, sample present position at each end,
 * save only those two extrema, return to center.
 */
esp_err_t nino_servo_pan_calibrate(void);

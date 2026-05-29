#pragma once

#include <stdbool.h>

typedef enum {
  NINO_SERVO_MOTION_FULL = 0,
  NINO_SERVO_MOTION_NOD_LR = 1,
} nino_servo_motion_mode_t;

/** Start cyclic head motion (runs until nino_servo_motion_stop). */
void nino_servo_motion_start(nino_servo_motion_mode_t mode);

/** Stop motion and return servos to neutral 512. Blocks briefly until neutral is queued. */
void nino_servo_motion_stop(void);

bool nino_servo_motion_is_active(void);

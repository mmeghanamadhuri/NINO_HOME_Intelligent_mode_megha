#pragma once

#include <stdbool.h>

#include "esp_err.h"

/** Register USB host client for U2D2 (FTDI) and start the Dynamixel worker task. */
esp_err_t nino_servo_dxl_start(void);

/** True after U2D2 is open, joint mode enabled, and servos are usable. */
bool nino_servo_dxl_is_ready(void);

/** True when U2D2 USB serial is open (may still be enabling joint mode). */
bool nino_servo_dxl_bus_open(void);

/** Queue both servos to neutral (512). Safe if not ready (no-op). */
void nino_servo_dxl_go_neutral(void);

/** Queue pan (ID 1) and tilt (ID 2) goal positions (0–1023). */
void nino_servo_dxl_set_pan_tilt(int pan_goal, int tilt_goal);

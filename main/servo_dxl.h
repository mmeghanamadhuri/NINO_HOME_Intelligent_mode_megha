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

/** Queue pan (ID 2) and tilt (ID 1) goal positions (0–1023). */
void nino_servo_dxl_set_pan_tilt(int pan_goal, int tilt_goal);

/** Queue a single servo goal by Dynamixel ID (1 or 2). */
void nino_servo_dxl_set_servo_goal(uint8_t id, int goal);

/** Read present position (0–1023) for one servo. Requires bus ready. */
esp_err_t nino_servo_dxl_get_present_position(uint8_t id, int *position);

/**
 * ID2 full rotation: neutral (512) if needed, then 512→0→1023→512.
 * Runs in a background task; returns ESP_ERR_INVALID_STATE if already running or bus not ready.
 */
esp_err_t nino_servo_dxl_spin_360(void);

/** True while the 360 spin task is running (pose/neutral writes are ignored then). */
bool nino_servo_dxl_spin_is_active(void);

#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "esp_err.h"

/**
 * Hardware load test (GPIO48 single press, or CLI `hwtest`).
 *
 * Starts / stops together:
 *   - both Dynamixels sweeping clockwise / anti-clockwise continuously
 *   - USB camera streaming (voice-session gate forced on for the test)
 *   - RGB LED cycling colours
 *   - TFT/OLED expressions changing every 5 s
 *
 * Same button press again stops motors, LED, and eyes.
 */
esp_err_t nino_battery_endurance_init(void);

void nino_battery_endurance_toggle(void);
esp_err_t nino_battery_endurance_start(void);
void nino_battery_endurance_stop(void);
bool nino_battery_endurance_is_active(void);

/** True while the test loop is running (before stop). Other tasks must not
 *  park motors, steal the RGB LED, or change eye expressions. */
bool nino_battery_endurance_owns_actuators(void);

/** True when the hardware-test task itself is calling into LED/eyes. */
bool nino_battery_endurance_is_self(void);

void nino_battery_endurance_cli_register(void);

#pragma once

#include "esp_err.h"

/**
 * GPIO48 push button (active-low, internal pull-up, press to GND):
 *  - short press: play DEMO_main.wav
 *  - hold ~3s: erase Wi-Fi credentials, enable BLE provisioning,
 *    play NiNO-Home_Wifi.wav
 *
 * Do not wire buttons to GPIO7 (I2C SDA) or GPIO53 (speaker PA).
 * Call once from app_main after audio queue start.
 */
esp_err_t nino_push_buttons_start(void);

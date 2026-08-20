#pragma once

#include <stdbool.h>

#include "esp_err.h"

/**
 * GPIO48 push button (active-low, internal pull-up, press to GND):
 *  - double press: play DEMO_main.wav
 *  - triple press: erase Wi-Fi credentials, enable BLE provisioning,
 *    play NiNO-Home_Wifi.wav
 *
 * GPIO47 mute button (same wiring): single press toggles speaker mute.
 * While muted the RGB LED is solid red (not the low-battery blink).
 *
 * Do not wire buttons to GPIO7 (I2C SDA) or GPIO53 (speaker PA).
 * Call once from app_main after audio queue start.
 */
esp_err_t nino_push_buttons_start(void);

/**
 * Queue the embedded DEMO_main.wav clip, exactly as a double button press does.
 * Lets the phone app trigger the on-device demo via POST /demo {"play":true}.
 * Returns ESP_ERR_INVALID_STATE if the button subsystem has not started yet.
 * Ignored (still ESP_OK) if the demo is already playing.
 */
esp_err_t nino_push_buttons_trigger_demo(void);

/** Mute/unmute the speaker and set the solid-red mute LED. */
void nino_mute_set(bool muted);

#pragma once

#include <stdbool.h>

#include "esp_err.h"

/**
 * GPIO48 (J1 pin 33, active-low to GND):
 *  - short press: toggle Wi-Fi setup (AP + BLE) and play the in-app setup
 *    guide. A second press leaves setup and restores the previous network.
 *    If the app never continues, setup still times out after two minutes.
 *  - hold 5 seconds: Demo mode — play DEMO_main.wav.
 *
 * GPIO47 (J1 pin 37): single press toggles Aux-in / Sirena-mic mute.
 * Muted: solid red LED, Aux-in from Sirena is ignored.
 * Unmuted: LED off, Aux-in listen resumes immediately.
 *
 * Do not wire buttons to GPIO7 (I2C SDA) or GPIO53 (speaker PA).
 * Call once from app_main after audio queue start.
 */
esp_err_t nino_push_buttons_start(void);

/**
 * Queue the embedded DEMO_main.wav clip, exactly as a 5 s GPIO48 hold does.
 * Lets the phone app trigger the on-device demo via POST /demo {"play":true}.
 * Returns ESP_ERR_INVALID_STATE if the button subsystem has not started yet.
 * Ignored (still ESP_OK) if the demo is already playing.
 */
esp_err_t nino_push_buttons_trigger_demo(void);

/** Toggle Aux-in / mic mute (same as GPIO47). Safe from console or HTTP. */
void nino_push_buttons_trigger_mute(void);

/** Speaker mute/unmute (console). GPIO47 no longer uses this. */
void nino_mute_set(bool muted);

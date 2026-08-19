#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "esp_err.h"

#define RGB_LED_LEVEL_MAX 255

/** Runtime LED scenes. Idle is always off. */
typedef enum {
  NINO_RGB_SHOW_IDLE = 0,       /* no light */
  NINO_RGB_SHOW_LISTEN,         /* solid blue — STREAM / listen until TTS */
  NINO_RGB_SHOW_TTS,            /* solid green — voice reply playing */
  NINO_RGB_SHOW_ERROR,          /* solid red — WS/capture/query fail */
  NINO_RGB_SHOW_WIFI_WAIT,      /* blink white — boot / no Wi-Fi */
  NINO_RGB_SHOW_SERVER_WAIT,    /* blink green — Wi-Fi up, voice server not OK */
} nino_rgb_show_t;

/** Common-anode RGB on GPIO 2 (red), 3 (green), 4 (blue). Black -> 3.3 V. */
esp_err_t nino_rgb_led_init(void);

/** Start a named scene (stops any previous blink). Safe from console or tasks. */
esp_err_t nino_rgb_led_show(nino_rgb_show_t show);

nino_rgb_show_t nino_rgb_led_current(void);

const char *nino_rgb_led_show_name(nino_rgb_show_t show);

/** Set one primary channel 0-255. Other channels unchanged. */
esp_err_t nino_rgb_led_set_channel_level(const char *color, uint8_t level);

/** Set red/green/blue mix, each 0-255. */
esp_err_t nino_rgb_led_set_rgb(uint8_t red, uint8_t green, uint8_t blue);

/** Global brightness scale 0-255 applied to all channels. */
esp_err_t nino_rgb_led_set_brightness(uint8_t level);

uint8_t nino_rgb_led_get_brightness(void);

/** Apply a named color at optional intensity 0-255 (default 255). */
esp_err_t nino_rgb_led_set_named(const char *name, uint8_t intensity);

void nino_rgb_led_all_off(void);

void nino_rgb_led_cli_register(void);

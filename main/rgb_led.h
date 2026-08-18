#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "esp_err.h"

#define RGB_LED_LEVEL_MAX 255

/** Runtime LED scenes: wake/listen, TTS done, Wi-Fi, battery, error. */
typedef enum {
  NINO_RGB_SHOW_IDLE = 0,   /* no light */
  NINO_RGB_SHOW_LISTEN,     /* solid green — user may speak */
  NINO_RGB_SHOW_DONE,       /* blink green a few times, then off */
  NINO_RGB_SHOW_BATTERY,    /* slow orange blink — low battery */
  NINO_RGB_SHOW_OTA,        /* solid purple — firmware update */
  NINO_RGB_SHOW_ERROR,      /* fast red blink — capture/WS/error */
  NINO_RGB_SHOW_WIFI_WAIT,  /* white blink — connecting */
  NINO_RGB_SHOW_WIFI_OK,    /* solid cyan — connected */
  NINO_RGB_SHOW_WIFI_FAIL,  /* solid orange — not connected */
} nino_rgb_show_t;

/** Common-anode RGB on GPIO 2 (red), 3 (green), 4 (blue). Black -> 3.3 V. */
esp_err_t nino_rgb_led_init(void);

/** Start a named scene (stops any previous blink). Safe from console or tasks. */
esp_err_t nino_rgb_led_show(nino_rgb_show_t show);

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

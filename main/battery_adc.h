#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "esp_err.h"

/**
 * On-chip ESP32-P4 ADC battery monitor (GPIO20 = ADC1_CHANNEL_4).
 * Display RST stays on GPIO6. Do not use GPIO54 — that pad is ESP32-C6
 * slave reset and its pull-up lifts the divider (~9.8 V pack reads ~11.5 V).
 *
 * Hardware:
 *   Battery+ -- 22k --+-- GPIO20
 *                     |
 *                  3.3k
 *                     |
 *                  3.3k
 *                     |
 *   Battery- ---------+-- ESP GND
 *
 *   battery_mv = adc_mv * (22000 + 6600) / 6600
 *
 * A 12 V pack should measure ~2.77 V from GPIO20 to GND. Never put pack
 * voltage straight on GPIO20.
 *
 * Low-battery protection only runs when a real pack is on the divider.
 * 5V USB / direct supply, an open divider (~0 V), ADC rail, or a floating
 * GPIO20 (implausible / unstable) skip the WAV, RGB blink, and unmute.
 */
typedef struct {
  int16_t raw;
  int16_t raw_span;
  int32_t adc_mv;
  int32_t battery_mv;
  uint8_t percent;
} nino_battery_sample_t;

esp_err_t nino_battery_adc_init(void);
bool nino_battery_adc_ready(void);
esp_err_t nino_battery_adc_read(nino_battery_sample_t *out);
bool nino_battery_low_alert_active(void);
void nino_battery_adc_cli_register(void);

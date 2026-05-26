#pragma once

#include "esp_err.h"

/**
 * Start QT2120 touch polling (12 keys on shared BSP I2C).
 * On stable touch, plays embedded "please don't touch me" WAV on the ES8311 speaker.
 * Safe to call once from app_main after nino_audio_init().
 */
esp_err_t nino_touch_sensor_start(void);

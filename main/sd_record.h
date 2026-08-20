#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

/** Mount the on-board microSD (4-bit SDMMC via BSP). Idempotent. */
esp_err_t nino_sd_record_init(void);

bool nino_sd_record_ready(void);

/**
 * Append Aux-in PCM (16 kHz mono 16-bit) while the mic is reading.
 * Every 15 s a WAV is written under the SD mount point. Safe from the mic task.
 */
void nino_sd_record_feed(const int16_t *samples, int sample_count);

#ifdef __cplusplus
}
#endif

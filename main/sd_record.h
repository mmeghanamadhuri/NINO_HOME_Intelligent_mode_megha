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

/** Write a WAV blob to /sdcard/rec_NNNN.wav. @p out_path receives the path used. */
esp_err_t nino_sd_record_save_wav(const uint8_t *wav, size_t len, char *out_path,
                                  size_t out_path_len);

#ifdef __cplusplus
}
#endif

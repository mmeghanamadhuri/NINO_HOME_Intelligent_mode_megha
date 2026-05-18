#pragma once

#include <stddef.h>
#include <stdint.h>

#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

/** Record mono 16-bit PCM as WAV (16 kHz) for `duration_ms` (max ~10 s). */
esp_err_t nino_audio_capture_wav(uint8_t **out_wav, size_t *out_len,
                                 uint32_t duration_ms);

void nino_audio_capture_free(uint8_t *wav);

#ifdef __cplusplus
}
#endif

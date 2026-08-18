#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

/** Record mono 16-bit PCM from Aux-in as WAV (16 kHz) for `duration_ms` (max ~10 s). */
esp_err_t nino_audio_capture_wav(uint8_t **out_wav, size_t *out_len,
                                 uint32_t duration_ms);

/**
 * Record until the Aux-in goes quiet (end of sentence), with an optional
 * pre-roll prepended. Stops after `quiet_end_ms` below `quiet_energy`, but
 * not before `min_ms`, and never after `max_ms` (max 10 s).
 * If `flush_first` is false the current I2S stream is kept (wake tail).
 */
esp_err_t nino_audio_capture_wav_until_quiet(
    uint8_t **out_wav, size_t *out_len, const int16_t *preroll,
    size_t preroll_samples, uint32_t min_ms, uint32_t max_ms,
    uint32_t quiet_end_ms, uint32_t quiet_energy, bool flush_first);

/** Mount the BSP microSD card if needed and save a WAV under /sdcard/recordings. */
esp_err_t nino_audio_capture_save_to_sd(const uint8_t *wav, size_t wav_len,
                                        char *path, size_t path_size);

void nino_audio_capture_free(uint8_t *wav);

#ifdef __cplusplus
}
#endif

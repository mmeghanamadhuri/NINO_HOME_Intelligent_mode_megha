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
 * pre-roll prepended. After `min_ms` (wake gap), wait up to `wait_speech_ms`
 * for the question to start before allowing a quiet stop. Never exceeds
 * `max_ms` (max 10 s). If `flush_first` is false the I2S stream is kept.
 */
esp_err_t nino_audio_capture_wav_until_quiet(
    uint8_t **out_wav, size_t *out_len, const int16_t *preroll,
    size_t preroll_samples, uint32_t min_ms, uint32_t max_ms,
    uint32_t quiet_end_ms, uint32_t quiet_energy, uint32_t speech_energy,
    uint32_t wait_speech_ms, bool flush_first);

/** Mount the BSP microSD card if needed and save a WAV under /sdcard. */
esp_err_t nino_audio_capture_save_to_sd(const uint8_t *wav, size_t wav_len,
                                        char *path, size_t path_size);

/** Keep a copy of the latest Aux-in WAV for HTTP GET /aux_in.wav. */
esp_err_t nino_audio_capture_keep_last(const uint8_t *wav, size_t wav_len);

/** Malloc a copy of the last Aux-in WAV. Caller frees. */
esp_err_t nino_audio_capture_copy_last(uint8_t **out_wav, size_t *out_len);

void nino_audio_capture_free(uint8_t *wav);

#ifdef __cplusplus
}
#endif

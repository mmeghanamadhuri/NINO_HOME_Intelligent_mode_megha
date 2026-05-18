#pragma once

#include <stddef.h>
#include "esp_err.h"

esp_err_t nino_audio_init(void);

esp_err_t nino_audio_play_wav(const uint8_t *wav_bytes, size_t wav_len);

/** Play 16-bit mono PCM; waits for the DAC pipeline to finish before closing the codec. */
esp_err_t nino_audio_play_pcm16_mono(const int16_t *samples, size_t sample_count,
                                     uint32_t sample_rate_hz);

/** Serialize access to the shared ES8311 / I2S path (playback vs microphone). */
void nino_audio_bus_lock(void);
void nino_audio_bus_unlock(void);

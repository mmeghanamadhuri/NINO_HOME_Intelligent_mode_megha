#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include "esp_err.h"

esp_err_t nino_audio_init(void);

esp_err_t nino_audio_play_wav(const uint8_t *wav_bytes, size_t wav_len);

/** Decoded 16-bit mono PCM ready for speaker output. */
typedef struct {
  const int16_t *samples;
  size_t num_bytes;
  uint32_t sample_rate_hz;
  int16_t *mono_heap;
} nino_decoded_wav_t;

/** Parse WAV into mono PCM. Caller frees with nino_decoded_wav_free(). */
esp_err_t nino_audio_decode_wav(const uint8_t *wav_bytes, size_t wav_len,
                                nino_decoded_wav_t *out);

/** True when @p wav_bytes is a complete PCM WAV the ESP speaker path can play. */
bool nino_audio_wav_bytes_valid(const uint8_t *wav_bytes, size_t wav_len);

void nino_decoded_wav_free(nino_decoded_wav_t *decoded);

/**
 * Play decoded PCM from @p pcm_byte_offset. Updates @p pcm_byte_offset on exit.
 * @p completed is set true when the entire clip finishes; false if @p stop_requested
 * interrupted playback mid-clip.
 */
esp_err_t nino_audio_play_decoded(const nino_decoded_wav_t *decoded, size_t *pcm_byte_offset,
                                  volatile bool *stop_requested, bool *completed);

/** Play 16-bit mono PCM; waits for the DAC pipeline to finish before closing the codec. */
esp_err_t nino_audio_play_pcm16_mono(const int16_t *samples, size_t sample_count,
                                     uint32_t sample_rate_hz);

/** Fast wake/done chime: reuse open 16 kHz codec when possible; leaves path warm. */
esp_err_t nino_audio_play_chime_pcm16_mono(const int16_t *samples, size_t sample_count,
                                           uint32_t sample_rate_hz);

/** Open speaker at 16 kHz once at boot so the first wake beep has no codec setup delay. */
esp_err_t nino_audio_warm_chime_path(uint32_t sample_rate_hz);

/**
 * Open the speaker (sample_count == 0) or write 16-bit mono PCM without closing
 * the codec. Caller must hold nino_audio_bus_lock().
 */
esp_err_t nino_audio_write_pcm16_mono_locked(const int16_t *samples, size_t sample_count,
                                             uint32_t sample_rate_hz);

/** Serialize access to the ES8311 speaker I2S path (playback). */
void nino_audio_bus_lock(void);
void nino_audio_bus_unlock(void);

/**
 * Close the speaker I2S stream. Caller must hold nino_audio_bus_lock().
 * Required before opening the ES8311 ADC — mic and speaker share one duplex.
 */
void nino_audio_drop_speaker_stream_locked(void);

/** Set speaker output volume percent (0-100). Persisted to NVS. */
esp_err_t nino_audio_set_volume_percent(int volume_percent);

/** Current speaker output volume percent (0-100). */
int nino_audio_get_volume_percent(void);

/**
 * Load the speaker volume saved in NVS (set by the app/console) and apply it.
 * Falls back to the 80% default if nothing has been saved yet. Call once at
 * boot after nino_audio_init().
 */
esp_err_t nino_audio_load_saved_volume(void);

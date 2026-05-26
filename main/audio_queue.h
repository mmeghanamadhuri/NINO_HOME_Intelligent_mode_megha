#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "esp_err.h"

/** Start the shared FIFO playback worker (server, touch, voice replies). */
esp_err_t nino_audio_queue_start(void);

/**
 * Enqueue WAV for speaker playback. Takes ownership of @p wav (freed after play).
 * Blocks until the job is queued — no drop when the speaker is busy.
 */
esp_err_t nino_audio_queue_wav(uint8_t *wav, size_t len, bool play_done_chime);

/**
 * Copy @p wav into heap, then enqueue (for embedded flash clips e.g. touch warning).
 * Blocks until queued.
 */
esp_err_t nino_audio_queue_wav_copy(const uint8_t *wav, size_t len, bool play_done_chime);

/** Voice assistant: queue server reply WAV; optional done chime after. */
void nino_main_queue_audio_wav(uint8_t *pcm_wav, size_t len, bool play_done_chime);

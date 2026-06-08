#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "esp_err.h"

/** How servos move while a queued WAV plays on the speaker. */
typedef enum {
  NINO_AUDIO_SERVO_FULL = 0,
  NINO_AUDIO_SERVO_NOD_LR = 1,
  NINO_AUDIO_SERVO_NONE = 2,
} nino_audio_servo_mode_t;

/** Start the shared playback worker (touch priority; server/voice pause/resume). */
esp_err_t nino_audio_queue_start(void);

/**
 * Enqueue WAV for speaker playback. Takes ownership of @p wav (freed after play).
 * Blocks until the job is queued — no drop when the speaker is busy.
 */
esp_err_t nino_audio_queue_wav(uint8_t *wav, size_t len, bool play_done_chime,
                               nino_audio_servo_mode_t servo_mode,
                               bool prompt_ack_after);

/**
 * Copy @p wav into heap, then enqueue (for embedded flash clips e.g. touch warning).
 * Blocks until queued.
 */
esp_err_t nino_audio_queue_wav_copy(const uint8_t *wav, size_t len, bool play_done_chime,
                                    nino_audio_servo_mode_t servo_mode,
                                    bool prompt_ack_after);

/** Voice assistant: queue server reply WAV; optional done chime after. */
void nino_main_queue_audio_wav(uint8_t *pcm_wav, size_t len, bool play_done_chime,
                               bool prompt_ack_after);

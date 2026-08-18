#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "esp_err.h"
#include "nino_eye.h"

/** How servos move while a queued WAV plays on the speaker. */
typedef enum {
  NINO_AUDIO_SERVO_FULL = 0,
  NINO_AUDIO_SERVO_NOD_LR = 1,
  NINO_AUDIO_SERVO_NONE = 2,
  /* High-priority queue with no servo motion; preempts normal clips. */
  NINO_AUDIO_SERVO_PRIORITY_NONE = 3,
} nino_audio_servo_mode_t;

/** Start the shared playback worker (touch priority; server/voice pause/resume). */
esp_err_t nino_audio_queue_start(void);

/**
 * Enqueue WAV for speaker playback. Takes ownership of @p wav (freed after play).
 * Blocks until the job is queued — no drop when the speaker is busy.
 * @p eye_state is shown while this clip plays and reverts to idle when it ends;
 * pass NINO_EYE_STATE_COUNT to leave the eyes untouched.
 */
esp_err_t nino_audio_queue_wav(uint8_t *wav, size_t len, bool play_done_chime,
                               nino_audio_servo_mode_t servo_mode,
                               bool prompt_ack_after, nino_eye_state_t eye_state);

/**
 * Copy @p wav into heap, then enqueue (for embedded flash clips e.g. touch warning).
 * Blocks until queued.
 */
esp_err_t nino_audio_queue_wav_copy(const uint8_t *wav, size_t len, bool play_done_chime,
                                    nino_audio_servo_mode_t servo_mode,
                                    bool prompt_ack_after);

/** Voice assistant: queue server reply WAV; optional done chime after.
 *  @p eye_state is the expression to show during playback (idle afterwards);
 *  pass NINO_EYE_STATE_COUNT for no expression. */
void nino_main_queue_audio_wav(uint8_t *pcm_wav, size_t len, bool play_done_chime,
                               bool prompt_ack_after, nino_eye_state_t eye_state);

/** Block until the normal playback queue is idle (boot clips finished). */
void nino_audio_queue_wait_idle(uint32_t timeout_ms);

/** Stop any interruptible normal clip so the wake beep can take the speaker. */
void nino_audio_queue_preempt_for_wake(void);

/** True while a queued clip is pending, playing, or suspended. */
bool nino_audio_queue_busy(void);

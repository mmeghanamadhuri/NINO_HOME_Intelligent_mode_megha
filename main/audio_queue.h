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

/**
 * Enqueue an app-streamed music clip. Same ownership as nino_audio_queue_wav().
 * Keeps the speaker open between clips so playback is gapless when the next
 * job is already queued (or arrives within a short hand-off window).
 */
esp_err_t nino_audio_queue_stream_wav(uint8_t *wav, size_t len,
                                      nino_eye_state_t eye_state);

/** Snapshot for GET /play_wav/status. */
typedef struct {
  bool playing;
  bool paused;
  bool suspended;
  int queued;
} nino_audio_queue_status_t;

/** Cut speaker now; keep remaining PCM so resume continues from this point. */
void nino_audio_queue_pause(void);

/** Continue from the pause point, then any clips still in the queue. */
void nino_audio_queue_resume(void);

/** Drop queued/suspended clips and silence the speaker. */
void nino_audio_queue_stop(void);

void nino_audio_queue_get_status(nino_audio_queue_status_t *out);

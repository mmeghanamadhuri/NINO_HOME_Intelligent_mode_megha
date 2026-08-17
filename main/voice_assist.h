#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "esp_err.h"
#include "nino_eye.h"

#ifdef __cplusplus
extern "C" {
#endif

/** Queues WAV on the shared speaker FIFO (see audio_queue.c). */
void nino_main_queue_audio_wav(uint8_t *pcm_wav, size_t len, bool play_done_chime,
                               bool prompt_ack_after, nino_eye_state_t eye_state);

/** Copy current WebSocket URL (e.g. after `voice connect`). */
void nino_voice_assist_set_ws_uri(const char *uri);

/** Create WS-URI mutex (call once from app_main before console). */
esp_err_t nino_voice_assist_init_mutex(void);

/** Embedded main/beep.wav — played on wake ("Hi ESP" or "Jarvis"). */
esp_err_t nino_voice_play_wake_chime(void);

/** Decode beep + warm ES8311 at 16 kHz — call once after nino_audio_init(). */
esp_err_t nino_voice_preload_wake_chime(void);

/** Same embedded beep.wav — played after voice reply playback. */
esp_err_t nino_voice_play_done_chime(void);

<<<<<<< HEAD
/**
 * Energy VAD + adaptive end-of-utterance: wait for speech, record until
 * adaptive trailing silence (or max_seconds). Mid-utterance pauses raise the
 * stop timeout within clamped bounds (see docs/ADVA_PLAN.md).
 * Output is 16-bit mono WAV at 16 kHz. Caller frees with nino_audio_capture_free().
 */
esp_err_t nino_voice_capture_vad_wav(int max_seconds, uint8_t **out_wav, size_t *out_len);

bool nino_voice_assist_has_ws_uri(void);

/**
 * Console `start`: 2 s delay, record 5 s WAV, save to SD, send to server,
 * play reply on speaker. Requires `voice connect`.
 */
esp_err_t nino_voice_assist_console_start(void);

/** After wake word: VAD + WebSocket + queue TTS reply (no chime here). */
=======
bool nino_voice_assist_has_ws_uri(void);

/** Capture exactly five seconds, save it to microSD, send it to the voice WS, and queue the reply. */
>>>>>>> b63091e (Fixed the reply path from Ptron Mic source to P4 speaker out)
esp_err_t nino_voice_assist_run_query_only(void);

/**
 * Optional SD record loop (not started by default). Boot uses mic→speaker loopback
 * via nino_audio_loopback_start() in main.c.
 */
void nino_voice_assist_start_listen_loop(void);

/** After a medical alarm WAV from the PC: chime + listen for yes/no (needs voice connect). */
void nino_voice_assist_prompt_medical_ack(void);

/**
 * Next prompt_ack listen: play wake chime before VAD (default true).
 * /play_wav uses X-Nino-Prompt-Ack-Chime. WS continue-listen after a done
 * chime sets this false so only one beep plays before the mic opens.
 */
void nino_voice_assist_set_next_prompt_ack_chime(bool play_chime);

#ifdef __cplusplus
}
#endif

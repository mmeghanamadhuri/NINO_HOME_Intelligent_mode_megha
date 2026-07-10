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

/** Embedded main/beep.wav — played on "Hi ESP" wake. */
esp_err_t nino_voice_play_wake_chime(void);

/** Decode beep + warm ES8311 at 16 kHz — call once after nino_audio_init(). */
esp_err_t nino_voice_preload_wake_chime(void);

/** Same embedded beep.wav — played after voice reply playback. */
esp_err_t nino_voice_play_done_chime(void);

/**
 * Energy VAD: wait for speech, record until trailing silence or max_seconds.
 * Output is 16-bit mono WAV at 16 kHz. Caller frees with nino_audio_capture_free().
 */
esp_err_t nino_voice_capture_vad_wav(int max_seconds, uint8_t **out_wav, size_t *out_len);

bool nino_voice_assist_has_ws_uri(void);

/** After wake word: VAD + WebSocket + queue TTS reply (no chime here). */
esp_err_t nino_voice_assist_run_query_only(void);

/** After a medical alarm WAV from the PC: chime + listen for yes/no (needs voice connect). */
void nino_voice_assist_prompt_medical_ack(void);

/** Next POST /play_wav prompt_ack listen: play wake chime before VAD (default true). */
void nino_voice_assist_set_next_prompt_ack_chime(bool play_chime);

#ifdef __cplusplus
}
#endif

#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

/** Queues WAV on the shared speaker FIFO (see audio_queue.c). */
void nino_main_queue_audio_wav(uint8_t *pcm_wav, size_t len, bool play_done_chime);

/** Copy current WebSocket URL (e.g. after `voice connect`). */
void nino_voice_assist_set_ws_uri(const char *uri);

/** Create WS-URI mutex (call once from app_main before console). */
esp_err_t nino_voice_assist_init_mutex(void);

/** Short wake beep (16 kHz mono) — ascending two-tone on "Hi ESP". */
esp_err_t nino_voice_play_wake_chime(void);

/** Descending two-tone after voice-assistant reply playback. */
esp_err_t nino_voice_play_done_chime(void);

/**
 * Energy VAD: wait for speech, record until trailing silence or max_seconds.
 * Output is 16-bit mono WAV at 16 kHz. Caller frees with nino_audio_capture_free().
 */
esp_err_t nino_voice_capture_vad_wav(int max_seconds, uint8_t **out_wav, size_t *out_len);

bool nino_voice_assist_has_ws_uri(void);

/** After wake word: VAD + WebSocket + queue TTS reply (no chime here). */
esp_err_t nino_voice_assist_run_query_only(void);

#ifdef __cplusplus
}
#endif

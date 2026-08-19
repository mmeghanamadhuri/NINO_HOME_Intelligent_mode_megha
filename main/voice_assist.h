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

/** Embedded main/beep.wav — optional cue before a prompt-ack listen. */
esp_err_t nino_voice_play_wake_chime(void);

/** Decode beep + warm ES8311 at 16 kHz — call once after nino_audio_init(). */
esp_err_t nino_voice_preload_wake_chime(void);

/** Same embedded beep.wav — played after voice reply playback. */
esp_err_t nino_voice_play_done_chime(void);

bool nino_voice_assist_has_ws_uri(void);

/** Record AUX IN for @p duration_ms (CLI debug) or start a stream session. */
esp_err_t nino_voice_assist_run_query(uint32_t duration_ms);

/** CLI: start a streamed conversation session (no fixed 5 s clip). */
esp_err_t nino_voice_assist_run_query_only(void);

/**
 * Watch ES8311 Aux-in for Sirena wake energy, then stream PCM to the PC
 * until ASR end-of-speech. The session stays open through LLM/TTS turns
 * until the user says goodbye or stop, or 30s of no speech. After that TTS,
 * GPIO 5 goes high so Sirena can close its mics. Stream or WS failures leave
 * GPIO 5 low.
 */
void nino_voice_assist_start_listen_loop(void);

/** True while the Aux-in energy listener is running and not in a query. */
bool nino_voice_assist_aux_listen_is_running(void);

/** After a medical alarm WAV from the PC: chime + fixed-length listen. */
void nino_voice_assist_prompt_medical_ack(void);

/**
 * Next prompt_ack listen: play chime before capture (default true).
 * /play_wav uses X-Nino-Prompt-Ack-Chime. WS continue-listen after a done
 * chime sets this false so only one beep plays before the mic opens.
 */
void nino_voice_assist_set_next_prompt_ack_chime(bool play_chime);

/** True while a voice query (capture or WS job) is in flight. */
bool nino_voice_assist_query_is_busy(void);

#ifdef __cplusplus
}
#endif

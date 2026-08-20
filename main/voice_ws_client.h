#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

/** Start WS connect in background (call before beep/VAD to hide connect latency). */
esp_err_t nino_voice_ws_preconnect(const char *ws_uri);

/** Cancel any in-flight preconnect. */
void nino_voice_ws_preconnect_cancel(void);

/**
 * Send one WAV (binary) to PC NiNO server `ws://.../voice-query` or `.../ws/voice`,
 * wait for one JSON metadata text frame (optional) then one WAV reply.
 * Caller must free *wav_out with free().
 * If @p prompt_medical_ack_out is non-NULL, set from server metadata when present.
 * If @p eye_expr_out is non-NULL (with @p eye_expr_cap > 0), it receives the
 * server's `eye_expression` value (e.g. "sad"); empty string when the key is
 * absent (reply should stay on idle).
 */
esp_err_t nino_voice_ws_exchange(const char *ws_uri, const uint8_t *wav_in,
                                 size_t wav_in_len, uint8_t **wav_out,
                                 size_t *wav_out_len, int timeout_ms,
                                 bool *prompt_medical_ack_out,
                                 char *eye_expr_out, size_t eye_expr_cap);

/** Long-lived Aux-in stream: send PCM until the server signals end-of-speech. */
typedef struct nino_voice_ws_session nino_voice_ws_session_t;

esp_err_t nino_voice_ws_session_open(const char *ws_uri,
                                     nino_voice_ws_session_t **out);
bool nino_voice_ws_session_is_open(nino_voice_ws_session_t *session);
/** True while the ESP-IDF websocket transport is still connected. */
bool nino_voice_ws_session_socket_connected(nino_voice_ws_session_t *session);
esp_err_t nino_voice_ws_session_send_pcm(nino_voice_ws_session_t *session,
                                         const void *pcm, size_t len);
bool nino_voice_ws_session_should_pause(nino_voice_ws_session_t *session);
void nino_voice_ws_session_begin_turn(nino_voice_ws_session_t *session);
/** Clear EOS/WAV flags without ending the session-open (GREET) window. */
void nino_voice_ws_session_clear_reply(nino_voice_ws_session_t *session);
esp_err_t nino_voice_ws_session_send_text(nino_voice_ws_session_t *session,
                                          const char *text);
esp_err_t nino_voice_ws_session_wait_reply(nino_voice_ws_session_t *session,
                                           int timeout_ms, uint8_t **wav_out,
                                           size_t *wav_out_len, bool *skip,
                                           bool *end_session, char *eye_expr_out,
                                           size_t eye_expr_cap, char *motion_out,
                                           size_t motion_cap, bool *wake_ok);
void nino_voice_ws_session_close(nino_voice_ws_session_t *session);

#ifdef __cplusplus
}
#endif

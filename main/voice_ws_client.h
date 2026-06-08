#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

/**
 * Send one WAV (binary) to PC NiNO server `ws://.../voice-query` or `.../ws/voice`,
 * wait for one JSON metadata text frame (optional) then one WAV reply.
 * Caller must free *wav_out with free().
 * If @p prompt_medical_ack_out is non-NULL, set from server metadata when present.
 */
esp_err_t nino_voice_ws_exchange(const char *ws_uri, const uint8_t *wav_in,
                                 size_t wav_in_len, uint8_t **wav_out,
                                 size_t *wav_out_len, int timeout_ms,
                                 bool *prompt_medical_ack_out);

#ifdef __cplusplus
}
#endif

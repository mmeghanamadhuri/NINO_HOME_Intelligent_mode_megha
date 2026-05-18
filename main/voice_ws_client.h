#pragma once

#include <stddef.h>
#include <stdint.h>

#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

/**
 * Send one WAV (binary) to PC NiNO server `ws://.../voice-query` or `.../ws/voice`,
 * wait for one WAV reply. Caller must free *wav_out with free().
 */
esp_err_t nino_voice_ws_exchange(const char *ws_uri, const uint8_t *wav_in,
                                 size_t wav_in_len, uint8_t **wav_out,
                                 size_t *wav_out_len, int timeout_ms);

#ifdef __cplusplus
}
#endif

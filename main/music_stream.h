#pragma once

#include "esp_err.h"
#include <stdbool.h>

#ifdef __cplusplus
extern "C" {
#endif

esp_err_t nino_music_init(void);
esp_err_t nino_music_start(const char *url); /* copies url, starts puller */
void nino_music_stop(void);                  /* idempotent */
bool nino_music_is_playing(void);
void nino_music_pause_for_speech(bool paused); /* duck for wake/TTS */

/** True while the music feed currently owns the ES8311 speaker (not ducked). */
bool nino_music_blocks_mic(void);

#ifdef __cplusplus
}
#endif

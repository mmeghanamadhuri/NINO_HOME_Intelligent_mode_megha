#pragma once

#include <stdbool.h>

#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

/**
 * Start onboard ES8311 mic → speaker loopback (16 kHz mono).
 * Call after nino_audio_init(). WAV playback pauses loopback automatically.
 */
esp_err_t nino_audio_loopback_start(void);

/** Pause loopback so queued WAV / capture can own the ES8311 I2S path. */
void nino_audio_loopback_pause(void);

/** Resume loopback after exclusive speaker/mic use. */
void nino_audio_loopback_resume(void);

bool nino_audio_loopback_is_running(void);

#ifdef __cplusplus
}
#endif

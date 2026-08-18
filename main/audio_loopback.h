#pragma once

#include <stdbool.h>

#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

/**
 * Init the shared I2S hold used while the speaker owns the duplex codec.
 * Does not start onboard-mic loopback — Aux-in is capture-only.
 */
esp_err_t nino_audio_loopback_start(void);

/** Pause Aux-in reads so queued WAV / capture can own the ES8311 I2S path. */
void nino_audio_loopback_pause(void);

/** Resume Aux-in monitoring after exclusive speaker/capture use. */
void nino_audio_loopback_resume(void);

/** True while speaker/capture has paused Aux-in. */
bool nino_audio_loopback_is_paused(void);

/** True when Aux-in is allowed to read (hold is not asserted). */
bool nino_audio_loopback_is_running(void);

/** Listen task sets this around an Aux-in read so pause can wait it out. */
void nino_audio_input_mark_busy(bool busy);

#ifdef __cplusplus
}
#endif

#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

typedef enum {
  NINO_MIC_SOURCE_ES8311_AUX,
} nino_mic_source_t;

/**
 * Read 16 kHz mono PCM from the ES8311 analog AUX / line-in (LIN),
 * configured over I2C. Not the onboard MIC1 pad and not USB UAC.
 */
esp_err_t nino_mic_read(int16_t *samples, int sample_count);

/** Discard a short settle window after the ADC opens. */
void nino_mic_flush(void);

nino_mic_source_t nino_mic_preferred_source(void);
const char *nino_mic_source_name(nino_mic_source_t source);

/** True when the ES8311 ADC can be opened for AUX IN. */
bool nino_mic_available(void);

/**
 * Close the ES8311 ADC if open. Caller must hold nino_audio_bus_lock().
 * Required before speaker (re)open — mic and speaker share one I2S duplex.
 */
void nino_mic_drop_es8311_locked(void);

/** Close the ADC after a capture so speaker playback can reopen I2S. */
void nino_mic_close(void);

#ifdef __cplusplus
}
#endif

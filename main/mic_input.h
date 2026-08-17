#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

typedef enum {
  NINO_MIC_SOURCE_ES8311,
} nino_mic_source_t;

/** Read 16 kHz mono PCM from the onboard ES8311 ADC. */
esp_err_t nino_mic_read(int16_t *samples, int sample_count);

/**
 * Same as nino_mic_read(), but caller must hold nino_audio_bus_lock().
 * sample_count == 0 only opens the ADC (no read).
 */
esp_err_t nino_mic_read_locked(int16_t *samples, int sample_count);

/** ES8311 has no software queue; kept for call-site compatibility. */
void nino_mic_flush(void);

nino_mic_source_t nino_mic_preferred_source(void);

const char *nino_mic_source_name(nino_mic_source_t source);

bool nino_mic_available(void);

/**
 * Close the ES8311 ADC if open. Caller must hold nino_audio_bus_lock().
 * Required before speaker reopen when the I2S rate changes — mic and speaker
 * share one duplex I2S.
 */
void nino_mic_drop_es8311_locked(void);

/** Close the onboard ADC (takes the audio bus lock). */
void nino_mic_close(void);

#ifdef __cplusplus
}
#endif

#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

typedef enum {
  NINO_MIC_SOURCE_USB_4MIC,
  NINO_MIC_SOURCE_ES8311,
} nino_mic_source_t;

/**
 * Read 16 kHz mono PCM, preferring the streaming USB 4-mic. If it is not
 * available, the ES8311 onboard ADC is opened and used instead.
 */
esp_err_t nino_mic_read(int16_t *samples, int sample_count);

/** Discard queued USB PCM before a fresh capture; ES8311 has no software queue. */
void nino_mic_flush(void);

/** Source selected for the next read. */
nino_mic_source_t nino_mic_preferred_source(void);

/** Human-readable name for logs and console status. */
const char *nino_mic_source_name(nino_mic_source_t source);

/**
 * True if USB is streaming or the ES8311 ADC can be opened for fallback.
 */
bool nino_mic_available(void);

/**
 * Close the ES8311 ADC if open. Caller must hold nino_audio_bus_lock().
 * Required before speaker (re)open — mic and speaker share one I2S duplex.
 */
void nino_mic_drop_es8311_locked(void);

#ifdef __cplusplus
}
#endif

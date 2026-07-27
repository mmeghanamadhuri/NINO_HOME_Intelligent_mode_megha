#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

typedef enum {
  NINO_MIC_SOURCE_USB_4MIC,
  NINO_MIC_SOURCE_NONE,
} nino_mic_source_t;

/**
 * Read 16 kHz mono PCM from the streaming USB 4-mic.
 */
esp_err_t nino_mic_read(int16_t *samples, int sample_count);

/** Discard queued USB PCM before a fresh capture. */
void nino_mic_flush(void);

/** Source selected for the next read. */
nino_mic_source_t nino_mic_preferred_source(void);

/** Human-readable name for logs and console status. */
const char *nino_mic_source_name(nino_mic_source_t source);

/**
 * True only while the USB 4-mic is streaming.
 */
bool nino_mic_available(void);

#ifdef __cplusplus
}
#endif

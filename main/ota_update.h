#pragma once

#include "esp_err.h"
#include <stdbool.h>

#ifdef __cplusplus
extern "C" {
#endif

/** Start OTA from a firmware URL (http:// or https://). Runs in a FreeRTOS task. */
esp_err_t ota_update_start(const char *url);

/** True while download/flash is in progress. */
bool ota_update_in_progress(void);

/** True when running partition table includes OTA app slots. */
bool ota_update_capable(void);

#ifdef __cplusplus
}
#endif

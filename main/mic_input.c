#include "mic_input.h"

#include "usb_mic.h"

#include "esp_log.h"

static const char *TAG = "mic_input";
static bool s_usb_unavailable_logged;

nino_mic_source_t nino_mic_preferred_source(void) {
  return usb_mic_ready() ? NINO_MIC_SOURCE_USB_4MIC
                         : NINO_MIC_SOURCE_NONE;
}

const char *nino_mic_source_name(nino_mic_source_t source) {
  return source == NINO_MIC_SOURCE_USB_4MIC ? "USB 4-mic" : "unavailable";
}

bool nino_mic_available(void) { return usb_mic_ready(); }

esp_err_t nino_mic_read(int16_t *samples, int sample_count) {
  if (samples == NULL || sample_count <= 0) {
    return ESP_ERR_INVALID_ARG;
  }
  if (!usb_mic_ready()) {
    if (!s_usb_unavailable_logged) {
      ESP_LOGW(TAG, "USB 4-mic not ready — voice input unavailable");
      s_usb_unavailable_logged = true;
    }
    return ESP_ERR_INVALID_STATE;
  }
  s_usb_unavailable_logged = false;
  return usb_mic_read(samples, sample_count);
}

void nino_mic_flush(void) { usb_mic_flush(); }

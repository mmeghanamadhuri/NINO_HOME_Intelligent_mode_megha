#include "mic_input.h"

#include "audio_playback.h"

#include "bsp/esp32_p4_function_ev_board.h"
#include "esp_codec_dev.h"
#include "esp_codec_dev_types.h"
#include "esp_log.h"

static const char *TAG = "mic_input";

#define NINO_MIC_SAMPLE_RATE_HZ 16000
#define NINO_ES8311_MIC_GAIN_DB 24.0f

static esp_codec_dev_handle_t s_es8311_mic;

static void close_es8311_mic_locked(void) {
  if (s_es8311_mic != NULL) {
    esp_codec_dev_close(s_es8311_mic);
    s_es8311_mic = NULL;
    ESP_LOGI(TAG, "ES8311 microphone closed");
  }
}

static esp_err_t open_es8311_mic_locked(void) {
  if (s_es8311_mic != NULL) {
    return ESP_OK;
  }

  esp_err_t err = bsp_i2c_init();
  if (err != ESP_OK && err != ESP_ERR_INVALID_STATE) {
    ESP_LOGE(TAG, "bsp_i2c_init for ES8311 microphone failed: %s",
             esp_err_to_name(err));
    return err;
  }

  s_es8311_mic = bsp_audio_codec_microphone_init();
  if (s_es8311_mic == NULL) {
    ESP_LOGE(TAG, "bsp_audio_codec_microphone_init failed");
    return ESP_FAIL;
  }

  (void)esp_codec_dev_set_in_gain(s_es8311_mic, NINO_ES8311_MIC_GAIN_DB);
  esp_codec_dev_sample_info_t format = {
      .bits_per_sample = 16,
      .channel = 1,
      .channel_mask = 0,
      .sample_rate = NINO_MIC_SAMPLE_RATE_HZ,
      .mclk_multiple = 0,
  };
  const int result = esp_codec_dev_open(s_es8311_mic, &format);
  if (result != ESP_CODEC_DEV_OK) {
    ESP_LOGE(TAG, "esp_codec_dev_open(ES8311 microphone) failed: %d", result);
    close_es8311_mic_locked();
    return ESP_FAIL;
  }

  ESP_LOGI(TAG, "Using onboard ES8311 microphone at %d Hz",
           NINO_MIC_SAMPLE_RATE_HZ);
  return ESP_OK;
}

nino_mic_source_t nino_mic_preferred_source(void) {
  return NINO_MIC_SOURCE_ES8311;
}

const char *nino_mic_source_name(nino_mic_source_t source) {
  (void)source;
  return "ES8311 onboard";
}

bool nino_mic_available(void) {
  nino_audio_bus_lock();
  const esp_err_t err = open_es8311_mic_locked();
  nino_audio_bus_unlock();
  return err == ESP_OK;
}

esp_err_t nino_mic_read_locked(int16_t *samples, int sample_count) {
  if (sample_count < 0) {
    return ESP_ERR_INVALID_ARG;
  }
  if (sample_count > 0 && samples == NULL) {
    return ESP_ERR_INVALID_ARG;
  }

  esp_err_t err = open_es8311_mic_locked();
  if (err != ESP_OK) {
    return err;
  }
  if (sample_count == 0) {
    return ESP_OK;
  }

  const int result = esp_codec_dev_read(
      s_es8311_mic, samples, sample_count * (int)sizeof(*samples));
  if (result != ESP_CODEC_DEV_OK) {
    ESP_LOGW(TAG, "ES8311 microphone read failed: %d", result);
    close_es8311_mic_locked();
    return ESP_FAIL;
  }
  return ESP_OK;
}

esp_err_t nino_mic_read(int16_t *samples, int sample_count) {
  nino_audio_bus_lock();
  const esp_err_t err = nino_mic_read_locked(samples, sample_count);
  nino_audio_bus_unlock();
  return err;
}

void nino_mic_flush(void) {}

void nino_mic_drop_es8311_locked(void) {
  close_es8311_mic_locked();
}

void nino_mic_close(void) {
  nino_audio_bus_lock();
  close_es8311_mic_locked();
  nino_audio_bus_unlock();
}

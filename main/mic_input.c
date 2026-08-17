#include "mic_input.h"

#include "audio_playback.h"

#include "bsp/esp32_p4_function_ev_board.h"
#include "driver/i2c_master.h"
#include "esp_codec_dev.h"
#include "esp_codec_dev_types.h"
#include "esp_log.h"

static const char *TAG = "mic_input";

#define NINO_MIC_SAMPLE_RATE_HZ 16000
#define NINO_ES8311_AUX_GAIN_DB 12.0f
#define ES8311_I2C_ADDR 0x18
#define ES8311_REG_ADC_ANALOG 0x14
#define ES8311_LINSEL_MASK 0x0C
#define ES8311_LINSEL_LIN 0x0C

static esp_codec_dev_handle_t s_es8311_mic;
static i2c_master_dev_handle_t s_es8311_i2c;
static bool s_aux_selected;

static void close_es8311_mic_locked(void) {
  if (s_es8311_mic != NULL) {
    esp_codec_dev_close(s_es8311_mic);
    s_es8311_mic = NULL;
    s_aux_selected = false;
    ESP_LOGI(TAG, "ES8311 AUX ADC closed");
  }
}

static esp_err_t es8311_ensure_i2c(void) {
  if (s_es8311_i2c != NULL) {
    return ESP_OK;
  }

  i2c_master_bus_handle_t bus = bsp_i2c_get_handle();
  if (bus == NULL) {
    ESP_LOGE(TAG, "BSP I2C bus not ready for ES8311 AUX select");
    return ESP_ERR_INVALID_STATE;
  }

  i2c_device_config_t dev_cfg = {
      .dev_addr_length = I2C_ADDR_BIT_LEN_7,
      .device_address = ES8311_I2C_ADDR,
      .scl_speed_hz = 100000,
  };
  esp_err_t err = i2c_master_bus_add_device(bus, &dev_cfg, &s_es8311_i2c);
  if (err != ESP_OK) {
    ESP_LOGW(TAG, "ES8311 I2C add_device: %s (AUX select may still work via codec)",
             esp_err_to_name(err));
    s_es8311_i2c = NULL;
    return err;
  }
  return ESP_OK;
}

static esp_err_t es8311_read_reg(uint8_t reg, uint8_t *val) {
  if (s_es8311_i2c == NULL || val == NULL) {
    return ESP_ERR_INVALID_STATE;
  }
  return i2c_master_transmit_receive(s_es8311_i2c, &reg, 1, val, 1, 50);
}

static esp_err_t es8311_write_reg(uint8_t reg, uint8_t val) {
  if (s_es8311_i2c == NULL) {
    return ESP_ERR_INVALID_STATE;
  }
  uint8_t buf[2] = {reg, val};
  return i2c_master_transmit(s_es8311_i2c, buf, sizeof(buf), 50);
}

/** Point the ES8311 ADC at analog LIN (board AUX IN), not onboard MIC1. */
static void es8311_select_aux_in(void) {
  if (es8311_ensure_i2c() != ESP_OK) {
    return;
  }

  uint8_t reg14 = 0;
  if (es8311_read_reg(ES8311_REG_ADC_ANALOG, &reg14) != ESP_OK) {
    ESP_LOGW(TAG, "Failed to read ES8311 REG14 for AUX select");
    return;
  }
  const uint8_t next = (uint8_t)((reg14 & (uint8_t)~ES8311_LINSEL_MASK) | ES8311_LINSEL_LIN);
  if (next != reg14) {
    if (es8311_write_reg(ES8311_REG_ADC_ANALOG, next) != ESP_OK) {
      ESP_LOGW(TAG, "Failed to write ES8311 REG14=0x%02X for AUX IN", next);
      return;
    }
    ESP_LOGI(TAG, "ES8311 ADC input LIN/AUX IN (REG14 0x%02X -> 0x%02X)", reg14, next);
  } else if (!s_aux_selected) {
    ESP_LOGI(TAG, "ES8311 ADC already on LIN/AUX (REG14=0x%02X)", reg14);
  }
  s_aux_selected = true;
}

static esp_err_t open_es8311_aux_locked(void) {
  /* Speaker TX and AUX ADC share one ES8311 duplex. Boot leaves the 16 kHz
   * speaker stream warm; TTS is also 16 kHz, so playback would skip reopen
   * and write into a dead I2S path (silent reply, i2s_channel_disable errors). */
  nino_audio_drop_speaker_stream_locked();

  if (s_es8311_mic != NULL) {
    es8311_select_aux_in();
    return ESP_OK;
  }

  esp_err_t err = bsp_i2c_init();
  if (err != ESP_OK && err != ESP_ERR_INVALID_STATE) {
    ESP_LOGE(TAG, "bsp_i2c_init for ES8311 AUX failed: %s",
             esp_err_to_name(err));
    return err;
  }

  s_es8311_mic = bsp_audio_codec_microphone_init();
  if (s_es8311_mic == NULL) {
    ESP_LOGE(TAG, "bsp_audio_codec_microphone_init failed");
    return ESP_FAIL;
  }

  (void)esp_codec_dev_set_in_gain(s_es8311_mic, NINO_ES8311_AUX_GAIN_DB);
  esp_codec_dev_sample_info_t format = {
      .bits_per_sample = 16,
      .channel = 1,
      .channel_mask = 0,
      .sample_rate = NINO_MIC_SAMPLE_RATE_HZ,
      .mclk_multiple = 0,
  };
  const int result = esp_codec_dev_open(s_es8311_mic, &format);
  if (result != ESP_CODEC_DEV_OK) {
    ESP_LOGE(TAG, "esp_codec_dev_open(ES8311 AUX) failed: %d", result);
    close_es8311_mic_locked();
    return ESP_FAIL;
  }

  es8311_select_aux_in();
  ESP_LOGI(TAG, "Using ES8311 AUX IN at %d Hz (gain %.0f dB)",
           NINO_MIC_SAMPLE_RATE_HZ, (double)NINO_ES8311_AUX_GAIN_DB);
  return ESP_OK;
}

nino_mic_source_t nino_mic_preferred_source(void) {
  return NINO_MIC_SOURCE_ES8311_AUX;
}

const char *nino_mic_source_name(nino_mic_source_t source) {
  (void)source;
  return "ES8311 AUX IN";
}

bool nino_mic_available(void) {
  nino_audio_bus_lock();
  const esp_err_t err = open_es8311_aux_locked();
  nino_audio_bus_unlock();
  return err == ESP_OK;
}

esp_err_t nino_mic_read(int16_t *samples, int sample_count) {
  if (samples == NULL || sample_count <= 0) {
    return ESP_ERR_INVALID_ARG;
  }

  nino_audio_bus_lock();
  esp_err_t err = open_es8311_aux_locked();
  if (err == ESP_OK) {
    const int result = esp_codec_dev_read(
        s_es8311_mic, samples, sample_count * (int)sizeof(*samples));
    if (result != ESP_CODEC_DEV_OK) {
      ESP_LOGW(TAG, "ES8311 AUX read failed: %d", result);
      close_es8311_mic_locked();
      err = ESP_FAIL;
    }
  }
  nino_audio_bus_unlock();
  return err;
}

void nino_mic_flush(void) {
  int16_t dump[256];
  for (int i = 0; i < 8; ++i) {
    if (nino_mic_read(dump, 256) != ESP_OK) {
      break;
    }
  }
}

void nino_mic_drop_es8311_locked(void) {
  close_es8311_mic_locked();
}

void nino_mic_close(void) {
  nino_audio_bus_lock();
  close_es8311_mic_locked();
  nino_audio_bus_unlock();
}

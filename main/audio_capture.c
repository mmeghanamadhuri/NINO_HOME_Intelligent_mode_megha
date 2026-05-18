#include "audio_capture.h"

#include <stdlib.h>
#include <string.h>

#include "audio_playback.h"
#include "bsp/esp32_p4_function_ev_board.h"
#include "esp_codec_dev.h"
#include "esp_codec_dev_types.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

static const char *TAG = "nino_cap";

/* Must match I2S "peer" rate after speaker playback (typical SAPI WAV is 22050 Hz). */
#define CAP_SAMPLE_RATE 22050
#define CAP_BYTES_PER_SAMPLE 2
#define CAP_MAX_MS 10000

static void write_le32(uint8_t *p, uint32_t v) {
  p[0] = (uint8_t)(v & 0xff);
  p[1] = (uint8_t)((v >> 8) & 0xff);
  p[2] = (uint8_t)((v >> 16) & 0xff);
  p[3] = (uint8_t)((v >> 24) & 0xff);
}

static void write_le16(uint8_t *p, uint16_t v) {
  p[0] = (uint8_t)(v & 0xff);
  p[1] = (uint8_t)((v >> 8) & 0xff);
}

void nino_audio_capture_free(uint8_t *wav) { free(wav); }

esp_err_t nino_audio_capture_wav(uint8_t **out_wav, size_t *out_len,
                                   uint32_t duration_ms) {
  if (out_wav == NULL || out_len == NULL) {
    return ESP_ERR_INVALID_ARG;
  }
  *out_wav = NULL;
  *out_len = 0;
  if (duration_ms == 0 || duration_ms > CAP_MAX_MS) {
    return ESP_ERR_INVALID_ARG;
  }

  nino_audio_bus_lock();

  esp_err_t err = ESP_FAIL;
  esp_codec_dev_handle_t mic = NULL;
  uint8_t *pcm = NULL;

  err = bsp_i2c_init();
  if (err != ESP_OK && err != ESP_ERR_INVALID_STATE) {
    ESP_LOGE(TAG, "bsp_i2c_init: %s", esp_err_to_name(err));
    goto out_unlock;
  }

  mic = bsp_audio_codec_microphone_init();
  if (mic == NULL) {
    ESP_LOGE(TAG, "bsp_audio_codec_microphone_init failed");
    err = ESP_FAIL;
    goto out_unlock;
  }

  esp_codec_dev_set_in_gain(mic, 35.0f);

  esp_codec_dev_sample_info_t fs = {
      .bits_per_sample = 16,
      .channel = 1,
      .channel_mask = 0,
      .sample_rate = CAP_SAMPLE_RATE,
      .mclk_multiple = 0,
  };

  int cr = esp_codec_dev_open(mic, &fs);
  if (cr != ESP_CODEC_DEV_OK) {
    ESP_LOGE(TAG, "esp_codec_dev_open(mic) failed: %d", cr);
    err = ESP_FAIL;
    goto out_unlock;
  }

  const size_t pcm_bytes =
      (size_t)CAP_SAMPLE_RATE * (size_t)duration_ms / 1000U * CAP_BYTES_PER_SAMPLE;
  pcm = heap_caps_malloc(pcm_bytes, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
  if (pcm == NULL) {
    pcm = malloc(pcm_bytes);
  }
  if (pcm == NULL) {
    esp_codec_dev_close(mic);
    mic = NULL;
    err = ESP_ERR_NO_MEM;
    goto out_unlock;
  }

  size_t got = 0;
  while (got < pcm_bytes) {
    int chunk = (int)(pcm_bytes - got);
    if (chunk > 4096) {
      chunk = 4096;
    }
    cr = esp_codec_dev_read(mic, pcm + got, chunk);
    if (cr != ESP_CODEC_DEV_OK) {
      ESP_LOGE(TAG, "esp_codec_dev_read failed: %d", cr);
      free(pcm);
      pcm = NULL;
      esp_codec_dev_close(mic);
      mic = NULL;
      err = ESP_FAIL;
      goto out_unlock;
    }
    got += (size_t)chunk;
  }

  esp_codec_dev_close(mic);
  mic = NULL;

  const uint32_t data_size = (uint32_t)pcm_bytes;
  const uint32_t riff_size = 36 + data_size;
  const size_t wav_size = 44 + pcm_bytes;
  uint8_t *wav = heap_caps_malloc(wav_size, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
  if (wav == NULL) {
    wav = malloc(wav_size);
  }
  if (wav == NULL) {
    free(pcm);
    pcm = NULL;
    err = ESP_ERR_NO_MEM;
    goto out_unlock;
  }

  memcpy(wav, "RIFF", 4);
  write_le32(wav + 4, riff_size);
  memcpy(wav + 8, "WAVE", 4);
  memcpy(wav + 12, "fmt ", 4);
  write_le32(wav + 16, 16);
  write_le16(wav + 20, 1);
  write_le16(wav + 22, 1);
  write_le32(wav + 24, CAP_SAMPLE_RATE);
  write_le32(wav + 28, CAP_SAMPLE_RATE * CAP_BYTES_PER_SAMPLE);
  write_le16(wav + 32, CAP_BYTES_PER_SAMPLE);
  write_le16(wav + 34, 16);
  memcpy(wav + 36, "data", 4);
  write_le32(wav + 40, data_size);
  memcpy(wav + 44, pcm, pcm_bytes);
  free(pcm);
  pcm = NULL;

  *out_wav = wav;
  *out_len = wav_size;
  ESP_LOGI(TAG, "Captured WAV %u ms, %u bytes", (unsigned)duration_ms,
           (unsigned)wav_size);
  err = ESP_OK;

out_unlock:
  nino_audio_bus_unlock();
  return err;
}

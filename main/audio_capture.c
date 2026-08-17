#include "audio_capture.h"

#include <stdlib.h>
#include <string.h>

#include "esp_heap_caps.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "mic_input.h"
#include "audio_loopback.h"

static const char *TAG = "nino_cap";

#define CAP_SAMPLE_RATE 16000
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
  if (!nino_mic_available()) {
    ESP_LOGW(TAG, "No microphone source is available");
    return ESP_ERR_INVALID_STATE;
  }

  esp_err_t err = ESP_FAIL;
  uint8_t *pcm = NULL;

  const size_t pcm_bytes =
      (size_t)CAP_SAMPLE_RATE * (size_t)duration_ms / 1000U * CAP_BYTES_PER_SAMPLE;
  pcm = heap_caps_malloc(pcm_bytes, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
  if (pcm == NULL) {
    pcm = malloc(pcm_bytes);
  }
  if (pcm == NULL) {
    err = ESP_ERR_NO_MEM;
    goto out;
  }

  nino_audio_loopback_pause();
  nino_mic_flush();

  size_t got = 0;
  while (got < pcm_bytes) {
    const size_t chunk_samples = (pcm_bytes - got) / CAP_BYTES_PER_SAMPLE;
    const int to_read = (chunk_samples > 512) ? 512 : (int)chunk_samples;
    esp_err_t rr = nino_mic_read((int16_t *)(pcm + got), to_read);
    if (rr != ESP_OK) {
      ESP_LOGE(TAG, "%s microphone read failed: %s",
               nino_mic_source_name(nino_mic_preferred_source()),
               esp_err_to_name(rr));
      free(pcm);
      pcm = NULL;
      err = ESP_FAIL;
      goto out;
    }
    got += (size_t)to_read * CAP_BYTES_PER_SAMPLE;
  }

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
    goto out;
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
  ESP_LOGI(TAG, "Captured WAV %u ms, %u bytes (%s)", (unsigned)duration_ms,
           (unsigned)wav_size,
           nino_mic_source_name(nino_mic_preferred_source()));
  err = ESP_OK;

out:
  nino_mic_close();
  nino_audio_loopback_resume();
  return err;
}

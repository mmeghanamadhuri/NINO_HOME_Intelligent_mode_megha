#include "audio_capture.h"

#include <stdlib.h>
#include <string.h>

#include "esp_heap_caps.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "mic_input.h"

static const char *TAG = "nino_cap";

#define CAP_SAMPLE_RATE 16000
#define CAP_BYTES_PER_SAMPLE 2
#define CAP_MAX_MS 10000

static SemaphoreHandle_t s_last_mu;
static uint8_t *s_last_wav;
static size_t s_last_len;

static void last_lock_init(void) {
  if (s_last_mu == NULL) {
    s_last_mu = xSemaphoreCreateMutex();
  }
}

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

static uint32_t pcm_abs_mean(const int16_t *samples, size_t count) {
  uint64_t sum = 0;
  for (size_t i = 0; i < count; i++) {
    int32_t s = samples[i];
    sum += (uint32_t)(s < 0 ? -s : s);
  }
  return count == 0 ? 0 : (uint32_t)(sum / count);
}

static esp_err_t pcm_to_wav(const uint8_t *pcm, size_t pcm_bytes, uint8_t **out_wav,
                            size_t *out_len) {
  const uint32_t data_size = (uint32_t)pcm_bytes;
  const uint32_t riff_size = 36 + data_size;
  const size_t wav_size = 44 + pcm_bytes;
  uint8_t *wav = heap_caps_malloc(wav_size, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
  if (wav == NULL) {
    wav = malloc(wav_size);
  }
  if (wav == NULL) {
    return ESP_ERR_NO_MEM;
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

  *out_wav = wav;
  *out_len = wav_size;
  return ESP_OK;
}

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
    ESP_LOGW(TAG, "ES8311 AUX IN is not available");
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

  nino_mic_flush();

  size_t got = 0;
  while (got < pcm_bytes) {
    const size_t chunk_samples = (pcm_bytes - got) / CAP_BYTES_PER_SAMPLE;
    const int to_read = (chunk_samples > 512) ? 512 : (int)chunk_samples;
    esp_err_t rr = nino_mic_read((int16_t *)(pcm + got), to_read);
    if (rr != ESP_OK) {
      ESP_LOGE(TAG, "%s read failed: %s",
               nino_mic_source_name(nino_mic_preferred_source()),
               esp_err_to_name(rr));
      free(pcm);
      pcm = NULL;
      err = ESP_FAIL;
      goto out;
    }
    got += (size_t)to_read * CAP_BYTES_PER_SAMPLE;
  }

  err = pcm_to_wav(pcm, pcm_bytes, out_wav, out_len);
  free(pcm);
  pcm = NULL;
  if (err != ESP_OK) {
    goto out;
  }
  ESP_LOGI(TAG, "Captured WAV %u ms, %u bytes (%s)", (unsigned)duration_ms,
           (unsigned)*out_len,
           nino_mic_source_name(nino_mic_preferred_source()));

out:
  nino_mic_close();
  return err;
}

esp_err_t nino_audio_capture_save_to_sd(const uint8_t *wav, size_t wav_len,
                                        char *path, size_t path_size) {
  (void)wav;
  (void)wav_len;
  if (path != NULL && path_size > 0) {
    path[0] = '\0';
  }
  /* Aux-in clips stay in RAM (keep_last) / go to the PC. Do not mount SD. */
  return ESP_ERR_NOT_SUPPORTED;
}

esp_err_t nino_audio_capture_keep_last(const uint8_t *wav, size_t wav_len) {
  if (wav == NULL || wav_len < 44) {
    return ESP_ERR_INVALID_ARG;
  }
  last_lock_init();
  if (s_last_mu == NULL) {
    return ESP_ERR_NO_MEM;
  }
  uint8_t *copy = heap_caps_malloc(wav_len, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
  if (copy == NULL) {
    copy = malloc(wav_len);
  }
  if (copy == NULL) {
    return ESP_ERR_NO_MEM;
  }
  memcpy(copy, wav, wav_len);

  xSemaphoreTake(s_last_mu, portMAX_DELAY);
  free(s_last_wav);
  s_last_wav = copy;
  s_last_len = wav_len;
  xSemaphoreGive(s_last_mu);
  return ESP_OK;
}

esp_err_t nino_audio_capture_copy_last(uint8_t **out_wav, size_t *out_len) {
  if (out_wav == NULL || out_len == NULL) {
    return ESP_ERR_INVALID_ARG;
  }
  *out_wav = NULL;
  *out_len = 0;
  last_lock_init();
  if (s_last_mu == NULL) {
    return ESP_ERR_INVALID_STATE;
  }
  xSemaphoreTake(s_last_mu, portMAX_DELAY);
  esp_err_t err = ESP_ERR_NOT_FOUND;
  if (s_last_wav != NULL && s_last_len >= 44) {
    uint8_t *copy =
        heap_caps_malloc(s_last_len, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (copy == NULL) {
      copy = malloc(s_last_len);
    }
    if (copy == NULL) {
      err = ESP_ERR_NO_MEM;
    } else {
      memcpy(copy, s_last_wav, s_last_len);
      *out_wav = copy;
      *out_len = s_last_len;
      err = ESP_OK;
    }
  }
  xSemaphoreGive(s_last_mu);
  return err;
}

esp_err_t nino_audio_capture_wav_until_quiet(
    uint8_t **out_wav, size_t *out_len, const int16_t *preroll,
    size_t preroll_samples, uint32_t min_ms, uint32_t max_ms,
    uint32_t quiet_end_ms, uint32_t quiet_energy, uint32_t speech_energy,
    uint32_t wait_speech_ms, bool flush_first) {
  if (out_wav == NULL || out_len == NULL) {
    return ESP_ERR_INVALID_ARG;
  }
  *out_wav = NULL;
  *out_len = 0;
  if (max_ms == 0 || max_ms > CAP_MAX_MS || min_ms > max_ms) {
    return ESP_ERR_INVALID_ARG;
  }
  if (!nino_mic_available()) {
    ESP_LOGW(TAG, "ES8311 AUX IN is not available");
    return ESP_ERR_INVALID_STATE;
  }

  const size_t preroll_bytes = preroll_samples * CAP_BYTES_PER_SAMPLE;
  const size_t live_max_bytes =
      (size_t)CAP_SAMPLE_RATE * (size_t)max_ms / 1000U * CAP_BYTES_PER_SAMPLE;
  const size_t pcm_max = preroll_bytes + live_max_bytes;
  uint8_t *pcm = heap_caps_malloc(pcm_max, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
  if (pcm == NULL) {
    pcm = malloc(pcm_max);
  }
  if (pcm == NULL) {
    nino_mic_close();
    return ESP_ERR_NO_MEM;
  }

  size_t got = 0;
  if (preroll != NULL && preroll_bytes > 0) {
    memcpy(pcm, preroll, preroll_bytes);
    got = preroll_bytes;
  }

  if (flush_first) {
    nino_mic_flush();
  }

#define CAP_FRAME_SAMPLES 320
  int16_t frame[CAP_FRAME_SAMPLES];
  uint32_t elapsed_ms = 0;
  uint32_t quiet_ms = 0;
  bool heard_speech = false;
  const uint32_t speech_th =
      speech_energy > quiet_energy ? speech_energy : (quiet_energy + 80U);
  esp_err_t err = ESP_OK;

  while (got + (CAP_FRAME_SAMPLES * CAP_BYTES_PER_SAMPLE) <= pcm_max &&
         elapsed_ms < max_ms) {
    esp_err_t rr = nino_mic_read(frame, CAP_FRAME_SAMPLES);
    if (rr != ESP_OK) {
      ESP_LOGE(TAG, "%s read failed: %s",
               nino_mic_source_name(nino_mic_preferred_source()),
               esp_err_to_name(rr));
      err = ESP_FAIL;
      break;
    }
    memcpy(pcm + got, frame, CAP_FRAME_SAMPLES * CAP_BYTES_PER_SAMPLE);
    got += CAP_FRAME_SAMPLES * CAP_BYTES_PER_SAMPLE;
    elapsed_ms += 20;

    const uint32_t energy = pcm_abs_mean(frame, CAP_FRAME_SAMPLES);
    if (energy >= speech_th && (elapsed_ms % 200U) == 0U) {
      int16_t mn = 32767;
      int16_t mx = -32768;
      for (int i = 0; i < CAP_FRAME_SAMPLES; i++) {
        if (frame[i] < mn) {
          mn = frame[i];
        }
        if (frame[i] > mx) {
          mx = frame[i];
        }
      }
      ESP_LOGI(TAG, "I2S IN audio energy=%u min=%d max=%d th=%u at=%u ms",
               (unsigned)energy, (int)mn, (int)mx, (unsigned)speech_th,
               (unsigned)elapsed_ms);
    }
    if (elapsed_ms < min_ms) {
      /* Wake gap: keep recording, never end on quiet. */
      quiet_ms = 0;
      continue;
    }
    if (energy >= speech_th) {
      if (!heard_speech) {
        ESP_LOGI(TAG, "NINO VOICE | QUESTION | energy=%u th=%u at=%u ms",
                 (unsigned)energy, (unsigned)speech_th, (unsigned)elapsed_ms);
      }
      heard_speech = true;
      quiet_ms = 0;
      continue;
    }
    if (!heard_speech) {
      if (wait_speech_ms == 0) {
        quiet_ms += 20;
        if (quiet_end_ms > 0 && quiet_ms >= quiet_end_ms) {
          ESP_LOGI(TAG, "NINO VOICE | SENTENCE | reason=quiet_no_wait live=%u ms",
                   (unsigned)elapsed_ms);
          break;
        }
        continue;
      }
      if ((elapsed_ms - min_ms) >= wait_speech_ms) {
        ESP_LOGI(TAG, "NINO VOICE | NO_Q     | waited=%u ms — wake clip only",
                 (unsigned)wait_speech_ms);
        break;
      }
      continue;
    }
    quiet_ms += 20;
    if (quiet_end_ms > 0 && quiet_ms >= quiet_end_ms) {
      ESP_LOGI(TAG,
               "NINO VOICE | SENTENCE | quiet=%u ms live=%u ms energy=%u th=%u",
               (unsigned)quiet_ms, (unsigned)elapsed_ms, (unsigned)energy,
               (unsigned)quiet_energy);
      break;
    }
  }
#undef CAP_FRAME_SAMPLES

  if (err != ESP_OK || got <= preroll_bytes) {
    free(pcm);
    nino_mic_close();
    return err != ESP_OK ? err : ESP_FAIL;
  }

  err = pcm_to_wav(pcm, got, out_wav, out_len);
  free(pcm);
  nino_mic_close();
  if (err != ESP_OK) {
    return err;
  }
  const unsigned total_ms =
      (unsigned)((got / CAP_BYTES_PER_SAMPLE) * 1000U / CAP_SAMPLE_RATE);
  ESP_LOGI(TAG,
           "NINO VOICE | WAV      | total=%u ms live=%u ms bytes=%u src=%s",
           total_ms, (unsigned)elapsed_ms, (unsigned)*out_len,
           nino_mic_source_name(nino_mic_preferred_source()));
  return ESP_OK;
}

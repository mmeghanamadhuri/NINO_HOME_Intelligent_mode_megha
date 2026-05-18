#include "audio_playback.h"

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>

#include "voice_wake.h"

#include "bsp/esp32_p4_function_ev_board.h"
#include "esp_codec_dev.h"
#include "esp_codec_dev_types.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"

static const char *TAG = "nino_audio";

static SemaphoreHandle_t s_mutex;
static esp_codec_dev_handle_t s_spk;
static bool s_ready;

/** Let I2S/codec finish samples already queued (avoids truncated two-tone beeps). */
static void wait_pcm_pipeline_done(uint32_t sample_rate_hz, size_t pcm_bytes,
                                   TickType_t write_started) {
  if (sample_rate_hz == 0 || pcm_bytes == 0) {
    return;
  }
  const uint32_t audio_ms =
      (uint32_t)((pcm_bytes * 1000ULL) / (sample_rate_hz * 2ULL));
  const TickType_t now = xTaskGetTickCount();
  const uint32_t elapsed_ms =
      (uint32_t)((now - write_started) * portTICK_PERIOD_MS);
  const uint32_t pipeline_margin_ms = 100;
  uint32_t wait_ms = pipeline_margin_ms;
  if (elapsed_ms < audio_ms) {
    wait_ms = (audio_ms - elapsed_ms) + pipeline_margin_ms;
  }
  vTaskDelay(pdMS_TO_TICKS(wait_ms));
}

static uint32_t read_le32(const uint8_t *p) {
  return (uint32_t)p[0] | ((uint32_t)p[1] << 8) | ((uint32_t)p[2] << 16) |
         ((uint32_t)p[3] << 24);
}

static uint16_t read_le16(const uint8_t *p) {
  return (uint16_t)(p[0] | (p[1] << 8));
}

typedef struct {
  const uint8_t *pcm;
  size_t pcm_len;
  uint32_t sample_rate;
  uint16_t channels;
  uint16_t bits_per_sample;
} wav_pcm_t;

static bool parse_wav_pcm(const uint8_t *buf, size_t len, wav_pcm_t *out) {
  memset(out, 0, sizeof(*out));
  if (len < 44) {
    return false;
  }
  if (memcmp(buf, "RIFF", 4) != 0 || memcmp(buf + 8, "WAVE", 4) != 0) {
    return false;
  }

  bool have_fmt = false;
  bool have_data = false;
  uint16_t audio_format = 0;
  size_t off = 12;

  while (off + 8 <= len) {
    const uint8_t *hdr = buf + off;
    uint32_t chunk_size = read_le32(hdr + 4);
    off += 8;
    if (off + chunk_size > len) {
      return false;
    }
    const uint8_t *chunk_data = buf + off;

    if (memcmp(hdr, "fmt ", 4) == 0) {
      if (chunk_size < 16) {
        return false;
      }
      audio_format = read_le16(chunk_data);
      out->channels = read_le16(chunk_data + 2);
      out->sample_rate = read_le32(chunk_data + 4);
      out->bits_per_sample = read_le16(chunk_data + 14);
      have_fmt = true;
    } else if (memcmp(hdr, "data", 4) == 0) {
      out->pcm = chunk_data;
      out->pcm_len = chunk_size;
      have_data = true;
    }

    off += chunk_size;
    if ((chunk_size & 1U) != 0U) {
      off += 1;
    }
  }

  if (!have_fmt || !have_data) {
    return false;
  }
  /* 1 = PCM; 0xFFFE = WAVE_FORMAT_EXTENSIBLE (often still 16-bit PCM from Windows SAPI). */
  if (audio_format != 1 && audio_format != (uint16_t)0xFFFE) {
    return false;
  }
  if (out->bits_per_sample != 16) {
    return false;
  }
  if (out->channels < 1 || out->channels > 2) {
    return false;
  }
  if (out->sample_rate < 8000 || out->sample_rate > 48000) {
    return false;
  }
  return true;
}

esp_err_t nino_audio_init(void) {
  if (s_mutex == NULL) {
    s_mutex = xSemaphoreCreateMutex();
    if (s_mutex == NULL) {
      return ESP_ERR_NO_MEM;
    }
  }

  xSemaphoreTake(s_mutex, portMAX_DELAY);
  esp_err_t err = ESP_OK;
  if (!s_ready) {
    err = bsp_i2c_init();
    if (err != ESP_OK && err != ESP_ERR_INVALID_STATE) {
      ESP_LOGE(TAG, "bsp_i2c_init failed: %s", esp_err_to_name(err));
      xSemaphoreGive(s_mutex);
      return err;
    }

    s_spk = bsp_audio_codec_speaker_init();
    if (s_spk == NULL) {
      ESP_LOGE(TAG, "bsp_audio_codec_speaker_init failed");
      xSemaphoreGive(s_mutex);
      return ESP_FAIL;
    }

    esp_codec_dev_set_out_vol(s_spk, 100);
    s_ready = true;
    ESP_LOGI(TAG, "Speaker ready (ES8311)");
  }
  xSemaphoreGive(s_mutex);
  return ESP_OK;
}

esp_err_t nino_audio_play_pcm16_mono(const int16_t *samples, size_t sample_count,
                                     uint32_t sample_rate_hz) {
  if (samples == NULL || sample_count == 0 || sample_rate_hz < 8000 ||
      sample_rate_hz > 48000) {
    return ESP_ERR_INVALID_ARG;
  }

  if (!s_ready) {
    esp_err_t e = nino_audio_init();
    if (e != ESP_OK) {
      return e;
    }
  }

  const size_t play_len = sample_count * sizeof(int16_t);
  xSemaphoreTake(s_mutex, portMAX_DELAY);

  esp_codec_dev_sample_info_t fs = {
      .bits_per_sample = 16,
      .channel = 1,
      .channel_mask = 0,
      .sample_rate = sample_rate_hz,
      .mclk_multiple = 0,
  };

  int cr = esp_codec_dev_open(s_spk, &fs);
  if (cr != ESP_CODEC_DEV_OK) {
    ESP_LOGE(TAG, "esp_codec_dev_open failed: %d", cr);
    xSemaphoreGive(s_mutex);
    return ESP_FAIL;
  }

  const TickType_t write_started = xTaskGetTickCount();
  const uint8_t *play_ptr = (const uint8_t *)samples;
  size_t offset = 0;
  while (offset < play_len) {
    int block = (int)(play_len - offset);
    if (block > 8192) {
      block = 8192;
    }
    cr = esp_codec_dev_write(s_spk, (void *)(play_ptr + offset), block);
    if (cr != ESP_CODEC_DEV_OK) {
      ESP_LOGE(TAG, "esp_codec_dev_write failed: %d", cr);
      break;
    }
    offset += (size_t)block;
  }

  if (cr == ESP_CODEC_DEV_OK) {
    wait_pcm_pipeline_done(sample_rate_hz, play_len, write_started);
  }

  esp_codec_dev_close(s_spk);
  nino_voice_wake_drop_mic_locked();
  xSemaphoreGive(s_mutex);
  return (cr == ESP_CODEC_DEV_OK) ? ESP_OK : ESP_FAIL;
}

esp_err_t nino_audio_play_wav(const uint8_t *wav_bytes, size_t wav_len) {
  wav_pcm_t wav;
  if (!parse_wav_pcm(wav_bytes, wav_len, &wav)) {
    ESP_LOGE(TAG, "Invalid WAV (need PCM 16-bit mono or stereo, 8–48 kHz; fmt 1 or 0xFFFE)");
    return ESP_ERR_INVALID_ARG;
  }

  if (!s_ready) {
    esp_err_t e = nino_audio_init();
    if (e != ESP_OK) {
      return e;
    }
  }

  const int16_t *in_samples = (const int16_t *)wav.pcm;
  size_t in_bytes = wav.pcm_len;
  int16_t *mono_heap = NULL;
  const uint8_t *play_ptr = wav.pcm;
  size_t play_len = wav.pcm_len;

  if (wav.channels == 2) {
    size_t frames = in_bytes / (sizeof(int16_t) * 2);
    size_t mono_bytes = frames * sizeof(int16_t);
    mono_heap = (int16_t *)heap_caps_malloc(
        mono_bytes, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (mono_heap == NULL) {
      mono_heap = (int16_t *)malloc(mono_bytes);
    }
    if (mono_heap == NULL) {
      return ESP_ERR_NO_MEM;
    }
    for (size_t i = 0; i < frames; i++) {
      int32_t L = in_samples[i * 2];
      int32_t R = in_samples[i * 2 + 1];
      mono_heap[i] = (int16_t)((L + R) / 2);
    }
    play_ptr = (const uint8_t *)mono_heap;
    play_len = mono_bytes;
  }

  xSemaphoreTake(s_mutex, portMAX_DELAY);

  esp_codec_dev_sample_info_t fs = {
      .bits_per_sample = 16,
      .channel = 1,
      .channel_mask = 0,
      .sample_rate = wav.sample_rate,
      .mclk_multiple = 0,
  };

  int cr = esp_codec_dev_open(s_spk, &fs);
  if (cr != ESP_CODEC_DEV_OK) {
    ESP_LOGE(TAG, "esp_codec_dev_open failed: %d", cr);
    free(mono_heap);
    xSemaphoreGive(s_mutex);
    return ESP_FAIL;
  }

  const TickType_t write_started = xTaskGetTickCount();
  size_t offset = 0;
  while (offset < play_len) {
    int block = (int)(play_len - offset);
    if (block > 4096) {
      block = 4096;
    }
    cr = esp_codec_dev_write(s_spk, (void *)(play_ptr + offset), block);
    if (cr != ESP_CODEC_DEV_OK) {
      ESP_LOGE(TAG, "esp_codec_dev_write failed: %d", cr);
      break;
    }
    offset += (size_t)block;
  }

  if (cr == ESP_CODEC_DEV_OK) {
    wait_pcm_pipeline_done(wav.sample_rate, play_len, write_started);
  }

  esp_codec_dev_close(s_spk);
  nino_voice_wake_drop_mic_locked();
  free(mono_heap);
  xSemaphoreGive(s_mutex);

  return (cr == ESP_CODEC_DEV_OK) ? ESP_OK : ESP_FAIL;
}

void nino_audio_bus_lock(void) {
  if (s_mutex == NULL) {
    (void)nino_audio_init();
  }
  if (s_mutex != NULL) {
    xSemaphoreTake(s_mutex, portMAX_DELAY);
  }
}

void nino_audio_bus_unlock(void) {
  if (s_mutex != NULL) {
    xSemaphoreGive(s_mutex);
  }
}

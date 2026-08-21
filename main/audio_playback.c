#include "audio_playback.h"

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>

#include "mic_input.h"
#include "battery_adc.h"

#include "bsp/esp32_p4_function_ev_board.h"
#include "esp_codec_dev.h"
#include "esp_codec_dev_types.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "nvs.h"

static const char *TAG = "nino_audio";

#define NINO_AUDIO_NVS_NS "nino_audio"
#define NINO_AUDIO_NVS_KEY_VOL "vol"
#define NINO_AUDIO_DEFAULT_VOLUME 80

/** Mild treble cut + headroom so 16 kHz TTS is less harsh on the EV speaker. */
static void soften_playback_pcm(int16_t *pcm, size_t samples) {
  if (pcm == NULL || samples == 0) {
    return;
  }
  int32_t prev = (int32_t)pcm[0];
  for (size_t i = 0; i < samples; i++) {
    const int32_t x = (int32_t)pcm[i];
    const int32_t lp = (x * 5 + prev * 3) / 8;
    prev = lp;
    int32_t y = (lp * 88) / 100;
    if (y > 32767) {
      y = 32767;
    } else if (y < -32768) {
      y = -32768;
    }
    pcm[i] = (int16_t)y;
  }
}

static SemaphoreHandle_t s_mutex;
static esp_codec_dev_handle_t s_spk;
static bool s_ready;
static int s_volume_percent = NINO_AUDIO_DEFAULT_VOLUME;
static volatile bool s_user_muted;
static bool s_spk_stream_open;
static uint32_t s_spk_stream_rate_hz;
/* Set when Aux-in opens/closes the same ES8311. Next speaker open must reopen
 * the DAC even if the firmware still thinks the stream is live. */
static bool s_spk_force_reopen;

static void audio_persist_volume(int volume_percent) {
  nvs_handle_t h;
  if (nvs_open(NINO_AUDIO_NVS_NS, NVS_READWRITE, &h) != ESP_OK) {
    return;
  }
  if (nvs_set_i32(h, NINO_AUDIO_NVS_KEY_VOL, (int32_t)volume_percent) == ESP_OK) {
    nvs_commit(h);
  }
  nvs_close(h);
}

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

static esp_err_t spk_stream_open_clocks_locked(void) {
  if (s_spk == NULL) {
    return ESP_FAIL;
  }
  if (s_spk_stream_open && !s_spk_force_reopen && s_spk_stream_rate_hz == 16000) {
    (void)esp_codec_dev_set_out_mute(s_spk, true);
    return ESP_OK;
  }

  if (s_spk != NULL && s_spk_stream_open) {
    (void)esp_codec_dev_set_out_mute(s_spk, true);
    (void)esp_codec_dev_close(s_spk);
    s_spk_stream_open = false;
    s_spk_stream_rate_hz = 0;
  }

  esp_codec_dev_sample_info_t fs = {
      .bits_per_sample = 16,
      .channel = 1,
      .channel_mask = 0,
      .sample_rate = 16000,
      .mclk_multiple = 0,
  };
  const int cr = esp_codec_dev_open(s_spk, &fs);
  if (cr != ESP_CODEC_DEV_OK) {
    ESP_LOGE(TAG, "esp_codec_dev_open (clocks) failed: %d", cr);
    s_spk_force_reopen = true;
    return ESP_FAIL;
  }
  s_spk_stream_open = true;
  s_spk_stream_rate_hz = 16000;
  s_spk_force_reopen = false;
  (void)esp_codec_dev_set_out_vol(s_spk, s_volume_percent);
  (void)esp_codec_dev_set_out_mute(s_spk, true);
  {
    int16_t zeros[160] = {0};
    (void)esp_codec_dev_write(s_spk, zeros, sizeof(zeros));
  }
  ESP_LOGI(TAG, "Duplex I2S clocks held @ 16000 Hz (DAC muted for Aux-in)");
  return ESP_OK;
}

static void spk_stream_close_locked(void) {
  if (s_spk != NULL && s_spk_stream_open) {
    (void)esp_codec_dev_close(s_spk);
  }
  s_spk_stream_open = false;
  s_spk_stream_rate_hz = 0;
}

static esp_err_t spk_stream_open_locked(uint32_t sample_rate_hz, bool leave_open) {
  if (s_spk == NULL) {
    return ESP_FAIL;
  }
  /* Always reopen. TTS is 16 kHz, same as the warm chime / AUX path, so the
   * old skip-reopen branch wrote into a dead I2S (silent speaker, i2s errors). */
  nino_mic_drop_es8311_locked();
  spk_stream_close_locked();

  esp_codec_dev_sample_info_t fs = {
      .bits_per_sample = 16,
      .channel = 1,
      .channel_mask = 0,
      .sample_rate = sample_rate_hz,
      .mclk_multiple = 0,
  };
  const int cr = esp_codec_dev_open(s_spk, &fs);
  if (cr != ESP_CODEC_DEV_OK) {
    ESP_LOGE(TAG, "esp_codec_dev_open failed: %d", cr);
    s_spk_force_reopen = true;
    return ESP_FAIL;
  }
  s_spk_stream_open = true;
  s_spk_stream_rate_hz = sample_rate_hz;
  s_spk_force_reopen = false;
  (void)esp_codec_dev_set_out_vol(s_spk, s_volume_percent);
  (void)esp_codec_dev_set_out_mute(s_spk, s_user_muted &&
                                             !nino_battery_low_alert_active());
  ESP_LOGI(TAG, "Speaker opened @ %u Hz vol=%d%% mute=%d",
           (unsigned)sample_rate_hz, s_volume_percent, (int)s_user_muted);
  (void)leave_open;
  return ESP_OK;
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

bool nino_audio_wav_bytes_valid(const uint8_t *wav_bytes, size_t wav_len) {
  wav_pcm_t wav;
  return parse_wav_pcm(wav_bytes, wav_len, &wav);
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

    esp_codec_dev_set_out_vol(s_spk, s_volume_percent);
    s_ready = true;
    ESP_LOGI(TAG, "Speaker ready (ES8311), volume=%d%%", s_volume_percent);
  }
  xSemaphoreGive(s_mutex);
  return ESP_OK;
}

esp_err_t nino_audio_set_volume_percent(int volume_percent) {
  if (volume_percent < 0 || volume_percent > 100) {
    return ESP_ERR_INVALID_ARG;
  }

  if (s_mutex == NULL) {
    esp_err_t err = nino_audio_init();
    if (err != ESP_OK) {
      return err;
    }
  }

  xSemaphoreTake(s_mutex, portMAX_DELAY);
  s_volume_percent = volume_percent;
  if (s_spk != NULL) {
    (void)esp_codec_dev_set_out_vol(s_spk, s_volume_percent);
  }
  xSemaphoreGive(s_mutex);

  audio_persist_volume(volume_percent);
  ESP_LOGI(TAG, "Speaker volume set to %d%%", s_volume_percent);
  return ESP_OK;
}

int nino_audio_get_volume_percent(void) { return s_volume_percent; }

static void apply_codec_mute_locked(void) {
  if (s_spk == NULL) {
    return;
  }
  const bool mute = s_user_muted && !nino_battery_low_alert_active();
  (void)esp_codec_dev_set_out_mute(s_spk, mute);
}

bool nino_audio_is_muted(void) { return s_user_muted; }

void nino_audio_refresh_mute(void) {
  if (s_mutex == NULL) {
    return;
  }
  xSemaphoreTake(s_mutex, portMAX_DELAY);
  apply_codec_mute_locked();
  xSemaphoreGive(s_mutex);
}

esp_err_t nino_audio_set_muted(bool muted) {
  if (s_mutex == NULL) {
    esp_err_t err = nino_audio_init();
    if (err != ESP_OK) {
      s_user_muted = muted;
      return err;
    }
  }

  xSemaphoreTake(s_mutex, portMAX_DELAY);
  s_user_muted = muted;
  apply_codec_mute_locked();
  xSemaphoreGive(s_mutex);
  ESP_LOGI(TAG, "Speaker %s", muted ? "MUTED" : "unmuted");
  return ESP_OK;
}

esp_err_t nino_audio_load_saved_volume(void) {
  int volume_percent = NINO_AUDIO_DEFAULT_VOLUME;
  nvs_handle_t h;
  if (nvs_open(NINO_AUDIO_NVS_NS, NVS_READONLY, &h) == ESP_OK) {
    int32_t stored = NINO_AUDIO_DEFAULT_VOLUME;
    if (nvs_get_i32(h, NINO_AUDIO_NVS_KEY_VOL, &stored) == ESP_OK &&
        stored >= 0 && stored <= 100) {
      volume_percent = (int)stored;
      ESP_LOGI(TAG, "Loaded saved speaker volume from NVS: %d%%", volume_percent);
    } else {
      ESP_LOGI(TAG, "No saved volume in NVS, using default %d%%", volume_percent);
    }
    nvs_close(h);
  }
  return nino_audio_set_volume_percent(volume_percent);
}

esp_err_t nino_audio_warm_chime_path(uint32_t sample_rate_hz) {
  if (sample_rate_hz < 8000 || sample_rate_hz > 48000) {
    return ESP_ERR_INVALID_ARG;
  }
  if (!s_ready) {
    esp_err_t e = nino_audio_init();
    if (e != ESP_OK) {
      return e;
    }
  }
  xSemaphoreTake(s_mutex, portMAX_DELAY);
  esp_err_t e = spk_stream_open_locked(sample_rate_hz, true);
  if (e == ESP_OK) {
    /* Prime I2S DMA so the first wake beep does not pay open latency on cold start. */
    int16_t silence[320] = {0};
    (void)esp_codec_dev_write(s_spk, silence, sizeof(silence));
    vTaskDelay(pdMS_TO_TICKS(25));
    ESP_LOGI(TAG, "Chime path warm @ %u Hz", (unsigned)sample_rate_hz);
  }
  xSemaphoreGive(s_mutex);
  return e;
}

esp_err_t nino_audio_play_chime_pcm16_mono(const int16_t *samples, size_t sample_count,
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

  esp_err_t err = spk_stream_open_locked(sample_rate_hz, true);
  if (err != ESP_OK) {
    xSemaphoreGive(s_mutex);
    return err;
  }

  const TickType_t write_started = xTaskGetTickCount();
  const uint8_t *play_ptr = (const uint8_t *)samples;
  size_t offset = 0;
  int cr = ESP_CODEC_DEV_OK;
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
    if (sample_rate_hz == 0 || play_len == 0) {
      /* skip */
    } else {
      const uint32_t audio_ms =
          (uint32_t)((play_len * 1000ULL) / (sample_rate_hz * 2ULL));
      const TickType_t now = xTaskGetTickCount();
      const uint32_t elapsed_ms =
          (uint32_t)((now - write_started) * portTICK_PERIOD_MS);
      const uint32_t pipeline_margin_ms = 40;
      uint32_t wait_ms = pipeline_margin_ms;
      if (elapsed_ms < audio_ms) {
        wait_ms = (audio_ms - elapsed_ms) + pipeline_margin_ms;
      }
      vTaskDelay(pdMS_TO_TICKS(wait_ms));
    }
  }

  nino_mic_drop_es8311_locked();
  xSemaphoreGive(s_mutex);
  return (cr == ESP_CODEC_DEV_OK) ? ESP_OK : ESP_FAIL;
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

  esp_err_t err = spk_stream_open_locked(sample_rate_hz, false);
  if (err != ESP_OK) {
    xSemaphoreGive(s_mutex);
    return err;
  }

  const TickType_t write_started = xTaskGetTickCount();
  const uint8_t *play_ptr = (const uint8_t *)samples;
  size_t offset = 0;
  int cr = ESP_CODEC_DEV_OK;
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

  spk_stream_close_locked();
  nino_mic_drop_es8311_locked();
  xSemaphoreGive(s_mutex);
  return (cr == ESP_CODEC_DEV_OK) ? ESP_OK : ESP_FAIL;
}

esp_err_t nino_audio_decode_wav(const uint8_t *wav_bytes, size_t wav_len,
                                nino_decoded_wav_t *out) {
  if (out == NULL) {
    return ESP_ERR_INVALID_ARG;
  }
  memset(out, 0, sizeof(*out));

  wav_pcm_t wav;
  if (!parse_wav_pcm(wav_bytes, wav_len, &wav)) {
    ESP_LOGE(TAG, "Invalid WAV (need PCM 16-bit mono or stereo, 8–48 kHz; fmt 1 or 0xFFFE)");
    return ESP_ERR_INVALID_ARG;
  }

  const int16_t *in_samples = (const int16_t *)wav.pcm;
  size_t in_bytes = wav.pcm_len;
  int16_t *pcm_owned = NULL;
  const int16_t *samples = in_samples;
  size_t num_bytes = wav.pcm_len;

  if (wav.channels == 2) {
    size_t frames = in_bytes / (sizeof(int16_t) * 2);
    size_t mono_bytes = frames * sizeof(int16_t);
    pcm_owned = (int16_t *)heap_caps_malloc(
        mono_bytes, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (pcm_owned == NULL) {
      pcm_owned = (int16_t *)malloc(mono_bytes);
    }
    if (pcm_owned == NULL) {
      return ESP_ERR_NO_MEM;
    }
    for (size_t i = 0; i < frames; i++) {
      int32_t L = in_samples[i * 2];
      int32_t R = in_samples[i * 2 + 1];
      pcm_owned[i] = (int16_t)((L + R) / 2);
    }
    samples = pcm_owned;
    num_bytes = mono_bytes;
  } else {
    pcm_owned = (int16_t *)heap_caps_malloc(num_bytes, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (pcm_owned == NULL) {
      pcm_owned = (int16_t *)malloc(num_bytes);
    }
    if (pcm_owned == NULL) {
      return ESP_ERR_NO_MEM;
    }
    memcpy(pcm_owned, in_samples, num_bytes);
    samples = pcm_owned;
  }

  soften_playback_pcm(pcm_owned, num_bytes / sizeof(int16_t));

  out->samples = samples;
  out->num_bytes = num_bytes;
  out->sample_rate_hz = wav.sample_rate;
  out->mono_heap = pcm_owned;
  return ESP_OK;
}

void nino_decoded_wav_free(nino_decoded_wav_t *decoded) {
  if (decoded == NULL) {
    return;
  }
  free(decoded->mono_heap);
  memset(decoded, 0, sizeof(*decoded));
}

esp_err_t nino_audio_play_decoded(const nino_decoded_wav_t *decoded, size_t *pcm_byte_offset,
                                  volatile bool *stop_requested, bool *completed,
                                  bool leave_codec_open) {
  if (decoded == NULL || pcm_byte_offset == NULL || completed == NULL) {
    return ESP_ERR_INVALID_ARG;
  }
  *completed = false;

  if (decoded->samples == NULL || decoded->num_bytes == 0) {
    *completed = true;
    return ESP_OK;
  }

  if (!s_ready) {
    esp_err_t e = nino_audio_init();
    if (e != ESP_OK) {
      return e;
    }
  }

  size_t offset = *pcm_byte_offset;
  if (offset >= decoded->num_bytes) {
    *completed = true;
    return ESP_OK;
  }

  const uint8_t *play_ptr = (const uint8_t *)decoded->samples;

  xSemaphoreTake(s_mutex, portMAX_DELAY);

  esp_err_t open_err = spk_stream_open_locked(decoded->sample_rate_hz, leave_codec_open);
  if (open_err != ESP_OK) {
    xSemaphoreGive(s_mutex);
    return open_err;
  }
  ESP_LOGI(TAG, "Playing %u bytes @ %u Hz vol=%d%%",
           (unsigned)(decoded->num_bytes - offset),
           (unsigned)decoded->sample_rate_hz, s_volume_percent);

  const size_t session_start = offset;
  const TickType_t write_started = xTaskGetTickCount();
  bool stopped = false;
  int cr = ESP_CODEC_DEV_OK;
  /* Interruptible clips: small writes + ~40 ms lead so pause can cut without
   * waiting for a whole queued chunk already sitting in DMA. */
  const int max_block = (stop_requested != NULL) ? 512 : 4096;
  const uint32_t write_ahead_ms = (stop_requested != NULL) ? 40U : 0U;

  while (offset < decoded->num_bytes) {
    if (stop_requested != NULL && *stop_requested) {
      stopped = true;
      break;
    }
    if (!s_spk_stream_open) {
      stopped = true;
      break;
    }

    int block = (int)(decoded->num_bytes - offset);
    if (block > max_block) {
      block = max_block;
    }
    cr = esp_codec_dev_write(s_spk, (void *)(play_ptr + offset), block);
    if (cr != ESP_CODEC_DEV_OK) {
      /* Pause/stop often close the codec while a write is in flight. */
      stopped = true;
      if (stop_requested == NULL || !(*stop_requested)) {
        ESP_LOGE(TAG, "esp_codec_dev_write failed: %d", cr);
      }
      cr = ESP_CODEC_DEV_OK;
      break;
    }
    offset += (size_t)block;

    if (stop_requested != NULL && *stop_requested) {
      stopped = true;
      break;
    }

    if (write_ahead_ms > 0 && decoded->sample_rate_hz > 0) {
      const uint32_t audio_ms = (uint32_t)(((offset - session_start) * 1000ULL) /
                                           (decoded->sample_rate_hz * 2ULL));
      const uint32_t elapsed_ms =
          (uint32_t)((xTaskGetTickCount() - write_started) * portTICK_PERIOD_MS);
      if (audio_ms > elapsed_ms + write_ahead_ms) {
        uint32_t sleep_ms = audio_ms - elapsed_ms - write_ahead_ms;
        xSemaphoreGive(s_mutex);
        while (sleep_ms > 0) {
          if (stop_requested != NULL && *stop_requested) {
            stopped = true;
            break;
          }
          const uint32_t slice = sleep_ms > 10U ? 10U : sleep_ms;
          vTaskDelay(pdMS_TO_TICKS(slice));
          sleep_ms -= slice;
        }
        xSemaphoreTake(s_mutex, portMAX_DELAY);
        if (stopped) {
          break;
        }
        if (!s_spk_stream_open) {
          stopped = true;
          break;
        }
      }
    }
  }

  if (stopped && decoded->sample_rate_hz > 0) {
    /* DMA still holds a few tens of ms that close() discards. Rewind so
     * resume does not skip that unplayed tail. */
    const size_t rewind =
        (size_t)((decoded->sample_rate_hz * 2ULL * 80ULL) / 1000ULL);
    if (offset > rewind) {
      offset -= rewind;
    } else {
      offset = session_start;
    }
    if (offset < session_start) {
      offset = session_start;
    }
  }
  *pcm_byte_offset = offset;

  if (!stopped && cr == ESP_CODEC_DEV_OK && offset >= decoded->num_bytes) {
    const size_t written = offset - session_start;
    if (!leave_codec_open) {
      wait_pcm_pipeline_done(decoded->sample_rate_hz, written, write_started);
    }
    *completed = true;
    ESP_LOGI(TAG, "Speaker finished %u bytes @ %u Hz%s", (unsigned)written,
             (unsigned)decoded->sample_rate_hz,
             leave_codec_open ? " (codec held)" : "");
  }

  if (!leave_codec_open || stopped) {
    if (s_spk != NULL && s_spk_stream_open) {
      (void)esp_codec_dev_set_out_mute(s_spk, true);
    }
    spk_stream_close_locked();
    nino_mic_drop_es8311_locked();
    s_spk_force_reopen = true;
  }
  xSemaphoreGive(s_mutex);

  if (cr != ESP_CODEC_DEV_OK) {
    return ESP_FAIL;
  }
  return ESP_OK;
}

void nino_audio_close_speaker(void) {
  nino_audio_bus_lock();
  spk_stream_close_locked();
  nino_mic_drop_es8311_locked();
  nino_audio_bus_unlock();
}

void nino_audio_cut_speaker(void) {
  if (s_mutex == NULL) {
    return;
  }
  /* Do not block behind a long clip write; the play loop also watches the
   * stop flag. 40 ms covers one small PCM write. */
  if (xSemaphoreTake(s_mutex, pdMS_TO_TICKS(40)) != pdTRUE) {
    ESP_LOGW(TAG, "cut speaker: codec busy");
    return;
  }
  if (s_spk != NULL && s_spk_stream_open) {
    (void)esp_codec_dev_set_out_mute(s_spk, true);
  }
  spk_stream_close_locked();
  nino_mic_drop_es8311_locked();
  s_spk_force_reopen = true;
  xSemaphoreGive(s_mutex);
}

#define NINO_AUDIO_WRITE_CHUNK 4096

esp_err_t nino_audio_write_pcm16_mono_locked(const int16_t *samples, size_t sample_count,
                                             uint32_t sample_rate_hz) {
  if (!s_ready) {
    return ESP_ERR_INVALID_STATE;
  }
  if (s_spk == NULL) {
    return ESP_ERR_INVALID_STATE;
  }
  if (sample_rate_hz < 8000 || sample_rate_hz > 48000) {
    return ESP_ERR_INVALID_ARG;
  }
  if (sample_count > 0 && samples == NULL) {
    return ESP_ERR_INVALID_ARG;
  }

  esp_err_t err = spk_stream_open_locked(sample_rate_hz, true);
  if (err != ESP_OK) {
    return err;
  }
  if (sample_count == 0) {
    return ESP_OK;
  }

  const size_t play_len = sample_count * sizeof(int16_t);
  const uint8_t *play_ptr = (const uint8_t *)samples;
  size_t offset = 0;
  while (offset < play_len) {
    int block = (int)(play_len - offset);
    if (block > NINO_AUDIO_WRITE_CHUNK) {
      block = NINO_AUDIO_WRITE_CHUNK;
    }
    const int cr = esp_codec_dev_write(s_spk, (void *)(play_ptr + offset), block);
    if (cr != ESP_CODEC_DEV_OK) {
      ESP_LOGE(TAG, "esp_codec_dev_write failed: %d", cr);
      return ESP_FAIL;
    }
    offset += (size_t)block;
  }
  return ESP_OK;
}

esp_err_t nino_audio_play_wav(const uint8_t *wav_bytes, size_t wav_len) {
  nino_decoded_wav_t decoded = {};
  esp_err_t err = nino_audio_decode_wav(wav_bytes, wav_len, &decoded);
  if (err != ESP_OK) {
    return err;
  }

  size_t offset = 0;
  bool completed = false;
  err = nino_audio_play_decoded(&decoded, &offset, NULL, &completed, false);
  nino_decoded_wav_free(&decoded);
  if (err != ESP_OK) {
    return err;
  }
  return completed ? ESP_OK : ESP_FAIL;
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

void nino_audio_drop_speaker_stream_locked(void) {
  if (s_spk_stream_open) {
    ESP_LOGI(TAG, "Closing speaker I2S so AUX ADC can take the duplex");
  }
  spk_stream_close_locked();
  s_spk_force_reopen = true;
}

esp_err_t nino_audio_ensure_duplex_clocks_locked(void) {
  return spk_stream_open_clocks_locked();
}

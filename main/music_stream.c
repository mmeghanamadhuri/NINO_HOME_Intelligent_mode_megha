#include "music_stream.h"

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>

#include "audio_playback.h"
#include "audio_queue.h"
#include "esp_heap_caps.h"
#include "esp_http_client.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/stream_buffer.h"
#include "freertos/task.h"
#include "voice_assist.h"

static const char *TAG = "nino_music";

#define MUSIC_RING_BYTES (256 * 1024) /* PSRAM, ~4 s at 32 kHz mono */
#define MUSIC_PREBUFFER_BYTES (96 * 1024) /* ~1.5 s before first sample */
#define MUSIC_HTTP_CHUNK 4096
#define MUSIC_WRITE_CHUNK 4096
#define MUSIC_FEED_TASK_STACK 8192
#define MUSIC_FEED_TASK_PRIO 4 /* > wake (3), < audio_play (6) */
#define MUSIC_HTTP_TASK_STACK 10240
#define MUSIC_HTTP_TASK_PRIO 4
#define MUSIC_URL_MAX 512
#define MUSIC_HDR_MAX 512
#define MUSIC_STOP_WAIT_MS 6000
#define MUSIC_HTTP_TIMEOUT_MS 4000

static SemaphoreHandle_t s_op_mu;
static StreamBufferHandle_t s_ring;
static uint8_t *s_ring_storage;
static StaticStreamBuffer_t s_ring_ctrl;

static char s_url[MUSIC_URL_MAX];
static volatile bool s_inited;
static volatile bool s_active;
static volatile bool s_stop;
static volatile bool s_speech_pause;
static volatile bool s_http_eof;
static volatile bool s_header_ok;
static volatile bool s_http_running;
static volatile bool s_feed_running;
static volatile uint32_t s_rate_hz;
static volatile uint32_t s_underruns;

static uint32_t read_le32(const uint8_t *p) {
  return (uint32_t)p[0] | ((uint32_t)p[1] << 8) | ((uint32_t)p[2] << 16) |
         ((uint32_t)p[3] << 24);
}

static uint16_t read_le16(const uint8_t *p) {
  return (uint16_t)(p[0] | (p[1] << 8));
}

static bool speech_holds_speaker(void) {
  return s_speech_pause || nino_voice_assist_query_is_busy() ||
         nino_audio_queue_busy();
}

static bool parse_wav_stream_header(const uint8_t *buf, size_t len, uint32_t *rate,
                                    uint16_t *channels, uint16_t *bits,
                                    size_t *data_off) {
  if (buf == NULL || rate == NULL || channels == NULL || bits == NULL ||
      data_off == NULL || len < 44) {
    return false;
  }
  if (memcmp(buf, "RIFF", 4) != 0 || memcmp(buf + 8, "WAVE", 4) != 0) {
    return false;
  }

  bool have_fmt = false;
  uint16_t audio_format = 0;
  size_t off = 12;
  while (off + 8 <= len) {
    const uint8_t *hdr = buf + off;
    const uint32_t chunk_size = read_le32(hdr + 4);
    if (memcmp(hdr, "fmt ", 4) == 0) {
      if (chunk_size < 16 || off + 8 + 16 > len) {
        return false;
      }
      audio_format = read_le16(buf + off + 8);
      *channels = read_le16(buf + off + 10);
      *rate = read_le32(buf + off + 12);
      *bits = read_le16(buf + off + 22);
      have_fmt = true;
      if (chunk_size > (UINT32_MAX - 9u)) {
        return false;
      }
      off += 8 + (size_t)chunk_size;
      if (chunk_size & 1u) {
        off++;
      }
      continue;
    }
    if (memcmp(hdr, "data", 4) == 0) {
      /* Do not trust RIFF/data sizes — stream length is unknown. */
      *data_off = off + 8;
      if (!have_fmt) {
        return false;
      }
      if (*bits != 16 || (*channels != 1 && *channels != 2)) {
        return false;
      }
      if (*rate < 8000 || *rate > 48000) {
        return false;
      }
      if (audio_format != 1 && audio_format != 0xFFFE) {
        return false;
      }
      return true;
    }
    if (chunk_size > (UINT32_MAX - 9u)) {
      return false;
    }
    off += 8 + (size_t)chunk_size;
    if (chunk_size & 1u) {
      off++;
    }
  }
  return false;
}

static bool ring_push(const uint8_t *data, size_t len) {
  size_t sent = 0;
  while (sent < len && !s_stop) {
    while (s_speech_pause && !s_stop) {
      vTaskDelay(pdMS_TO_TICKS(20));
    }
    if (s_stop) {
      break;
    }
    const size_t n = xStreamBufferSend(s_ring, data + sent, len - sent,
                                       pdMS_TO_TICKS(100));
    sent += n;
  }
  return sent == len;
}

static void downmix_push_mono(const uint8_t *pcm, size_t bytes, uint16_t channels,
                              uint8_t *leftover, size_t *leftover_n) {
  uint8_t tmp[MUSIC_HTTP_CHUNK + 4];
  size_t have = *leftover_n;
  if (have > 0) {
    memcpy(tmp, leftover, have);
  }
  if (bytes > sizeof(tmp) - have) {
    bytes = sizeof(tmp) - have;
  }
  memcpy(tmp + have, pcm, bytes);
  have += bytes;

  const size_t frame_bytes = (size_t)channels * sizeof(int16_t);
  const size_t frames = have / frame_bytes;
  const size_t consume = frames * frame_bytes;
  if (frames == 0) {
    memcpy(leftover, tmp, have);
    *leftover_n = have;
    return;
  }

  if (channels == 1) {
    (void)ring_push(tmp, consume);
  } else {
    const int16_t *src = (const int16_t *)tmp;
    int16_t mono[MUSIC_HTTP_CHUNK / 2];
    size_t out_frames = 0;
    for (size_t i = 0; i < frames; i++) {
      const int32_t l = src[2 * i];
      const int32_t r = src[2 * i + 1];
      mono[out_frames++] = (int16_t)((l + r) / 2);
      if (out_frames >= (sizeof(mono) / sizeof(mono[0]))) {
        (void)ring_push((const uint8_t *)mono, out_frames * sizeof(int16_t));
        out_frames = 0;
      }
    }
    if (out_frames > 0) {
      (void)ring_push((const uint8_t *)mono, out_frames * sizeof(int16_t));
    }
  }

  *leftover_n = have - consume;
  if (*leftover_n > 0) {
    memcpy(leftover, tmp + consume, *leftover_n);
  }
}

static void music_http_task(void *arg) {
  (void)arg;
  s_http_running = true;
  if (s_stop) {
    s_http_eof = true;
    s_http_running = false;
    vTaskDelete(NULL);
    return;
  }

  char url[MUSIC_URL_MAX];
  strncpy(url, s_url, sizeof(url) - 1);
  url[sizeof(url) - 1] = '\0';

  esp_http_client_config_t config = {
      .url = url,
      .method = HTTP_METHOD_GET,
      .timeout_ms = MUSIC_HTTP_TIMEOUT_MS,
      .buffer_size = MUSIC_HTTP_CHUNK,
      .keep_alive_enable = true,
  };
  esp_http_client_handle_t client = esp_http_client_init(&config);
  if (client == NULL) {
    ESP_LOGE(TAG, "HTTP client alloc failed");
    s_http_eof = true;
    s_http_running = false;
    vTaskDelete(NULL);
    return;
  }

  esp_err_t err = esp_http_client_open(client, 0);
  if (err != ESP_OK) {
    ESP_LOGE(TAG, "HTTP open failed: %s", esp_err_to_name(err));
    esp_http_client_cleanup(client);
    s_http_eof = true;
    s_http_running = false;
    vTaskDelete(NULL);
    return;
  }

  const int content_len = esp_http_client_fetch_headers(client);
  const int status = esp_http_client_get_status_code(client);
  ESP_LOGI(TAG, "GET %s -> HTTP %d (content_len=%d)", url, status, content_len);
  if (status < 200 || status >= 300) {
    ESP_LOGE(TAG, "Stream HTTP status %d", status);
    esp_http_client_close(client);
    esp_http_client_cleanup(client);
    s_http_eof = true;
    s_http_running = false;
    vTaskDelete(NULL);
    return;
  }

  uint8_t hdr[MUSIC_HDR_MAX];
  size_t hdr_n = 0;
  uint8_t chunk[MUSIC_HTTP_CHUNK];
  uint32_t rate = 0;
  uint16_t channels = 1;
  uint16_t bits = 16;
  size_t data_off = 0;
  bool header_done = false;

  while (!s_stop && !header_done && hdr_n < sizeof(hdr)) {
    const int n = esp_http_client_read(client, (char *)chunk,
                                       (int)(sizeof(hdr) - hdr_n));
    if (n < 0) {
      ESP_LOGE(TAG, "HTTP read failed while parsing WAV header");
      break;
    }
    if (n == 0) {
      ESP_LOGE(TAG, "HTTP EOF before WAV header completed");
      break;
    }
    memcpy(hdr + hdr_n, chunk, (size_t)n);
    hdr_n += (size_t)n;
    if (parse_wav_stream_header(hdr, hdr_n, &rate, &channels, &bits, &data_off)) {
      header_done = true;
    }
  }

  if (!header_done || data_off > hdr_n) {
    ESP_LOGE(TAG, "Could not parse streaming WAV header (%u bytes)",
             (unsigned)hdr_n);
    esp_http_client_close(client);
    esp_http_client_cleanup(client);
    s_http_eof = true;
    s_http_running = false;
    vTaskDelete(NULL);
    return;
  }

  s_rate_hz = rate;
  s_header_ok = true;
  ESP_LOGI(TAG, "WAV header: %u Hz, %u ch, %u-bit, data_off=%u (sizes ignored)",
           (unsigned)rate, (unsigned)channels, (unsigned)bits, (unsigned)data_off);

  uint8_t leftover[4] = {0};
  size_t leftover_n = 0;
  if (hdr_n > data_off) {
    downmix_push_mono(hdr + data_off, hdr_n - data_off, channels, leftover,
                      &leftover_n);
  }

  while (!s_stop) {
    if (s_speech_pause) {
      vTaskDelay(pdMS_TO_TICKS(20));
      continue;
    }
    const int n = esp_http_client_read(client, (char *)chunk, MUSIC_HTTP_CHUNK);
    if (n < 0) {
      ESP_LOGW(TAG, "HTTP read error, ending stream");
      break;
    }
    if (n == 0) {
      ESP_LOGI(TAG, "HTTP stream closed by server");
      break;
    }
    downmix_push_mono(chunk, (size_t)n, channels, leftover, &leftover_n);
  }

  esp_http_client_close(client);
  esp_http_client_cleanup(client);
  s_http_eof = true;
  s_http_running = false;
  ESP_LOGI(TAG, "music HTTP task exit (stop=%d underruns=%u)", (int)s_stop,
           (unsigned)s_underruns);
  vTaskDelete(NULL);
}

static void release_speaker_if_ours(void) {
  if (s_speech_pause) {
    return;
  }
  nino_audio_bus_lock();
  nino_audio_drop_speaker_stream_locked();
  nino_audio_bus_unlock();
  (void)nino_audio_warm_chime_path(16000);
}

static void music_feed_task(void *arg) {
  (void)arg;
  s_feed_running = true;
  if (s_stop) {
    s_active = false;
    s_feed_running = false;
    vTaskDelete(NULL);
    return;
  }

  uint8_t chunk[MUSIC_WRITE_CHUNK];
  bool codec_armed = false;
  bool started = false;
  uint32_t underruns = 0;

  while (!s_header_ok && !s_stop && !s_http_eof) {
    vTaskDelay(pdMS_TO_TICKS(10));
  }
  if (!s_header_ok || s_stop) {
    s_feed_running = false;
    s_active = false;
    vTaskDelete(NULL);
    return;
  }

  const uint32_t rate = s_rate_hz;

  while (!s_stop) {
    if (speech_holds_speaker()) {
      codec_armed = false;
      vTaskDelay(pdMS_TO_TICKS(20));
      continue;
    }

    const size_t avail = xStreamBufferBytesAvailable(s_ring);
    if (!started) {
      if (avail < MUSIC_PREBUFFER_BYTES && !s_http_eof) {
        vTaskDelay(pdMS_TO_TICKS(10));
        continue;
      }
      started = true;
      ESP_LOGI(TAG, "Prebuffer ready (%u bytes), opening speaker @ %u Hz",
               (unsigned)avail, (unsigned)rate);
    }

    if (avail == 0) {
      if (s_http_eof) {
        break;
      }
      underruns++;
      s_underruns = underruns;
      if ((underruns % 10u) == 1u) {
        ESP_LOGW(TAG, "music underrun (count=%u)", (unsigned)underruns);
      }
      started = false; /* re-prebuffer after jitter */
      vTaskDelay(pdMS_TO_TICKS(10));
      continue;
    }

    const size_t got =
        xStreamBufferReceive(s_ring, chunk, MUSIC_WRITE_CHUNK, pdMS_TO_TICKS(50));
    if (got < 2) {
      continue;
    }
    const size_t even = got & ~(size_t)1;

    nino_audio_bus_lock();
    if (!codec_armed) {
      const esp_err_t open_err =
          nino_audio_write_pcm16_mono_locked(NULL, 0, rate);
      if (open_err != ESP_OK) {
        nino_audio_bus_unlock();
        ESP_LOGE(TAG, "Speaker open failed: %s", esp_err_to_name(open_err));
        vTaskDelay(pdMS_TO_TICKS(50));
        continue;
      }
      codec_armed = true;
    }
    const esp_err_t wr = nino_audio_write_pcm16_mono_locked(
        (const int16_t *)chunk, even / sizeof(int16_t), rate);
    nino_audio_bus_unlock();
    if (wr != ESP_OK) {
      ESP_LOGW(TAG, "PCM write failed: %s", esp_err_to_name(wr));
      codec_armed = false;
    }
  }

  ESP_LOGI(TAG, "music feed exit (stop=%d eof=%d underruns=%u)", (int)s_stop,
           (int)s_http_eof, (unsigned)underruns);
  if (codec_armed) {
    release_speaker_if_ours();
  }
  s_active = false;
  s_feed_running = false;
  vTaskDelete(NULL);
}

static void wait_session_tasks(uint32_t timeout_ms) {
  const TickType_t deadline = xTaskGetTickCount() + pdMS_TO_TICKS(timeout_ms);
  while ((s_http_running || s_feed_running) && xTaskGetTickCount() < deadline) {
    vTaskDelay(pdMS_TO_TICKS(10));
  }
  if (s_http_running || s_feed_running) {
    ESP_LOGW(TAG, "Music session tasks still running after %u ms",
             (unsigned)timeout_ms);
  }
}

esp_err_t nino_music_init(void) {
  if (s_inited) {
    return ESP_OK;
  }

  s_op_mu = xSemaphoreCreateMutex();
  if (s_op_mu == NULL) {
    return ESP_ERR_NO_MEM;
  }

  s_ring_storage =
      heap_caps_malloc(MUSIC_RING_BYTES + 1, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
  if (s_ring_storage == NULL) {
    s_ring_storage = malloc(MUSIC_RING_BYTES + 1);
  }
  if (s_ring_storage == NULL) {
    vSemaphoreDelete(s_op_mu);
    s_op_mu = NULL;
    ESP_LOGE(TAG, "Failed to allocate %u-byte music ring",
             (unsigned)(MUSIC_RING_BYTES + 1));
    return ESP_ERR_NO_MEM;
  }

  s_ring = xStreamBufferCreateStatic(MUSIC_RING_BYTES + 1, 1, s_ring_storage,
                                     &s_ring_ctrl);
  if (s_ring == NULL) {
    free(s_ring_storage);
    s_ring_storage = NULL;
    vSemaphoreDelete(s_op_mu);
    s_op_mu = NULL;
    ESP_LOGE(TAG, "Failed to create music stream buffer");
    return ESP_ERR_NO_MEM;
  }

  s_inited = true;
  ESP_LOGI(TAG, "Music stream ready (ring=%u KiB, prebuffer=%u KiB)",
           (unsigned)(MUSIC_RING_BYTES / 1024),
           (unsigned)(MUSIC_PREBUFFER_BYTES / 1024));
  return ESP_OK;
}

void nino_music_stop(void) {
  if (!s_inited) {
    return;
  }
  xSemaphoreTake(s_op_mu, portMAX_DELAY);
  s_stop = true;
  s_speech_pause = false;
  wait_session_tasks(MUSIC_STOP_WAIT_MS);
  s_active = false;
  if (s_ring != NULL && !s_http_running && !s_feed_running) {
    (void)xStreamBufferReset(s_ring);
  }
  xSemaphoreGive(s_op_mu);
}

esp_err_t nino_music_start(const char *url) {
  if (url == NULL || url[0] == '\0') {
    return ESP_ERR_INVALID_ARG;
  }
  if (strncmp(url, "http://", 7) != 0 && strncmp(url, "https://", 8) != 0) {
    return ESP_ERR_INVALID_ARG;
  }
  if (!s_inited) {
    esp_err_t e = nino_music_init();
    if (e != ESP_OK) {
      return e;
    }
  }

  xSemaphoreTake(s_op_mu, portMAX_DELAY);
  s_stop = true;
  s_speech_pause = false;
  wait_session_tasks(MUSIC_STOP_WAIT_MS);
  if (s_http_running || s_feed_running) {
    xSemaphoreGive(s_op_mu);
    ESP_LOGE(TAG, "Previous music session still running");
    return ESP_ERR_INVALID_STATE;
  }

  strncpy(s_url, url, sizeof(s_url) - 1);
  s_url[sizeof(s_url) - 1] = '\0';
  s_stop = false;
  s_http_eof = false;
  s_header_ok = false;
  s_rate_hz = 0;
  s_underruns = 0;
  s_speech_pause =
      nino_voice_assist_query_is_busy() || nino_audio_queue_busy();
  s_active = true;
  if (s_ring != NULL) {
    (void)xStreamBufferReset(s_ring);
  }

  s_http_running = true;
  s_feed_running = true;
  BaseType_t http_ok =
      xTaskCreate(music_http_task, "music_http", MUSIC_HTTP_TASK_STACK, NULL,
                  MUSIC_HTTP_TASK_PRIO, NULL);
  if (http_ok != pdPASS) {
    s_http_running = false;
  }
  BaseType_t feed_ok =
      xTaskCreate(music_feed_task, "music_feed", MUSIC_FEED_TASK_STACK, NULL,
                  MUSIC_FEED_TASK_PRIO, NULL);
  if (feed_ok != pdPASS) {
    s_feed_running = false;
  }
  if (http_ok != pdPASS || feed_ok != pdPASS) {
    s_stop = true;
    s_active = false;
    wait_session_tasks(MUSIC_STOP_WAIT_MS);
    xSemaphoreGive(s_op_mu);
    ESP_LOGE(TAG, "Could not start music tasks");
    return ESP_ERR_NO_MEM;
  }

  ESP_LOGI(TAG, "Music start %s", s_url);
  xSemaphoreGive(s_op_mu);
  return ESP_OK;
}

bool nino_music_is_playing(void) { return s_active && !s_stop; }

void nino_music_pause_for_speech(bool paused) {
  if (!s_active) {
    s_speech_pause = paused;
    return;
  }
  if (s_speech_pause == paused) {
    return;
  }
  s_speech_pause = paused;
  ESP_LOGI(TAG, "Music %s for speech", paused ? "paused" : "resumed");
}

bool nino_music_blocks_mic(void) {
  return s_active && !s_stop && !s_speech_pause;
}

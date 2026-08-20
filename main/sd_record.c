#include "sd_record.h"

#include <inttypes.h>
#include <stdio.h>
#include <string.h>

#include "bsp/esp32_p4_function_ev_board.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_vfs_fat.h"
#include "sdmmc_cmd.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/semphr.h"
#include "freertos/task.h"

static const char *TAG = "sd_rec";

#define REC_SAMPLE_RATE 16000
#define REC_BYTES_PER_SAMPLE 2
#define REC_CHUNK_MS 15000
#define REC_CHUNK_SAMPLES ((size_t)REC_SAMPLE_RATE * (REC_CHUNK_MS / 1000))
#define REC_CHUNK_BYTES (REC_CHUNK_SAMPLES * REC_BYTES_PER_SAMPLE)
#define REC_WRITE_TASK_STACK 6144
#define REC_WRITE_TASK_PRIO 3

typedef struct {
  uint8_t *pcm;
  size_t bytes;
} rec_job_t;

static bool s_mounted;
static SemaphoreHandle_t s_fill_mu;
static QueueHandle_t s_jobs;
static uint8_t *s_fill;
static size_t s_fill_bytes;
static uint32_t s_seq;

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

static uint8_t *alloc_pcm(size_t bytes) {
  uint8_t *p = heap_caps_malloc(bytes, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
  if (p == NULL) {
    p = malloc(bytes);
  }
  return p;
}

static esp_err_t write_wav_file(const uint8_t *pcm, size_t pcm_bytes) {
  if (!s_mounted || pcm == NULL || pcm_bytes < REC_BYTES_PER_SAMPLE) {
    return ESP_ERR_INVALID_ARG;
  }

  s_seq++;
  char path[64];
  int n = snprintf(path, sizeof(path), BSP_SD_MOUNT_POINT "/rec_%04" PRIu32 ".wav",
                   s_seq);
  if (n < 0 || (size_t)n >= sizeof(path)) {
    return ESP_ERR_INVALID_SIZE;
  }

  const uint32_t data_size = (uint32_t)pcm_bytes;
  const uint32_t riff_size = 36 + data_size;
  uint8_t hdr[44];
  memcpy(hdr, "RIFF", 4);
  write_le32(hdr + 4, riff_size);
  memcpy(hdr + 8, "WAVE", 4);
  memcpy(hdr + 12, "fmt ", 4);
  write_le32(hdr + 16, 16);
  write_le16(hdr + 20, 1);
  write_le16(hdr + 22, 1);
  write_le32(hdr + 24, REC_SAMPLE_RATE);
  write_le32(hdr + 28, REC_SAMPLE_RATE * REC_BYTES_PER_SAMPLE);
  write_le16(hdr + 32, REC_BYTES_PER_SAMPLE);
  write_le16(hdr + 34, 16);
  memcpy(hdr + 36, "data", 4);
  write_le32(hdr + 40, data_size);

  FILE *f = fopen(path, "wb");
  if (f == NULL) {
    ESP_LOGE(TAG, "fopen(%s) failed", path);
    return ESP_FAIL;
  }
  const size_t hdr_ok = fwrite(hdr, 1, sizeof(hdr), f);
  const size_t pcm_ok = fwrite(pcm, 1, pcm_bytes, f);
  fclose(f);
  if (hdr_ok != sizeof(hdr) || pcm_ok != pcm_bytes) {
    ESP_LOGE(TAG, "WAV write incomplete %s", path);
    return ESP_FAIL;
  }

  const unsigned ms =
      (unsigned)((pcm_bytes / REC_BYTES_PER_SAMPLE) * 1000U / REC_SAMPLE_RATE);
  ESP_LOGI(TAG, "Saved %u ms Aux-in WAV (%u bytes) to %s", ms, (unsigned)pcm_bytes,
           path);
  return ESP_OK;
}

static void rec_write_task(void *arg) {
  (void)arg;
  rec_job_t job;
  while (true) {
    if (xQueueReceive(s_jobs, &job, portMAX_DELAY) != pdTRUE) {
      continue;
    }
    if (job.pcm != NULL && job.bytes > 0) {
      (void)write_wav_file(job.pcm, job.bytes);
    }
    free(job.pcm);
  }
}

static void queue_filled_chunk(void) {
  if (s_fill == NULL || s_fill_bytes == 0) {
    return;
  }
  rec_job_t job = {
      .pcm = s_fill,
      .bytes = s_fill_bytes,
  };
  s_fill = alloc_pcm(REC_CHUNK_BYTES);
  s_fill_bytes = 0;
  if (s_fill == NULL) {
    ESP_LOGW(TAG, "No memory for next 15 s Aux-in buffer");
  }
  if (xQueueSend(s_jobs, &job, 0) != pdTRUE) {
    ESP_LOGW(TAG, "SD writer busy — dropped 15 s Aux-in clip");
    free(job.pcm);
  }
}

esp_err_t nino_sd_record_init(void) {
  if (s_mounted) {
    return ESP_OK;
  }

  static const esp_vfs_fat_sdmmc_mount_config_t mount_config = {
      .format_if_mount_failed = false,
      .max_files = 5,
      .allocation_unit_size = 16 * 1024,
  };
  bsp_sdcard_cfg_t cfg = {
      .mount = &mount_config,
  };

  esp_err_t err = bsp_sdcard_sdmmc_mount(&cfg);
  if (err != ESP_OK) {
    ESP_LOGW(TAG,
             "SD mount failed: %s — insert a FAT32/exFAT card to save Aux-in",
             esp_err_to_name(err));
    return err;
  }

  s_fill_mu = xSemaphoreCreateMutex();
  s_jobs = xQueueCreate(2, sizeof(rec_job_t));
  s_fill = alloc_pcm(REC_CHUNK_BYTES);
  if (s_fill_mu == NULL || s_jobs == NULL || s_fill == NULL) {
    ESP_LOGE(TAG, "SD recorder alloc failed");
    return ESP_ERR_NO_MEM;
  }

  BaseType_t ok = xTaskCreate(rec_write_task, "sd_rec", REC_WRITE_TASK_STACK, NULL,
                              REC_WRITE_TASK_PRIO, NULL);
  if (ok != pdPASS) {
    ESP_LOGE(TAG, "SD writer task failed");
    return ESP_ERR_NO_MEM;
  }

  s_mounted = true;
  sdmmc_card_t *card = bsp_sdcard_get_handle();
  if (card != NULL) {
    ESP_LOGI(TAG, "SD ready at %s (%s) — Aux-in saved every %d s",
             BSP_SD_MOUNT_POINT, card->cid.name, REC_CHUNK_MS / 1000);
  } else {
    ESP_LOGI(TAG, "SD ready at %s — Aux-in saved every %d s", BSP_SD_MOUNT_POINT,
             REC_CHUNK_MS / 1000);
  }
  return ESP_OK;
}

bool nino_sd_record_ready(void) { return s_mounted; }

void nino_sd_record_feed(const int16_t *samples, int sample_count) {
  if (!s_mounted || samples == NULL || sample_count <= 0 || s_fill_mu == NULL) {
    return;
  }
  const uint8_t *src = (const uint8_t *)samples;
  size_t remain = (size_t)sample_count * REC_BYTES_PER_SAMPLE;

  if (xSemaphoreTake(s_fill_mu, 0) != pdTRUE) {
    return;
  }
  while (remain > 0 && s_fill != NULL) {
    const size_t room = REC_CHUNK_BYTES - s_fill_bytes;
    const size_t n = remain < room ? remain : room;
    memcpy(s_fill + s_fill_bytes, src, n);
    s_fill_bytes += n;
    src += n;
    remain -= n;
    if (s_fill_bytes >= REC_CHUNK_BYTES) {
      queue_filled_chunk();
    }
  }
  xSemaphoreGive(s_fill_mu);
}

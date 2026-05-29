#include "audio_queue.h"

#include <stdlib.h>
#include <string.h>

#include "audio_playback.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/task.h"
#include "servo_dxl.h"
#include "servo_motion.h"
#include "voice_assist.h"

static const char *TAG = "audio_q";

/** Deep FIFO so server + touch + voice never drop; enqueue blocks when full. */
#define AUDIO_PLAY_QUEUE_LEN 32
#define AUDIO_PLAY_TASK_STACK_SIZE 6144
#define AUDIO_PLAY_TASK_PRIORITY 6

typedef struct {
  uint8_t *data;
  size_t len;
  bool play_done_chime;
  nino_audio_servo_mode_t servo_mode;
} audio_play_job_t;

static QueueHandle_t s_audio_play_queue;

static void servo_motion_for_mode(nino_audio_servo_mode_t mode, bool start) {
  if (mode == NINO_AUDIO_SERVO_NONE) {
    return;
  }
  if (start) {
    if (!nino_servo_dxl_bus_open()) {
      ESP_LOGW(TAG, "Speaker clip with servo motion — U2D2 not on USB yet (J18 hub?)");
    }
    nino_servo_motion_start(
        mode == NINO_AUDIO_SERVO_NOD_LR ? NINO_SERVO_MOTION_NOD_LR : NINO_SERVO_MOTION_FULL);
  } else {
    nino_servo_motion_stop();
  }
}

static void audio_playback_task(void *arg) {
  (void)arg;
  audio_play_job_t job = {};

  while (true) {
    if (xQueueReceive(s_audio_play_queue, &job, portMAX_DELAY) != pdPASS) {
      continue;
    }
    if (job.data == NULL || job.len == 0) {
      free(job.data);
      continue;
    }

    servo_motion_for_mode(job.servo_mode, true);
    esp_err_t play_err = nino_audio_play_wav(job.data, job.len);
    servo_motion_for_mode(job.servo_mode, false);

    if (play_err != ESP_OK) {
      ESP_LOGW(TAG, "WAV playback failed: %s", esp_err_to_name(play_err));
    } else if (job.play_done_chime) {
      (void)nino_voice_play_done_chime();
    }
    free(job.data);
  }
}

static esp_err_t enqueue_job(audio_play_job_t job) {
  if (s_audio_play_queue == NULL) {
    free(job.data);
    return ESP_ERR_INVALID_STATE;
  }
  if (job.data == NULL || job.len == 0) {
    free(job.data);
    return ESP_ERR_INVALID_ARG;
  }

  if (xQueueSendToBack(s_audio_play_queue, &job, portMAX_DELAY) != pdPASS) {
    free(job.data);
    return ESP_FAIL;
  }
  return ESP_OK;
}

esp_err_t nino_audio_queue_start(void) {
  if (s_audio_play_queue != NULL) {
    return ESP_OK;
  }

  s_audio_play_queue =
      xQueueCreate(AUDIO_PLAY_QUEUE_LEN, sizeof(audio_play_job_t));
  if (s_audio_play_queue == NULL) {
    ESP_LOGE(TAG, "Failed to create audio play queue");
    return ESP_ERR_NO_MEM;
  }

  BaseType_t ok =
      xTaskCreate(audio_playback_task, "audio_play", AUDIO_PLAY_TASK_STACK_SIZE,
                  NULL, AUDIO_PLAY_TASK_PRIORITY, NULL);
  if (ok != pdPASS) {
    vQueueDelete(s_audio_play_queue);
    s_audio_play_queue = NULL;
    ESP_LOGE(TAG, "Failed to create audio play task");
    return ESP_ERR_NO_MEM;
  }

  ESP_LOGI(TAG, "Audio FIFO ready (depth %d, blocking enqueue)", AUDIO_PLAY_QUEUE_LEN);
  return ESP_OK;
}

esp_err_t nino_audio_queue_wav(uint8_t *wav, size_t len, bool play_done_chime,
                               nino_audio_servo_mode_t servo_mode) {
  if (wav == NULL || len == 0) {
    free(wav);
    return ESP_ERR_INVALID_ARG;
  }

  audio_play_job_t job = {
      .data = wav,
      .len = len,
      .play_done_chime = play_done_chime,
      .servo_mode = servo_mode,
  };
  return enqueue_job(job);
}

esp_err_t nino_audio_queue_wav_copy(const uint8_t *wav, size_t len, bool play_done_chime,
                                    nino_audio_servo_mode_t servo_mode) {
  if (wav == NULL || len == 0) {
    return ESP_ERR_INVALID_ARG;
  }

  uint8_t *copy = heap_caps_malloc(len, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
  if (copy == NULL) {
    copy = malloc(len);
  }
  if (copy == NULL) {
    ESP_LOGE(TAG, "Out of memory copying %u-byte WAV", (unsigned)len);
    return ESP_ERR_NO_MEM;
  }
  memcpy(copy, wav, len);
  return nino_audio_queue_wav(copy, len, play_done_chime, servo_mode);
}

void nino_main_queue_audio_wav(uint8_t *pcm_wav, size_t len, bool play_done_chime) {
  esp_err_t err =
      nino_audio_queue_wav(pcm_wav, len, play_done_chime, NINO_AUDIO_SERVO_FULL);
  if (err != ESP_OK) {
    ESP_LOGW(TAG, "voice: queue WAV failed: %s", esp_err_to_name(err));
  }
}

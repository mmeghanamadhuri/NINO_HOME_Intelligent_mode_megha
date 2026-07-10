#include "audio_queue.h"

#include <stdlib.h>
#include <string.h>

#include "audio_playback.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "face_tracker.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/task.h"
#include "nino_eye.h"
#include "servo_dxl.h"
#include "servo_motion.h"
#include "voice_assist.h"

static const char *TAG = "audio_q";

#define NORMAL_QUEUE_LEN 32
#define TOUCH_QUEUE_LEN 8
#define AUDIO_PLAY_TASK_STACK_SIZE 6144
#define AUDIO_PLAY_TASK_PRIORITY 6
#define WAKE_CHIME_RATE_HZ 16000

static void rewarm_wake_chime_path(void) {
  (void)nino_audio_warm_chime_path(WAKE_CHIME_RATE_HZ);
}

typedef struct {
  uint8_t *data;
  size_t len;
  bool play_done_chime;
  nino_audio_servo_mode_t servo_mode;
  bool prompt_ack_after;
  nino_eye_state_t eye_state; /* expression while playing; NINO_EYE_STATE_COUNT = none */
} audio_play_job_t;

typedef struct {
  nino_decoded_wav_t decoded;
  size_t pcm_offset;
  bool play_done_chime;
  nino_audio_servo_mode_t servo_mode;
  bool prompt_ack_after;
} suspended_playback_t;

static QueueHandle_t s_normal_queue;
static QueueHandle_t s_touch_queue;
static volatile bool s_stop_requested;
static volatile bool s_normal_playing;
static suspended_playback_t s_suspended;
static bool s_has_suspended;

static bool is_touch_job(const audio_play_job_t *job) {
  return job->servo_mode == NINO_AUDIO_SERVO_NOD_LR ||
         job->servo_mode == NINO_AUDIO_SERVO_PRIORITY_NONE;
}

static nino_audio_servo_mode_t effective_servo_mode(nino_audio_servo_mode_t mode) {
  if (nino_face_tracker_is_enabled() &&
      (mode == NINO_AUDIO_SERVO_FULL || mode == NINO_AUDIO_SERVO_NOD_LR)) {
    ESP_LOGI(TAG, "Face tracking on — WAV audio only (no L/R/U/D)");
    return NINO_AUDIO_SERVO_NONE;
  }
  return mode;
}

static void servo_motion_for_mode(nino_audio_servo_mode_t mode, bool start) {
  if (mode == NINO_AUDIO_SERVO_NONE ||
      mode == NINO_AUDIO_SERVO_PRIORITY_NONE) {
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

static bool play_decoded_job(nino_decoded_wav_t *decoded, size_t *pcm_offset,
                             nino_audio_servo_mode_t servo_mode, bool allow_interrupt) {
  if (decoded->samples == NULL || decoded->num_bytes == 0) {
    return true;
  }

  servo_motion_for_mode(servo_mode, true);
  volatile bool *stop_ptr = allow_interrupt ? &s_stop_requested : NULL;
  bool completed = false;
  esp_err_t err =
      nino_audio_play_decoded(decoded, pcm_offset, stop_ptr, &completed);
  servo_motion_for_mode(servo_mode, false);

  if (err != ESP_OK) {
    ESP_LOGW(TAG, "WAV playback failed: %s", esp_err_to_name(err));
    return true;
  }
  return completed;
}

static bool play_touch_job(audio_play_job_t *job) {
  nino_decoded_wav_t decoded = {};
  if (nino_audio_decode_wav(job->data, job->len, &decoded) != ESP_OK) {
    ESP_LOGW(TAG, "Touch WAV decode failed");
    free(job->data);
    return true;
  }
  free(job->data);
  job->data = NULL;

  s_normal_playing = true;
  size_t offset = 0;
  const nino_audio_servo_mode_t servo_mode = effective_servo_mode(job->servo_mode);
  (void)play_decoded_job(&decoded, &offset, servo_mode, false);
  s_normal_playing = false;
  nino_decoded_wav_free(&decoded);
  rewarm_wake_chime_path();
  return true;
}

static bool play_normal_job(audio_play_job_t *job) {
  /* Expression (if any) is already showing from the moment the tag arrived; keep
   * it for the whole reply and revert to idle when the clip ends. */
  const bool has_expr = (job->eye_state < NINO_EYE_STATE_COUNT);

  nino_decoded_wav_t decoded = {};
  if (nino_audio_decode_wav(job->data, job->len, &decoded) != ESP_OK) {
    ESP_LOGW(TAG, "WAV decode failed");
    free(job->data);
    if (has_expr) {
      nino_eye_idle();
    }
    return true;
  }
  free(job->data);
  job->data = NULL;

  if (has_expr) {
    nino_eye_set_state(job->eye_state);
  }

  s_normal_playing = true;
  size_t offset = 0;
  const nino_audio_servo_mode_t servo_mode = effective_servo_mode(job->servo_mode);
  const bool completed = play_decoded_job(&decoded, &offset, servo_mode, true);

  if (!completed && offset < decoded.num_bytes) {
    s_suspended.decoded = decoded;
    s_suspended.pcm_offset = offset;
    s_suspended.play_done_chime = job->play_done_chime;
    s_suspended.servo_mode = job->servo_mode;
    s_suspended.prompt_ack_after = job->prompt_ack_after;
    s_has_suspended = true;
    s_stop_requested = false;
    s_normal_playing = false;
    ESP_LOGI(TAG, "Server WAV paused at %u/%u bytes for touch",
             (unsigned)offset, (unsigned)decoded.num_bytes);
    return false;
  }

  /* TTS finished: back to idle (server contract). */
  if (has_expr) {
    nino_eye_idle();
  }

  if (job->play_done_chime) {
    (void)nino_voice_play_done_chime();
  }
  if (job->prompt_ack_after) {
    nino_voice_assist_prompt_medical_ack();
  }
  nino_decoded_wav_free(&decoded);
  s_normal_playing = false;
  rewarm_wake_chime_path();
  return true;
}

static bool play_suspended(void) {
  if (!s_has_suspended) {
    return true;
  }

  suspended_playback_t snap = s_suspended;
  s_has_suspended = false;
  memset(&s_suspended, 0, sizeof(s_suspended));

  s_normal_playing = true;
  const nino_audio_servo_mode_t servo_mode = effective_servo_mode(snap.servo_mode);
  const bool completed =
      play_decoded_job(&snap.decoded, &snap.pcm_offset, servo_mode, true);

  if (!completed && snap.pcm_offset < snap.decoded.num_bytes) {
    s_suspended = snap;
    s_has_suspended = true;
    s_stop_requested = false;
    s_normal_playing = false;
    ESP_LOGI(TAG, "Server WAV paused again at %u/%u bytes for touch",
             (unsigned)snap.pcm_offset, (unsigned)snap.decoded.num_bytes);
    return false;
  }

  if (snap.play_done_chime) {
    (void)nino_voice_play_done_chime();
  }
  if (snap.prompt_ack_after) {
    nino_voice_assist_prompt_medical_ack();
  }
  nino_decoded_wav_free(&snap.decoded);
  s_normal_playing = false;
  rewarm_wake_chime_path();
  return true;
}

static bool try_receive_touch(audio_play_job_t *job) {
  return xQueueReceive(s_touch_queue, job, 0) == pdPASS;
}

static void audio_playback_task(void *arg) {
  (void)arg;

  while (true) {
    audio_play_job_t job = {};

    if (try_receive_touch(&job)) {
      (void)play_touch_job(&job);
      continue;
    }

    if (s_has_suspended) {
      if (!play_suspended()) {
        continue;
      }
    }

    if (try_receive_touch(&job)) {
      (void)play_touch_job(&job);
      continue;
    }

    if (xQueueReceive(s_normal_queue, &job, pdMS_TO_TICKS(50)) != pdPASS) {
      continue;
    }
    if (job.data == NULL || job.len == 0) {
      free(job.data);
      continue;
    }

    (void)play_normal_job(&job);
  }
}

static esp_err_t enqueue_job(audio_play_job_t job, QueueHandle_t queue) {
  if (queue == NULL) {
    free(job.data);
    return ESP_ERR_INVALID_STATE;
  }
  if (job.data == NULL || job.len == 0) {
    free(job.data);
    return ESP_ERR_INVALID_ARG;
  }

  if (xQueueSendToBack(queue, &job, portMAX_DELAY) != pdPASS) {
    free(job.data);
    return ESP_FAIL;
  }
  return ESP_OK;
}

esp_err_t nino_audio_queue_start(void) {
  if (s_normal_queue != NULL) {
    return ESP_OK;
  }

  s_normal_queue = xQueueCreate(NORMAL_QUEUE_LEN, sizeof(audio_play_job_t));
  s_touch_queue = xQueueCreate(TOUCH_QUEUE_LEN, sizeof(audio_play_job_t));
  if (s_normal_queue == NULL || s_touch_queue == NULL) {
    if (s_normal_queue != NULL) {
      vQueueDelete(s_normal_queue);
      s_normal_queue = NULL;
    }
    if (s_touch_queue != NULL) {
      vQueueDelete(s_touch_queue);
      s_touch_queue = NULL;
    }
    ESP_LOGE(TAG, "Failed to create audio play queues");
    return ESP_ERR_NO_MEM;
  }

  BaseType_t ok =
      xTaskCreate(audio_playback_task, "audio_play", AUDIO_PLAY_TASK_STACK_SIZE,
                  NULL, AUDIO_PLAY_TASK_PRIORITY, NULL);
  if (ok != pdPASS) {
    vQueueDelete(s_normal_queue);
    vQueueDelete(s_touch_queue);
    s_normal_queue = NULL;
    s_touch_queue = NULL;
    ESP_LOGE(TAG, "Failed to create audio play task");
    return ESP_ERR_NO_MEM;
  }

  ESP_LOGI(TAG, "Audio queue ready (touch priority, server pause/resume)");
  return ESP_OK;
}

esp_err_t nino_audio_queue_wav(uint8_t *wav, size_t len, bool play_done_chime,
                               nino_audio_servo_mode_t servo_mode,
                               bool prompt_ack_after, nino_eye_state_t eye_state) {
  if (wav == NULL || len == 0) {
    free(wav);
    return ESP_ERR_INVALID_ARG;
  }

  audio_play_job_t job = {
      .data = wav,
      .len = len,
      .play_done_chime = play_done_chime,
      .servo_mode = servo_mode,
      .prompt_ack_after = prompt_ack_after,
      .eye_state = eye_state,
  };

  if (is_touch_job(&job)) {
    s_stop_requested = true;
    return enqueue_job(job, s_touch_queue);
  }
  return enqueue_job(job, s_normal_queue);
}

esp_err_t nino_audio_queue_wav_copy(const uint8_t *wav, size_t len, bool play_done_chime,
                                    nino_audio_servo_mode_t servo_mode,
                                    bool prompt_ack_after) {
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
  return nino_audio_queue_wav(copy, len, play_done_chime, servo_mode, prompt_ack_after,
                              NINO_EYE_STATE_COUNT);
}

void nino_main_queue_audio_wav(uint8_t *pcm_wav, size_t len, bool play_done_chime,
                               bool prompt_ack_after, nino_eye_state_t eye_state) {
  /* Same L/R/U/D as POST /play_wav; motion stops when clip ends (/servo/360 stops it too). */
  esp_err_t err = nino_audio_queue_wav(pcm_wav, len, play_done_chime,
                                       NINO_AUDIO_SERVO_FULL, prompt_ack_after, eye_state);
  if (err != ESP_OK) {
    ESP_LOGW(TAG, "voice: queue WAV failed: %s", esp_err_to_name(err));
  }
}

static bool audio_queue_is_idle(void) {
  if (s_normal_queue == NULL) {
    return true;
  }
  if (s_normal_playing || s_has_suspended) {
    return false;
  }
  return uxQueueMessagesWaiting(s_normal_queue) == 0;
}

void nino_audio_queue_wait_idle(uint32_t timeout_ms) {
  const TickType_t deadline = xTaskGetTickCount() + pdMS_TO_TICKS(timeout_ms);
  while (xTaskGetTickCount() < deadline) {
    if (audio_queue_is_idle()) {
      return;
    }
    vTaskDelay(pdMS_TO_TICKS(50));
  }
  ESP_LOGW(TAG, "Audio queue wait_idle timed out after %u ms", (unsigned)timeout_ms);
}

void nino_audio_queue_preempt_for_wake(void) {
  if (s_normal_queue == NULL) {
    return;
  }
  s_stop_requested = true;
  const TickType_t deadline = xTaskGetTickCount() + pdMS_TO_TICKS(600);
  while (s_normal_playing && xTaskGetTickCount() < deadline) {
    vTaskDelay(pdMS_TO_TICKS(10));
  }
}

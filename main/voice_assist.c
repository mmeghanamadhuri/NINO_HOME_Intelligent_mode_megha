#include "voice_assist.h"

#include <inttypes.h>
#include <stdlib.h>
#include <string.h>

#include "audio_capture.h"
#include "audio_playback.h"
#include "audio_queue.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"

#include "nino_eye.h"
#include "voice_ws_client.h"

extern const uint8_t beep_wav_start[] asm("_binary_beep_wav_start");
extern const uint8_t beep_wav_end[] asm("_binary_beep_wav_end");

static const char *TAG = "voice_ast";

#define VOICE_MIC_RATE 16000
#define WAV_HEADER_SIZE 44
#define VOICE_WS_URI_MAX 200
#define VOICE_QUERY_DEFAULT_MS 5000
#define VOICE_QUERY_MAX_MS 10000
#define MED_ACK_CAPTURE_MS 5000

static char s_ws_uri[VOICE_WS_URI_MAX];
static SemaphoreHandle_t s_ws_uri_mutex;
static volatile bool s_next_prompt_ack_play_chime = true;
static volatile bool s_query_busy;

void nino_voice_assist_set_next_prompt_ack_chime(bool play_chime) {
  s_next_prompt_ack_play_chime = play_chime;
}

void nino_voice_assist_set_ws_uri(const char *uri) {
  if (s_ws_uri_mutex == NULL) {
    return;
  }
  if (uri == NULL) {
    uri = "";
  }
  xSemaphoreTake(s_ws_uri_mutex, portMAX_DELAY);
  strncpy(s_ws_uri, uri, sizeof(s_ws_uri) - 1);
  s_ws_uri[sizeof(s_ws_uri) - 1] = '\0';
  xSemaphoreGive(s_ws_uri_mutex);
}

static int16_t *s_beep_pcm16 = NULL;
static size_t s_beep_pcm16_samples = 0;

static esp_err_t ensure_beep_pcm16(void) {
  if (s_beep_pcm16 != NULL && s_beep_pcm16_samples > 0) {
    return ESP_OK;
  }

  const size_t wav_len = (size_t)(beep_wav_end - beep_wav_start);
  if (wav_len < WAV_HEADER_SIZE) {
    ESP_LOGE(TAG, "embedded beep.wav missing or too small");
    return ESP_ERR_INVALID_SIZE;
  }

  nino_decoded_wav_t dec = {};
  esp_err_t e = nino_audio_decode_wav(beep_wav_start, wav_len, &dec);
  if (e != ESP_OK) {
    return e;
  }

  const size_t in_frames = dec.num_bytes / sizeof(int16_t);
  if (in_frames == 0) {
    nino_decoded_wav_free(&dec);
    return ESP_ERR_INVALID_SIZE;
  }

  if (dec.sample_rate_hz == (uint32_t)VOICE_MIC_RATE) {
    s_beep_pcm16 = (int16_t *)malloc(dec.num_bytes);
    if (s_beep_pcm16 == NULL) {
      nino_decoded_wav_free(&dec);
      return ESP_ERR_NO_MEM;
    }
    memcpy(s_beep_pcm16, dec.samples, dec.num_bytes);
    s_beep_pcm16_samples = in_frames;
    nino_decoded_wav_free(&dec);
    return ESP_OK;
  }

  const size_t out_frames =
      (in_frames * (size_t)VOICE_MIC_RATE) / dec.sample_rate_hz + 2U;
  s_beep_pcm16 = (int16_t *)calloc(out_frames, sizeof(int16_t));
  if (s_beep_pcm16 == NULL) {
    nino_decoded_wav_free(&dec);
    return ESP_ERR_NO_MEM;
  }

  size_t produced = 0;
  for (size_t o = 0; o < out_frames; o++) {
    const size_t src = (o * dec.sample_rate_hz) / (uint32_t)VOICE_MIC_RATE;
    if (src >= in_frames) {
      break;
    }
    s_beep_pcm16[o] = dec.samples[src];
    produced = o + 1U;
  }
  s_beep_pcm16_samples = produced;
  nino_decoded_wav_free(&dec);

  ESP_LOGI(TAG, "beep.wav cached: %u samples @ %d Hz",
           (unsigned)s_beep_pcm16_samples, VOICE_MIC_RATE);
  return ESP_OK;
}

static esp_err_t play_embedded_beep(void) {
  esp_err_t e = ensure_beep_pcm16();
  if (e != ESP_OK) {
    return e;
  }
  return nino_audio_play_chime_pcm16_mono(s_beep_pcm16, s_beep_pcm16_samples,
                                          (uint32_t)VOICE_MIC_RATE);
}

esp_err_t nino_voice_preload_wake_chime(void) {
  esp_err_t e = ensure_beep_pcm16();
  if (e != ESP_OK) {
    return e;
  }
  return nino_audio_warm_chime_path((uint32_t)VOICE_MIC_RATE);
}

esp_err_t nino_voice_play_wake_chime(void) {
  nino_audio_queue_preempt_for_wake();
  return play_embedded_beep();
}

esp_err_t nino_voice_play_done_chime(void) { return play_embedded_beep(); }

esp_err_t nino_voice_assist_init_mutex(void) {
  if (s_ws_uri_mutex == NULL) {
    s_ws_uri_mutex = xSemaphoreCreateMutex();
    if (s_ws_uri_mutex == NULL) {
      return ESP_ERR_NO_MEM;
    }
  }
  return ESP_OK;
}

#define VOICE_WS_JOB_STACK 20480

typedef struct {
  uint8_t *cap;
  size_t cap_len;
  char uri[VOICE_WS_URI_MAX];
  bool medical_ack_session;
} voice_ws_job_t;

static void voice_ws_job_task(void *pv) {
  voice_ws_job_t *job = (voice_ws_job_t *)pv;
  if (job == NULL) {
    s_query_busy = false;
    vTaskDelete(NULL);
    return;
  }

  uint8_t *resp = NULL;
  size_t resp_len = 0;
  bool prompt_after = false;
  char eye_expr[16] = {0};
  const int64_t t_ws = esp_timer_get_time();
  esp_err_t e = nino_voice_ws_exchange(job->uri, job->cap, job->cap_len, &resp,
                                       &resp_len, 300000, &prompt_after,
                                       eye_expr, sizeof(eye_expr));
  ESP_LOGI(TAG, "latency: WS round-trip %" PRId64 " ms",
           (esp_timer_get_time() - t_ws) / 1000LL);
  nino_audio_capture_free(job->cap);
  const bool medical_ack = job->medical_ack_session;
  free(job);

  if (e != ESP_OK || resp == NULL || resp_len == 0) {
    ESP_LOGE(TAG, "WS exchange failed: %s", esp_err_to_name(e));
    free(resp);
    nino_eye_idle();
    s_query_busy = false;
    vTaskDelete(NULL);
    return;
  }

  nino_eye_state_t eye_state = nino_eye_state_from_name(eye_expr);
  if (eye_state < NINO_EYE_STATE_COUNT) {
    ESP_LOGI(TAG, "Reply eye_expression=%s -> state %d", eye_expr, (int)eye_state);
  }
  const bool play_done_chime = !prompt_after;
  nino_main_queue_audio_wav(resp, resp_len, play_done_chime, prompt_after, eye_state);
  if (medical_ack && prompt_after) {
    ESP_LOGI(TAG, "Medical follow-up listen scheduled after reply");
  }
  if (prompt_after) {
    ESP_LOGI(TAG, "Prompt-ack listen scheduled after reply");
  }
  s_query_busy = false;
  vTaskDelete(NULL);
}

static esp_err_t spawn_voice_ws_job(uint8_t *cap, size_t cap_len, const char *uri,
                                    bool medical_ack_session) {
  voice_ws_job_t *job = (voice_ws_job_t *)malloc(sizeof(voice_ws_job_t));
  if (job == NULL) {
    nino_audio_capture_free(cap);
    s_query_busy = false;
    return ESP_ERR_NO_MEM;
  }
  job->cap = cap;
  job->cap_len = cap_len;
  strncpy(job->uri, uri, sizeof(job->uri) - 1);
  job->uri[sizeof(job->uri) - 1] = '\0';
  job->medical_ack_session = medical_ack_session;

  BaseType_t ok =
      xTaskCreate(voice_ws_job_task, "voice_ws", VOICE_WS_JOB_STACK, job, 3, NULL);
  if (ok != pdPASS) {
    free(job);
    nino_audio_capture_free(cap);
    s_query_busy = false;
    ESP_LOGE(TAG, "Could not start voice_ws task");
    return ESP_ERR_NO_MEM;
  }
  ESP_LOGI(TAG, "Voice query running in background");
  return ESP_OK;
}

static esp_err_t run_ws_and_queue(uint32_t duration_ms, bool medical_ack_session) {
  char uri[VOICE_WS_URI_MAX];
  if (s_ws_uri_mutex == NULL) {
    return ESP_ERR_INVALID_STATE;
  }
  xSemaphoreTake(s_ws_uri_mutex, portMAX_DELAY);
  strncpy(uri, s_ws_uri, sizeof(uri) - 1);
  uri[sizeof(uri) - 1] = '\0';
  xSemaphoreGive(s_ws_uri_mutex);

  if (uri[0] == '\0') {
    ESP_LOGW(TAG, "WS URI not set — voice connect <PC_LAN_IP> 8000");
    nino_eye_idle();
    return ESP_ERR_INVALID_STATE;
  }
  if (s_query_busy) {
    ESP_LOGW(TAG, "Voice query already running");
    return ESP_ERR_INVALID_STATE;
  }
  if (duration_ms == 0 || duration_ms > VOICE_QUERY_MAX_MS) {
    return ESP_ERR_INVALID_ARG;
  }

  s_query_busy = true;
  nino_eye_listening();
  const int64_t t_query = esp_timer_get_time();

  uint8_t *cap = NULL;
  size_t cap_len = 0;
  esp_err_t e = nino_audio_capture_wav(&cap, &cap_len, duration_ms);
  ESP_LOGI(TAG, "latency: AUX capture %" PRId64 " ms (%u ms requested)",
           (esp_timer_get_time() - t_query) / 1000LL, (unsigned)duration_ms);
  if (e != ESP_OK) {
    ESP_LOGE(TAG, "AUX capture failed: %s", esp_err_to_name(e));
    nino_eye_idle();
    s_query_busy = false;
    return e;
  }

  nino_eye_thinking();
  return spawn_voice_ws_job(cap, cap_len, uri, medical_ack_session);
}

esp_err_t nino_voice_assist_run_query(uint32_t duration_ms) {
  return run_ws_and_queue(duration_ms, false);
}

esp_err_t nino_voice_assist_run_query_only(void) {
  return run_ws_and_queue(VOICE_QUERY_DEFAULT_MS, false);
}

#define MED_ACK_TASK_STACK 20480
#define PROMPT_ACK_POST_PLAY_MS 900

static void prompt_listen_task(void *arg) {
  (void)arg;
  const bool play_chime = s_next_prompt_ack_play_chime;
  s_next_prompt_ack_play_chime = true;

  vTaskDelay(pdMS_TO_TICKS(PROMPT_ACK_POST_PLAY_MS));
  if (!nino_voice_assist_has_ws_uri()) {
    ESP_LOGW(TAG,
             "Prompt listen: PC voice not linked — need voice connect or "
             "X-Nino-Voice-Ws-Url from server");
    vTaskDelete(NULL);
    return;
  }
  ESP_LOGI(TAG, "Prompt listen — AUX IN %d ms (chime=%d)", MED_ACK_CAPTURE_MS,
           (int)play_chime);
  if (play_chime) {
    esp_err_t chime = nino_voice_play_wake_chime();
    if (chime != ESP_OK) {
      ESP_LOGW(TAG, "Prompt listen chime failed: %s", esp_err_to_name(chime));
    }
    vTaskDelay(pdMS_TO_TICKS(350));
  }
  esp_err_t e = run_ws_and_queue(MED_ACK_CAPTURE_MS, false);
  if (e != ESP_OK) {
    ESP_LOGW(TAG, "Prompt listen voice query failed: %s", esp_err_to_name(e));
  }
  vTaskDelete(NULL);
}

void nino_voice_assist_prompt_medical_ack(void) {
  BaseType_t ok =
      xTaskCreate(prompt_listen_task, "prompt_listen", MED_ACK_TASK_STACK, NULL, 3, NULL);
  if (ok != pdPASS) {
    ESP_LOGW(TAG, "Could not start prompt listen task");
  }
}

bool nino_voice_assist_has_ws_uri(void) {
  if (s_ws_uri_mutex == NULL) {
    return false;
  }
  bool ok = false;
  xSemaphoreTake(s_ws_uri_mutex, portMAX_DELAY);
  ok = (s_ws_uri[0] != '\0');
  xSemaphoreGive(s_ws_uri_mutex);
  return ok;
}

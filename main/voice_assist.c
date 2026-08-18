#include "voice_assist.h"

#include <inttypes.h>
#include <stdio.h>
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

#include "mic_input.h"
#include "nino_eye.h"
#include "rgb_led.h"
#include "voice_ws_client.h"

extern const uint8_t beep_wav_start[] asm("_binary_beep_wav_start");
extern const uint8_t beep_wav_end[] asm("_binary_beep_wav_end");

static const char *TAG = "voice_ast";

#define VOICE_MIC_RATE 16000
#define WAV_HEADER_SIZE 44
#define VOICE_WS_URI_MAX 240
#define VOICE_QUERY_DEFAULT_MS 5000
#define VOICE_QUERY_MAX_MS 10000
#define MED_ACK_CAPTURE_MS 5000
#define LISTEN_LOOP_STACK 12288
#define AUX_DETECT_FRAME_MS 20
#define AUX_DETECT_SAMPLES ((VOICE_MIC_RATE * AUX_DETECT_FRAME_MS) / 1000)
#define AUX_NOISE_FLOOR_DEFAULT 250
#define AUX_MIN_START_ENERGY 400
#define AUX_NOISE_RATIO 4U
#define AUX_START_CONSECUTIVE_FRAMES 8
#define AUX_QUIET_MS 800
#define AUX_QUIET_MAX_MS 4000
#define AUX_REARM_DELAY_MS 1500
#define AUX_STATUS_LOG_MS 1000
#define AUX_REPLY_WAIT_MS 180000
#define AUX_PREROLL_MS 500
#define AUX_PREROLL_FRAMES (AUX_PREROLL_MS / AUX_DETECT_FRAME_MS)
#define AUX_WAKE_GAP_MS 1000
#define AUX_QUESTION_WAIT_MS 4000
#define AUX_CAPTURE_MAX_MS 8000
#define AUX_SENTENCE_QUIET_MS 800
#define AUX_CONTINUE_MIN_MS 800

typedef enum {
  VOICE_SESSION_WAKE = 0,
  VOICE_SESSION_CONTINUE,
} voice_session_kind_t;

static char s_ws_uri[VOICE_WS_URI_MAX];
static SemaphoreHandle_t s_ws_uri_mutex;
static volatile bool s_next_prompt_ack_play_chime = true;
static volatile bool s_query_busy;
static volatile bool s_listen_loop_started;
static volatile bool s_aux_listen_running;
static uint32_t s_aux_noise_floor = AUX_NOISE_FLOOR_DEFAULT;
static int16_t s_preroll_ring[AUX_PREROLL_FRAMES][AUX_DETECT_SAMPLES];
static size_t s_preroll_head;
static size_t s_preroll_filled;
static int16_t s_preroll_flat[AUX_PREROLL_FRAMES * AUX_DETECT_SAMPLES];

static const char *session_query_name(voice_session_kind_t kind) {
  return kind == VOICE_SESSION_CONTINUE ? "continue" : "wake";
}

static void preroll_push(const int16_t *frame) {
  memcpy(s_preroll_ring[s_preroll_head], frame,
         AUX_DETECT_SAMPLES * sizeof(int16_t));
  s_preroll_head = (s_preroll_head + 1U) % AUX_PREROLL_FRAMES;
  if (s_preroll_filled < AUX_PREROLL_FRAMES) {
    s_preroll_filled++;
  }
}

static size_t preroll_flatten(int16_t *out, size_t out_samples) {
  const size_t frames = s_preroll_filled;
  if (frames == 0 || out == NULL || out_samples == 0) {
    return 0;
  }
  const size_t start =
      (s_preroll_head + AUX_PREROLL_FRAMES - frames) % AUX_PREROLL_FRAMES;
  size_t written = 0;
  for (size_t i = 0; i < frames; i++) {
    if (written + AUX_DETECT_SAMPLES > out_samples) {
      break;
    }
    const size_t idx = (start + i) % AUX_PREROLL_FRAMES;
    memcpy(out + written, s_preroll_ring[idx],
           AUX_DETECT_SAMPLES * sizeof(int16_t));
    written += AUX_DETECT_SAMPLES;
  }
  return written;
}

static void uri_append_session(char *uri, size_t uri_sz, voice_session_kind_t kind) {
  const char *sep = strchr(uri, '?') != NULL ? "&" : "?";
  const size_t used = strlen(uri);
  if (used >= uri_sz) {
    return;
  }
  snprintf(uri + used, uri_sz - used, "%ssession=%s", sep, session_query_name(kind));
}

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

static uint32_t aux_quiet_threshold(uint32_t noise_floor);

static uint32_t aux_start_threshold(uint32_t noise_floor);

static esp_err_t run_ws_and_queue_ex(
    voice_session_kind_t session, const int16_t *preroll, size_t preroll_samples,
    uint32_t min_ms, uint32_t max_ms, uint32_t quiet_end_ms,
    uint32_t quiet_energy, uint32_t speech_energy, uint32_t wait_speech_ms,
    bool flush_first, bool medical_ack_session);

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
    ESP_LOGE(TAG, "VOICE_FAIL WS exchange: %s — RGB error", esp_err_to_name(e));
    free(resp);
    nino_eye_idle();
    (void)nino_rgb_led_show(NINO_RGB_SHOW_ERROR);
    s_query_busy = false;
    vTaskDelete(NULL);
    return;
  }

  nino_eye_state_t eye_state = nino_eye_state_from_name(eye_expr);
  if (eye_state < NINO_EYE_STATE_COUNT) {
    ESP_LOGI(TAG, "Reply eye_expression=%s -> state %d", eye_expr, (int)eye_state);
  }
  const bool play_done_chime = false;
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
  return run_ws_and_queue_ex(VOICE_SESSION_WAKE, NULL, 0, duration_ms, duration_ms,
                             0, 0, 0, 0, true, medical_ack_session);
}

static esp_err_t run_ws_and_queue_ex(
    voice_session_kind_t session, const int16_t *preroll, size_t preroll_samples,
    uint32_t min_ms, uint32_t max_ms, uint32_t quiet_end_ms,
    uint32_t quiet_energy, uint32_t speech_energy, uint32_t wait_speech_ms,
    bool flush_first, bool medical_ack_session) {
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
  if (max_ms == 0 || max_ms > VOICE_QUERY_MAX_MS) {
    return ESP_ERR_INVALID_ARG;
  }

  uri_append_session(uri, sizeof(uri), session);
  s_query_busy = true;
  nino_eye_listening();
  (void)nino_rgb_led_show(NINO_RGB_SHOW_LISTEN);
  const int64_t t_query = esp_timer_get_time();

  uint8_t *cap = NULL;
  size_t cap_len = 0;
  esp_err_t e;
  if (quiet_end_ms > 0) {
    ESP_LOGI(TAG,
             "========== NINO %s ==========\n"
             "  uri      %s\n"
             "  preroll  %u samples\n"
             "  gap      %u ms\n"
             "  wait_q   %u ms\n"
             "  max      %u ms",
             session == VOICE_SESSION_WAKE ? "WAKE" : "CONV", uri,
             (unsigned)preroll_samples, (unsigned)min_ms,
             (unsigned)wait_speech_ms, (unsigned)max_ms);
    e = nino_audio_capture_wav_until_quiet(
        &cap, &cap_len, preroll, preroll_samples, min_ms, max_ms, quiet_end_ms,
        quiet_energy, speech_energy, wait_speech_ms, flush_first);
  } else {
    e = nino_audio_capture_wav(&cap, &cap_len, max_ms);
  }
  ESP_LOGI(TAG, "latency: AUX capture %" PRId64 " ms (session=%s)",
           (esp_timer_get_time() - t_query) / 1000LL, session_query_name(session));
  if (e != ESP_OK) {
    ESP_LOGE(TAG, "VOICE_FAIL AUX capture: %s — RGB error", esp_err_to_name(e));
    nino_eye_idle();
    (void)nino_rgb_led_show(NINO_RGB_SHOW_ERROR);
    s_query_busy = false;
    return e;
  }

  /* Listen is only while the mic is open. Go idle while the server thinks. */
  (void)nino_rgb_led_show(NINO_RGB_SHOW_IDLE);
  ESP_LOGI(TAG, "UPLOAD session=%s bytes=%u — listen LED off, waiting for server",
           session_query_name(session), (unsigned)cap_len);
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
  ESP_LOGI(TAG, "CONV_LISTEN — no wake required, VAD until sentence end (chime=%d)",
           (int)play_chime);
  (void)nino_rgb_led_show(NINO_RGB_SHOW_LISTEN);
  if (play_chime) {
    esp_err_t chime = nino_voice_play_wake_chime();
    if (chime != ESP_OK) {
      ESP_LOGW(TAG, "Prompt listen chime failed: %s", esp_err_to_name(chime));
    }
    vTaskDelay(pdMS_TO_TICKS(350));
  }
  esp_err_t e = run_ws_and_queue_ex(
      VOICE_SESSION_CONTINUE, NULL, 0, AUX_CONTINUE_MIN_MS, AUX_CAPTURE_MAX_MS,
      AUX_SENTENCE_QUIET_MS, aux_quiet_threshold(s_aux_noise_floor),
      aux_start_threshold(s_aux_noise_floor), 0, true, false);
  if (e != ESP_OK) {
    ESP_LOGW(TAG, "CONV_LISTEN voice query failed: %s", esp_err_to_name(e));
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

static uint32_t aux_frame_energy(const int16_t *samples, size_t count) {
  uint64_t sum = 0;
  for (size_t i = 0; i < count; i++) {
    int32_t s = samples[i];
    sum += (uint32_t)(s < 0 ? -s : s);
  }
  return count == 0 ? 0 : (uint32_t)(sum / count);
}

static uint32_t aux_start_threshold(uint32_t noise_floor) {
  const uint32_t from_noise = noise_floor * AUX_NOISE_RATIO;
  return from_noise > (uint32_t)AUX_MIN_START_ENERGY ? from_noise
                                                     : (uint32_t)AUX_MIN_START_ENERGY;
}

static uint32_t aux_quiet_threshold(uint32_t noise_floor) {
  const uint32_t from_noise = noise_floor + noise_floor / 2U;
  return from_noise > 200U ? from_noise : 200U;
}

static void aux_update_noise_floor(uint32_t energy) {
  s_aux_noise_floor = (s_aux_noise_floor * 31U + energy) / 32U;
  if (s_aux_noise_floor < 1U) {
    s_aux_noise_floor = 1U;
  }
}

static bool wait_aux_activity(void) {
  int16_t frame[AUX_DETECT_SAMPLES];
  uint32_t speech_streak = 0;
  uint32_t status_ms = 0;

  while (true) {
    if (s_query_busy) {
      vTaskDelay(pdMS_TO_TICKS(20));
      speech_streak = 0;
      continue;
    }

    esp_err_t rr = nino_mic_read(frame, AUX_DETECT_SAMPLES);
    if (rr != ESP_OK) {
      ESP_LOGW(TAG, "Aux-in listen read failed: %s", esp_err_to_name(rr));
      vTaskDelay(pdMS_TO_TICKS(20));
      speech_streak = 0;
      continue;
    }

    preroll_push(frame);
    const uint32_t energy = aux_frame_energy(frame, AUX_DETECT_SAMPLES);
    const uint32_t threshold = aux_start_threshold(s_aux_noise_floor);
    status_ms += AUX_DETECT_FRAME_MS;
    if (status_ms >= AUX_STATUS_LOG_MS) {
      ESP_LOGI(TAG, "Aux-in listen energy=%" PRIu32 " noise=%" PRIu32 " th=%" PRIu32,
               energy, s_aux_noise_floor, threshold);
      status_ms = 0;
    }

    if (energy >= threshold) {
      speech_streak++;
    } else {
      speech_streak = 0;
      aux_update_noise_floor(energy);
    }
    if (speech_streak >= AUX_START_CONSECUTIVE_FRAMES) {
      ESP_LOGI(TAG, "Aux-in activity energy=%" PRIu32 " noise=%" PRIu32 " th=%" PRIu32,
               energy, s_aux_noise_floor, threshold);
      return true;
    }
  }
}

static void wait_aux_quiet(void) {
  int16_t frame[AUX_DETECT_SAMPLES];
  uint32_t quiet_ms = 0;
  uint32_t waited_ms = 0;
  uint32_t last_energy = s_aux_noise_floor;

  while (waited_ms < AUX_QUIET_MAX_MS) {
    if (s_query_busy) {
      return;
    }
    esp_err_t rr = nino_mic_read(frame, AUX_DETECT_SAMPLES);
    if (rr != ESP_OK) {
      return;
    }
    last_energy = aux_frame_energy(frame, AUX_DETECT_SAMPLES);
    waited_ms += AUX_DETECT_FRAME_MS;
    if (last_energy < aux_quiet_threshold(s_aux_noise_floor)) {
      quiet_ms += AUX_DETECT_FRAME_MS;
      aux_update_noise_floor(last_energy);
      if (quiet_ms >= AUX_QUIET_MS) {
        return;
      }
    } else {
      quiet_ms = 0;
    }
  }
  aux_update_noise_floor(last_energy);
  ESP_LOGW(TAG, "Aux-in still active after wait; noise floor now %" PRIu32, s_aux_noise_floor);
}

static void wait_query_and_reply_done(void) {
  const TickType_t deadline = xTaskGetTickCount() + pdMS_TO_TICKS(AUX_REPLY_WAIT_MS);
  while (s_query_busy && xTaskGetTickCount() < deadline) {
    vTaskDelay(pdMS_TO_TICKS(50));
  }
  nino_audio_queue_wait_idle(AUX_REPLY_WAIT_MS);
}

static void aux_listen_task(void *arg) {
  (void)arg;

  while (!nino_mic_available()) {
    vTaskDelay(pdMS_TO_TICKS(500));
  }

  ESP_LOGI(TAG,
           "Aux-in listen armed — Sirena wake energy, 500 ms preroll + 1 s gap, "
           "then VAD until sentence end");
  s_aux_listen_running = true;

  while (true) {
    if (!nino_voice_assist_has_ws_uri()) {
      vTaskDelay(pdMS_TO_TICKS(500));
      continue;
    }
    if (!wait_aux_activity()) {
      continue;
    }
    if (s_query_busy) {
      continue;
    }

    const size_t preroll_n =
        preroll_flatten(s_preroll_flat, AUX_PREROLL_FRAMES * AUX_DETECT_SAMPLES);
    ESP_LOGI(TAG,
             "WAKE_ENERGY preroll=%u ms — green listen, 1 s gap, then wait up to "
             "%u ms for the question (session=wake)",
             (unsigned)(preroll_n * 1000U / (size_t)VOICE_MIC_RATE),
             (unsigned)AUX_QUESTION_WAIT_MS);
    nino_eye_listening();
    (void)nino_rgb_led_show(NINO_RGB_SHOW_LISTEN);
    /* Keep the mic open: gap records the wake tail, then we wait for the question. */
    esp_err_t e = run_ws_and_queue_ex(
        VOICE_SESSION_WAKE, s_preroll_flat, preroll_n, AUX_WAKE_GAP_MS,
        AUX_CAPTURE_MAX_MS, AUX_SENTENCE_QUIET_MS,
        aux_quiet_threshold(s_aux_noise_floor),
        aux_start_threshold(s_aux_noise_floor), AUX_QUESTION_WAIT_MS, false,
        false);
    if (e != ESP_OK) {
      ESP_LOGW(TAG, "WAKE query failed: %s", esp_err_to_name(e));
      nino_eye_idle();
      (void)nino_rgb_led_show(NINO_RGB_SHOW_ERROR);
    } else {
      wait_query_and_reply_done();
    }

    wait_aux_quiet();
    vTaskDelay(pdMS_TO_TICKS(AUX_REARM_DELAY_MS));
    if (!s_query_busy) {
      (void)nino_rgb_led_show(NINO_RGB_SHOW_IDLE);
    }
    ESP_LOGI(TAG, "Aux-in listen re-armed (noise=%" PRIu32 " th=%" PRIu32 ")",
             s_aux_noise_floor, aux_start_threshold(s_aux_noise_floor));
  }
}

bool nino_voice_assist_aux_listen_is_running(void) {
  return s_aux_listen_running && !s_query_busy;
}

void nino_voice_assist_start_listen_loop(void) {
  if (s_listen_loop_started) {
    return;
  }
  s_listen_loop_started = true;
  BaseType_t ok =
      xTaskCreate(aux_listen_task, "aux_listen", LISTEN_LOOP_STACK, NULL, 3, NULL);
  if (ok != pdPASS) {
    s_listen_loop_started = false;
    ESP_LOGE(TAG, "Could not start Aux-in listen task");
  }
}

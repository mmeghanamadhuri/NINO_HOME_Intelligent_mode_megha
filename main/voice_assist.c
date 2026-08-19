#include "voice_assist.h"

#include <inttypes.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "audio_capture.h"
#include "audio_playback.h"
#include "audio_queue.h"
#include "esp_http_client.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"

#include "mic_input.h"
#include "music_stream.h"
#include "nino_eye.h"
#include "rgb_led.h"
#include "camera_stream.h"
#include "face_tracker.h"
#include "servo_recplay.h"
#include "voice_ws_client.h"
#include "wifi_config.h"

#include "driver/gpio.h"
#include "esp_random.h"

extern const uint8_t beep_wav_start[] asm("_binary_beep_wav_start");
extern const uint8_t beep_wav_end[] asm("_binary_beep_wav_end");

static const char *TAG = "voice_ast";

#define VOICE_MIC_RATE 16000
#define WAV_HEADER_SIZE 44
#define VOICE_WS_URI_MAX 360
#define RECORDINGS_URL_MAX 320
#define VOICE_QUERY_DEFAULT_MS 5000
#define VOICE_QUERY_MAX_MS 10000
#define MED_ACK_CAPTURE_MS 5000
#define LISTEN_LOOP_STACK 16384
#define AUX_DETECT_FRAME_MS 20
#define AUX_DETECT_SAMPLES ((VOICE_MIC_RATE * AUX_DETECT_FRAME_MS) / 1000)
#define AUX_NOISE_FLOOR_DEFAULT 250
#define AUX_MIN_START_ENERGY 50
#define AUX_NOISE_RATIO 4U
#define AUX_MIN_QUIET_ENERGY 200
#define AUX_MIN_UPLOAD_ENERGY 50
#define AUX_START_CONSECUTIVE_FRAMES 8
#define AUX_QUIET_MS 800
#define AUX_QUIET_MAX_MS 4000
#define AUX_REARM_DELAY_MS 1500
#define AUX_POST_SPEAKER_IGNORE_MS 1500
#define AUX_STATUS_LOG_MS 1000
#define AUX_REPLY_WAIT_MS 180000
#define STREAM_LISTEN_CAP_MS 65000
#define CAMERA_STREAM_WAIT_MS 2500
#define FACE_HUNT_MS 5000
#define SESSION_GREET_WAIT_MS 15000
#define AUX_MUSIC_LISTEN_MS 200
#define AUX_MUSIC_PLAY_SLICE_MS 800
#define AUX_WAKE_GAP_MS 1000
#define AUX_QUESTION_WAIT_MS 4000
#define AUX_CAPTURE_MAX_MS 8000
#define AUX_SENTENCE_QUIET_MS 800
#define AUX_CONTINUE_MIN_MS 800
#define SIRENA_MIC_CLOSE_GPIO GPIO_NUM_5
#define SIRENA_MIC_CLOSE_HOLD_MS 1500

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
static volatile bool s_force_session;
static uint32_t s_aux_noise_floor = AUX_NOISE_FLOOR_DEFAULT;
static uint32_t s_voice_turn;
static bool s_voice_turn_ready;
static int64_t s_aux_ignore_until_us;

static const char *session_query_name(voice_session_kind_t kind) {
  return kind == VOICE_SESSION_CONTINUE ? "continue" : "wake";
}

static uint32_t voice_begin_turn(void) {
  s_voice_turn++;
  if (s_voice_turn == 0) {
    s_voice_turn = 1;
  }
  s_voice_turn_ready = true;
  return s_voice_turn;
}

static uint32_t voice_take_turn(void) {
  if (!s_voice_turn_ready || s_voice_turn == 0) {
    (void)voice_begin_turn();
  }
  s_voice_turn_ready = false;
  return s_voice_turn;
}

static void voice_log(esp_log_level_t level, uint32_t turn, const char *stage,
                      const char *fmt, ...) {
  char detail[200];
  detail[0] = '\0';
  if (fmt != NULL && fmt[0] != '\0') {
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(detail, sizeof(detail), fmt, ap);
    va_end(ap);
  }
  if (detail[0] != '\0') {
    ESP_LOG_LEVEL(level, TAG, "NINO VOICE | turn=%" PRIu32 " | %-8s | %s", turn,
                  stage, detail);
  } else {
    ESP_LOG_LEVEL(level, TAG, "NINO VOICE | turn=%" PRIu32 " | %-8s |", turn, stage);
  }
}

static void uri_append_query(char *uri, size_t uri_sz, const char *key,
                             const char *value) {
  if (uri == NULL || key == NULL || value == NULL || uri_sz == 0) {
    return;
  }
  const char *sep = strchr(uri, '?') != NULL ? "&" : "?";
  const size_t used = strlen(uri);
  if (used >= uri_sz) {
    return;
  }
  snprintf(uri + used, uri_sz - used, "%s%s=%s", sep, key, value);
}

static void uri_append_session(char *uri, size_t uri_sz, voice_session_kind_t kind) {
  uri_append_query(uri, uri_sz, "session", session_query_name(kind));
}

static void uri_append_u32(char *uri, size_t uri_sz, const char *key, uint32_t value) {
  char buf[16];
  snprintf(buf, sizeof(buf), "%" PRIu32, value);
  uri_append_query(uri, uri_sz, key, buf);
}

static void sirena_gpio_init(void) {
  gpio_config_t io = {
      .pin_bit_mask = 1ULL << SIRENA_MIC_CLOSE_GPIO,
      .mode = GPIO_MODE_OUTPUT,
      .pull_up_en = GPIO_PULLUP_DISABLE,
      .pull_down_en = GPIO_PULLDOWN_DISABLE,
      .intr_type = GPIO_INTR_DISABLE,
  };
  gpio_config(&io);
  gpio_set_level(SIRENA_MIC_CLOSE_GPIO, 0);
}

static void sirena_mics_open(void) {
  gpio_set_level(SIRENA_MIC_CLOSE_GPIO, 0);
}

static void sirena_mics_close_pulse(void) {
  gpio_set_level(SIRENA_MIC_CLOSE_GPIO, 1);
  voice_log(ESP_LOG_INFO, s_voice_turn, "GPIO5", "high — Sirena close mics");
  vTaskDelay(pdMS_TO_TICKS(SIRENA_MIC_CLOSE_HOLD_MS));
  gpio_set_level(SIRENA_MIC_CLOSE_GPIO, 0);
}

static void make_session_id(char *out, size_t n) {
  snprintf(out, n, "%08x%08x%08x", (unsigned)esp_random(), (unsigned)esp_random(),
           (unsigned)(esp_timer_get_time() & 0xffffffffu));
}

static bool recordings_url_from_ws(const char *ws_uri, char *dst, size_t dst_size) {
  if (ws_uri == NULL || dst == NULL || dst_size < 16) {
    return false;
  }
  const char *sep = strstr(ws_uri, "://");
  if (sep == NULL) {
    return false;
  }
  const char *http = "http";
  if (strncmp(ws_uri, "wss://", 6) == 0 || strncmp(ws_uri, "https://", 8) == 0) {
    http = "https";
  } else if (strncmp(ws_uri, "ws://", 5) != 0 && strncmp(ws_uri, "http://", 7) != 0) {
    return false;
  }
  const char *authority = sep + 3;
  const size_t authority_len = strcspn(authority, "/?#");
  if (authority_len == 0 || authority_len > 180) {
    return false;
  }
  const char *query = strchr(authority, '?');
  int n;
  if (query != NULL) {
    n = snprintf(dst, dst_size, "%s://%.*s/recordings%s", http, (int)authority_len,
                 authority, query);
  } else {
    n = snprintf(dst, dst_size, "%s://%.*s/recordings", http, (int)authority_len,
                 authority);
  }
  return n > 0 && (size_t)n < dst_size;
}

static esp_err_t post_aux_recording(const char *ws_uri, const uint8_t *wav,
                                    size_t wav_len) {
  char url[RECORDINGS_URL_MAX];
  if (wav == NULL || wav_len == 0 || !recordings_url_from_ws(ws_uri, url, sizeof(url))) {
    return ESP_ERR_INVALID_ARG;
  }
  esp_http_client_config_t cfg = {
      .url = url,
      .method = HTTP_METHOD_POST,
      .timeout_ms = 8000,
  };
  esp_http_client_handle_t client = esp_http_client_init(&cfg);
  if (client == NULL) {
    return ESP_ERR_NO_MEM;
  }
  esp_http_client_set_header(client, "Content-Type", "audio/wav");
  esp_http_client_set_post_field(client, (const char *)wav, (int)wav_len);
  esp_err_t err = esp_http_client_perform(client);
  const int status = esp_http_client_get_status_code(client);
  esp_http_client_cleanup(client);
  if (err != ESP_OK) {
    return err;
  }
  if (status < 200 || status >= 300) {
    ESP_LOGW(TAG, "POST /recordings HTTP %d", status);
    return ESP_FAIL;
  }
  return ESP_OK;
}

void nino_voice_assist_set_next_prompt_ack_chime(bool play_chime) {
  s_next_prompt_ack_play_chime = play_chime;
}

bool nino_voice_assist_query_is_busy(void) { return s_query_busy; }

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

static uint32_t wav_peak_frame_energy(const uint8_t *wav, size_t len);

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
  uint32_t turn;
} voice_ws_job_t;

static void voice_ws_job_task(void *pv) {
  voice_ws_job_t *job = (voice_ws_job_t *)pv;
  if (job == NULL) {
    s_query_busy = false;
    vTaskDelete(NULL);
    return;
  }

  const uint32_t turn = job->turn;
  uint8_t *resp = NULL;
  size_t resp_len = 0;
  bool prompt_after = false;
  char eye_expr[16] = {0};
  const int64_t t_ws = esp_timer_get_time();
  voice_log(ESP_LOG_INFO, turn, "WAIT_PC", "uploading %u bytes",
            (unsigned)job->cap_len);
  esp_err_t e = nino_voice_ws_exchange(job->uri, job->cap, job->cap_len, &resp,
                                       &resp_len, 300000, &prompt_after,
                                       eye_expr, sizeof(eye_expr));
  const int64_t ws_ms = (esp_timer_get_time() - t_ws) / 1000LL;
  nino_audio_capture_free(job->cap);
  const bool medical_ack = job->medical_ack_session;
  free(job);

  if (e != ESP_OK || resp == NULL || resp_len == 0) {
    voice_log(ESP_LOG_ERROR, turn, "FAIL", "stage=ws err=%s led=red",
              esp_err_to_name(e));
    free(resp);
    nino_eye_idle();
    (void)nino_rgb_led_show(NINO_RGB_SHOW_ERROR);
    nino_music_pause_for_speech(false);
    s_query_busy = false;
    vTaskDelete(NULL);
    return;
  }

  nino_eye_state_t eye_state = nino_eye_state_from_name(eye_expr);
  const bool play_done_chime = false;
  nino_main_queue_audio_wav(resp, resp_len, play_done_chime, prompt_after, eye_state);
  resp = NULL; /* queue owns the WAV */
  voice_log(ESP_LOG_INFO, turn, "REPLY",
            "bytes=%u ws=%" PRId64 " ms continue=%d eye=%s led=off",
            (unsigned)resp_len, ws_ms, prompt_after ? 1 : 0,
            eye_expr[0] ? eye_expr : "idle");
  (void)medical_ack;
  s_query_busy = false;
  vTaskDelete(NULL);
}

static esp_err_t spawn_voice_ws_job(uint8_t *cap, size_t cap_len, const char *uri,
                                    bool medical_ack_session, uint32_t turn) {
  voice_ws_job_t *job = (voice_ws_job_t *)malloc(sizeof(voice_ws_job_t));
  if (job == NULL) {
    nino_audio_capture_free(cap);
    s_query_busy = false;
    nino_music_pause_for_speech(false);
    return ESP_ERR_NO_MEM;
  }
  job->cap = cap;
  job->cap_len = cap_len;
  strncpy(job->uri, uri, sizeof(job->uri) - 1);
  job->uri[sizeof(job->uri) - 1] = '\0';
  job->medical_ack_session = medical_ack_session;
  job->turn = turn;

  BaseType_t ok =
      xTaskCreate(voice_ws_job_task, "voice_ws", VOICE_WS_JOB_STACK, job, 3, NULL);
  if (ok != pdPASS) {
    free(job);
    nino_audio_capture_free(cap);
    s_query_busy = false;
    nino_music_pause_for_speech(false);
    voice_log(ESP_LOG_ERROR, turn, "FAIL", "stage=ws_task err=no_mem");
    return ESP_ERR_NO_MEM;
  }
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
    voice_log(ESP_LOG_WARN, s_voice_turn, "FAIL",
              "stage=uri reason=not_set — voice connect <PC_LAN_IP> 8000");
    nino_eye_idle();
    return ESP_ERR_INVALID_STATE;
  }
  if (s_query_busy) {
    voice_log(ESP_LOG_WARN, s_voice_turn, "FAIL", "stage=busy reason=query_running");
    return ESP_ERR_INVALID_STATE;
  }
  if (max_ms == 0 || max_ms > VOICE_QUERY_MAX_MS) {
    return ESP_ERR_INVALID_ARG;
  }

  const uint32_t turn = voice_take_turn();
  uri_append_session(uri, sizeof(uri), session);
  uri_append_u32(uri, sizeof(uri), "turn", turn);
  s_query_busy = true;
  nino_music_pause_for_speech(true);
  nino_eye_listening();
  (void)nino_rgb_led_show(NINO_RGB_SHOW_LISTEN);
  const int64_t t_query = esp_timer_get_time();

  uint8_t *cap = NULL;
  size_t cap_len = 0;
  esp_err_t e;
  if (quiet_end_ms > 0) {
    voice_log(ESP_LOG_INFO, turn, "CAPTURE",
              "session=%s preroll=%u gap=%u ms wait=%u ms max=%u ms led=blue",
              session_query_name(session), (unsigned)preroll_samples,
              (unsigned)min_ms, (unsigned)wait_speech_ms, (unsigned)max_ms);
    e = nino_audio_capture_wav_until_quiet(
        &cap, &cap_len, preroll, preroll_samples, min_ms, max_ms, quiet_end_ms,
        quiet_energy, speech_energy, wait_speech_ms, flush_first);
  } else {
    voice_log(ESP_LOG_INFO, turn, "CAPTURE",
              "session=%s fixed=%u ms led=blue", session_query_name(session),
              (unsigned)max_ms);
    e = nino_audio_capture_wav(&cap, &cap_len, max_ms);
  }
  const int64_t cap_ms = (esp_timer_get_time() - t_query) / 1000LL;
  if (e != ESP_OK) {
    voice_log(ESP_LOG_ERROR, turn, "FAIL", "stage=capture err=%s led=red ms=%" PRId64,
              esp_err_to_name(e), cap_ms);
    nino_eye_idle();
    (void)nino_rgb_led_show(NINO_RGB_SHOW_ERROR);
    s_query_busy = false;
    nino_music_pause_for_speech(false);
    return e;
  }

  const uint32_t peak = wav_peak_frame_energy(cap, cap_len);
  uri_append_u32(uri, sizeof(uri), "energy", peak);

  (void)nino_audio_capture_keep_last(cap, cap_len);
  (void)post_aux_recording(uri, cap, cap_len);
  if (peak < (uint32_t)AUX_MIN_UPLOAD_ENERGY) {
    voice_log(ESP_LOG_WARN, turn, "SKIP",
              "reason=silent session=%s peak=%" PRIu32 " th=%u bytes=%u ms=%" PRId64
              " led=off",
              session_query_name(session), peak, (unsigned)AUX_MIN_UPLOAD_ENERGY,
              (unsigned)cap_len, cap_ms);
    nino_audio_capture_free(cap);
    s_query_busy = false;
    nino_eye_idle();
    (void)nino_rgb_led_show(NINO_RGB_SHOW_IDLE);
    nino_music_pause_for_speech(false);
    return ESP_OK;
  }

  /* Listen is only while the mic is open. Go idle while the server thinks. */
  (void)nino_rgb_led_show(NINO_RGB_SHOW_IDLE);
  voice_log(ESP_LOG_INFO, turn, "UPLOAD",
            "session=%s peak=%" PRIu32 " bytes=%u ms=%" PRId64 " led=off",
            session_query_name(session), peak, (unsigned)cap_len, cap_ms);
  nino_eye_thinking();
  return spawn_voice_ws_job(cap, cap_len, uri, medical_ack_session, turn);
}

esp_err_t nino_voice_assist_run_query(uint32_t duration_ms) {
  (void)duration_ms;
  s_force_session = true;
  return ESP_OK;
}

esp_err_t nino_voice_assist_run_query_only(void) {
  s_force_session = true;
  return ESP_OK;
}

#define MED_ACK_TASK_STACK 20480
#define PROMPT_ACK_POST_PLAY_MS 1800

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
  const uint32_t turn = voice_begin_turn();
  voice_log(ESP_LOG_INFO, turn, "CONV",
            "fixed=%u ms chime=%d led=blue", (unsigned)MED_ACK_CAPTURE_MS,
            (int)play_chime);
  (void)nino_rgb_led_show(NINO_RGB_SHOW_LISTEN);
  if (play_chime) {
    esp_err_t chime = nino_voice_play_wake_chime();
    if (chime != ESP_OK) {
      ESP_LOGW(TAG, "Prompt listen chime failed: %s", esp_err_to_name(chime));
    }
    vTaskDelay(pdMS_TO_TICKS(350));
  }
  esp_err_t e = run_ws_and_queue(MED_ACK_CAPTURE_MS, false);
  if (e != ESP_OK) {
    voice_log(ESP_LOG_WARN, turn, "FAIL", "stage=conv err=%s", esp_err_to_name(e));
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

static void log_i2s_in_audio(const int16_t *samples, size_t count, uint32_t energy,
                             uint32_t threshold) {
  int16_t mn = 32767;
  int16_t mx = -32768;
  for (size_t i = 0; i < count; i++) {
    if (samples[i] < mn) {
      mn = samples[i];
    }
    if (samples[i] > mx) {
      mx = samples[i];
    }
  }
  ESP_LOGI(TAG, "I2S IN audio energy=%" PRIu32 " min=%d max=%d th=%" PRIu32,
           energy, (int)mn, (int)mx, threshold);
}

static uint32_t aux_start_threshold(uint32_t noise_floor) {
  const uint32_t from_noise = noise_floor * AUX_NOISE_RATIO;
  return from_noise > (uint32_t)AUX_MIN_START_ENERGY ? from_noise
                                                     : (uint32_t)AUX_MIN_START_ENERGY;
}

static uint32_t aux_quiet_threshold(uint32_t noise_floor) {
  const uint32_t from_noise = noise_floor + noise_floor / 2U;
  uint32_t quiet = from_noise > (uint32_t)AUX_MIN_QUIET_ENERGY
                       ? from_noise
                       : (uint32_t)AUX_MIN_QUIET_ENERGY;
  const uint32_t start = aux_start_threshold(noise_floor);
  if (quiet >= start) {
    quiet = start > 1U ? start - 1U : 1U;
  }
  return quiet;
}

static uint32_t wav_peak_frame_energy(const uint8_t *wav, size_t len) {
  if (wav == NULL || len <= WAV_HEADER_SIZE) {
    return 0;
  }
  const int16_t *pcm = (const int16_t *)(wav + WAV_HEADER_SIZE);
  const size_t samples = (len - WAV_HEADER_SIZE) / sizeof(int16_t);
  uint32_t peak = 0;
  for (size_t i = 0; i + AUX_DETECT_SAMPLES <= samples; i += AUX_DETECT_SAMPLES) {
    const uint32_t energy = aux_frame_energy(pcm + i, AUX_DETECT_SAMPLES);
    if (energy > peak) {
      peak = energy;
    }
  }
  if (peak == 0 && samples > 0) {
    peak = aux_frame_energy(pcm, samples);
  }
  return peak;
}

static void aux_update_noise_floor(uint32_t energy) {
  s_aux_noise_floor = (s_aux_noise_floor * 31U + energy) / 32U;
  if (s_aux_noise_floor < 1U) {
    s_aux_noise_floor = 1U;
  }
}

static void aux_ignore_energy_for_ms(uint32_t ms) {
  const int64_t until = esp_timer_get_time() + (int64_t)ms * 1000LL;
  if (until > s_aux_ignore_until_us) {
    s_aux_ignore_until_us = until;
  }
}

static bool aux_ignore_energy_now(void) {
  if (nino_audio_queue_busy()) {
    aux_ignore_energy_for_ms(AUX_POST_SPEAKER_IGNORE_MS);
    return true;
  }
  return esp_timer_get_time() < s_aux_ignore_until_us;
}

static bool wait_aux_activity(void) {
  int16_t frame[AUX_DETECT_SAMPLES];
  uint32_t speech_streak = 0;
  uint32_t status_ms = 0;
  uint32_t music_listen_ms = 0;

  while (true) {
    if (s_force_session) {
      s_force_session = false;
      const uint32_t turn = voice_begin_turn();
      voice_log(ESP_LOG_INFO, turn, "TRIGGER", "reason=cli stream session led=green");
      return true;
    }
    if (s_query_busy) {
      vTaskDelay(pdMS_TO_TICKS(20));
      speech_streak = 0;
      music_listen_ms = 0;
      continue;
    }
    if (aux_ignore_energy_now()) {
      speech_streak = 0;
      music_listen_ms = 0;
      if (nino_audio_queue_busy()) {
        vTaskDelay(pdMS_TO_TICKS(20));
        status_ms += 20;
      } else {
        esp_err_t rr = nino_mic_read(frame, AUX_DETECT_SAMPLES);
        if (rr == ESP_OK) {
          const uint32_t energy = aux_frame_energy(frame, AUX_DETECT_SAMPLES);
          if (energy < aux_start_threshold(s_aux_noise_floor)) {
            aux_update_noise_floor(energy);
          }
        } else {
          vTaskDelay(pdMS_TO_TICKS(20));
        }
        status_ms += AUX_DETECT_FRAME_MS;
      }
      if (status_ms >= AUX_STATUS_LOG_MS) {
        voice_log(ESP_LOG_INFO, s_voice_turn + 1U, "IDLE",
                  "hold=speaker-bleed ignore=%d ms led=off",
                  (int)((s_aux_ignore_until_us - esp_timer_get_time()) / 1000LL));
        status_ms = 0;
      }
      continue;
    }

    const bool music_on = nino_music_is_playing();
    if (music_on) {
      /* Shared ES8311: duck the speaker so Aux-in can sample Sirena wake. */
      nino_music_pause_for_speech(true);
    }

    esp_err_t rr = nino_mic_read(frame, AUX_DETECT_SAMPLES);
    if (rr != ESP_OK) {
      ESP_LOGW(TAG, "Aux-in listen read failed: %s", esp_err_to_name(rr));
      vTaskDelay(pdMS_TO_TICKS(20));
      speech_streak = 0;
      continue;
    }

    const uint32_t energy = aux_frame_energy(frame, AUX_DETECT_SAMPLES);
    const uint32_t threshold = aux_start_threshold(s_aux_noise_floor);
    status_ms += AUX_DETECT_FRAME_MS;
    if (status_ms >= AUX_STATUS_LOG_MS) {
      voice_log(ESP_LOG_INFO, s_voice_turn + 1U, "IDLE",
                "energy=%" PRIu32 " noise=%" PRIu32 " th=%" PRIu32 " led=off",
                energy, s_aux_noise_floor, threshold);
      status_ms = 0;
    }

    if (energy >= threshold) {
      speech_streak++;
      if (speech_streak == 1U || (speech_streak % 5U) == 0U) {
        log_i2s_in_audio(frame, AUX_DETECT_SAMPLES, energy, threshold);
      }
    } else {
      speech_streak = 0;
      aux_update_noise_floor(energy);
    }
    if (speech_streak >= AUX_START_CONSECUTIVE_FRAMES) {
      const uint32_t turn = voice_begin_turn();
      voice_log(ESP_LOG_INFO, turn, "TRIGGER",
                "energy=%" PRIu32 " noise=%" PRIu32 " th=%" PRIu32 " led=green",
                energy, s_aux_noise_floor, threshold);
      return true;
    }

    if (music_on) {
      music_listen_ms += AUX_DETECT_FRAME_MS;
      if (music_listen_ms >= AUX_MUSIC_LISTEN_MS) {
        nino_mic_close();
        nino_music_pause_for_speech(false);
        vTaskDelay(pdMS_TO_TICKS(AUX_MUSIC_PLAY_SLICE_MS));
        music_listen_ms = 0;
        speech_streak = 0;
      }
    }
  }
}

static void wait_aux_quiet(void) {
  int16_t frame[AUX_DETECT_SAMPLES];
  uint32_t quiet_ms = 0;
  uint32_t waited_ms = 0;
  uint32_t last_energy = s_aux_noise_floor;

  while (waited_ms < AUX_QUIET_MAX_MS) {
    if (s_query_busy || nino_music_blocks_mic()) {
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
  aux_ignore_energy_for_ms(AUX_POST_SPEAKER_IGNORE_MS);
}

static bool copy_ws_uri(char *uri, size_t uri_sz) {
  if (s_ws_uri_mutex == NULL || uri == NULL || uri_sz == 0) {
    return false;
  }
  xSemaphoreTake(s_ws_uri_mutex, portMAX_DELAY);
  snprintf(uri, uri_sz, "%s", s_ws_uri);
  xSemaphoreGive(s_ws_uri_mutex);
  return uri[0] != '\0';
}

/* USB UVC + ESP-Hosted SDIO Wi-Fi contend if UVC starts on the same tick as
 * the first PCM send. Start the camera (and wait until it streams) before
 * opening the voice WS. Stay on through GREET / listen / TTS until true
 * session end (goodbye + GPIO5, or WS fully closed with no retry). */
static void session_camera_on(void) {
  nino_camera_set_session_active(true);
  if (!nino_camera_wait_streaming(CAMERA_STREAM_WAIT_MS)) {
    voice_log(ESP_LOG_WARN, s_voice_turn, "CAMERA",
              "stream wait timeout %u ms — continuing",
              (unsigned)CAMERA_STREAM_WAIT_MS);
  } else {
    voice_log(ESP_LOG_INFO, s_voice_turn, "CAMERA", "streaming");
  }
}

static void session_camera_off(void) {
  nino_camera_set_session_active(false);
}

/* Takes ownership of @p resp (queue frees it after playback, or on queue fail). */
static void play_ws_reply_wav(uint8_t *resp, size_t resp_len, const char *eye_expr,
                              const char *motion_json, bool session_open) {
  nino_eye_state_t eye_state = nino_eye_state_from_name(eye_expr);
  /* Hunt / register prompt: heart only when the person was identified. */
  if (session_open && eye_state != NINO_EYE_HAPPY) {
    eye_state = NINO_EYE_STATE_COUNT;
  }
  bool scripted = false;
  if (motion_json != NULL && motion_json[0] != '\0') {
    const bool skip_curious_greet =
        session_open && strstr(motion_json, "curious") != NULL;
    if (!skip_curious_greet) {
      esp_err_t merr = nino_servo_recplay_play_motion_json(motion_json);
      if (merr == ESP_OK) {
        scripted = true;
      } else if (merr != ESP_ERR_NOT_FOUND) {
        voice_log(ESP_LOG_WARN, s_voice_turn, "MOTION", "play err=%s json=%s",
                  esp_err_to_name(merr), motion_json);
      }
    }
  }
  if (scripted) {
    esp_err_t qerr = nino_audio_queue_wav(resp, resp_len, false, NINO_AUDIO_SERVO_NONE,
                                          false, eye_state);
    if (qerr != ESP_OK) {
      voice_log(ESP_LOG_WARN, s_voice_turn, "REPLY", "queue err=%s",
                esp_err_to_name(qerr));
    }
    return;
  }
  nino_main_queue_audio_wav(resp, resp_len, false, false, eye_state);
}

static void run_conversation_session(const int16_t *preroll, size_t preroll_samples) {
  char uri[VOICE_WS_URI_MAX];
  char session_id[40];
  nino_voice_ws_session_t *ws = NULL;

  if (!copy_ws_uri(uri, sizeof(uri))) {
    voice_log(ESP_LOG_WARN, s_voice_turn, "FAIL", "stage=uri reason=not_set");
    return;
  }

  make_session_id(session_id, sizeof(session_id));
  uri_append_query(uri, sizeof(uri), "stream", "1");
  uri_append_query(uri, sizeof(uri), "session_id", session_id);
  uri_append_u32(uri, sizeof(uri), "turn", s_voice_turn);

  sirena_mics_open();
  s_query_busy = true;
  nino_music_pause_for_speech(true);

  bool session_end = false;

  session_camera_on();
  (void)nino_face_hunt_for_person(FACE_HUNT_MS);

  esp_err_t err = nino_voice_ws_session_open(uri, &ws);
  if (err != ESP_OK || ws == NULL) {
    voice_log(ESP_LOG_ERROR, s_voice_turn, "FAIL", "stage=ws_open err=%s",
              esp_err_to_name(err));
    (void)nino_rgb_led_show(NINO_RGB_SHOW_ERROR);
    goto session_done;
  }

  voice_log(ESP_LOG_INFO, s_voice_turn, "SESSION",
            "id=%s stream=1 gpio5=low camera=on", session_id);

  /* Optional greeting / register prompt before the first listen. */
  {
    uint8_t *resp = NULL;
    size_t resp_len = 0;
    bool skip = false;
    bool end_session = false;
    char eye_expr[16] = {0};
    char motion[192] = {0};
    err = nino_voice_ws_session_wait_reply(ws, SESSION_GREET_WAIT_MS, &resp, &resp_len,
                                           &skip, &end_session, eye_expr,
                                           sizeof(eye_expr), motion, sizeof(motion));
    if (err == ESP_OK && !skip && resp != NULL && resp_len > 0) {
      play_ws_reply_wav(resp, resp_len, eye_expr, motion, true);
      resp = NULL; /* queue owns the WAV; do not free after playback */
      voice_log(ESP_LOG_INFO, s_voice_turn, "GREET",
                "bytes=%u end_session=%d eye=%s", (unsigned)resp_len,
                end_session ? 1 : 0, eye_expr[0] ? eye_expr : "idle");
      nino_audio_queue_wait_idle(AUX_REPLY_WAIT_MS);
      aux_ignore_energy_for_ms(AUX_POST_SPEAKER_IGNORE_MS);
      if (end_session) {
        session_end = true;
        goto session_done;
      }
    } else if (err == ESP_ERR_TIMEOUT) {
      voice_log(ESP_LOG_INFO, s_voice_turn, "GREET", "no session-open TTS — listen");
    }
    free(resp);
    nino_voice_ws_session_begin_turn(ws);
  }

  bool first_turn = true;

  while (!session_end && nino_voice_ws_session_is_open(ws)) {
    const uint32_t turn = first_turn ? s_voice_turn : voice_begin_turn();
    first_turn = false;
    nino_voice_ws_session_begin_turn(ws);
    nino_eye_listening();
    (void)nino_rgb_led_show(NINO_RGB_SHOW_LISTEN);
    voice_log(ESP_LOG_INFO, turn, "STREAM", "sending Aux-in PCM until ASR EOS");

    if (preroll != NULL && preroll_samples > 0) {
      if (nino_voice_ws_session_send_pcm(ws, preroll,
                                         preroll_samples * sizeof(int16_t)) != ESP_OK) {
        if (nino_voice_ws_session_should_pause(ws)) {
          voice_log(ESP_LOG_INFO, turn, "STREAM",
                    "paused because EOS/skip during preroll");
        } else {
          voice_log(ESP_LOG_WARN, turn, "STREAM",
                    "preroll write-0 — wait for server, camera stays on");
        }
      }
      preroll = NULL;
      preroll_samples = 0;
    }

    int16_t frame[AUX_DETECT_SAMPLES];
    uint32_t streamed_ms = 0;
    bool tx_failed = false;
    while (!nino_voice_ws_session_should_pause(ws)) {
      if (nino_music_blocks_mic()) {
        vTaskDelay(pdMS_TO_TICKS(20));
        continue;
      }
      esp_err_t rr = nino_mic_read(frame, AUX_DETECT_SAMPLES);
      if (rr != ESP_OK) {
        vTaskDelay(pdMS_TO_TICKS(20));
        continue;
      }
      if (nino_voice_ws_session_send_pcm(ws, frame, sizeof(frame)) != ESP_OK) {
        if (nino_voice_ws_session_should_pause(ws)) {
          voice_log(ESP_LOG_INFO, turn, "STREAM",
                    "paused because EOS/skip after %u ms", (unsigned)streamed_ms);
          break;
        }
        voice_log(ESP_LOG_WARN, turn, "STREAM",
                  "write-0 after %u ms — wait for server, camera stays on",
                  (unsigned)streamed_ms);
        tx_failed = true;
        break;
      }
      streamed_ms += AUX_DETECT_FRAME_MS;
      if (streamed_ms >= STREAM_LISTEN_CAP_MS) {
        voice_log(ESP_LOG_WARN, turn, "STREAM",
                  "cap=%u ms waiting for ASR EOS/guest/goodbye", (unsigned)streamed_ms);
        break;
      }
    }

    nino_mic_close();
    /* Keep solid blue through STT/LLM until TTS actually starts. */
    nino_eye_thinking();
    if (tx_failed) {
      voice_log(ESP_LOG_WARN, turn, "STREAM",
                "write glitch after %u ms — wait for server, camera stays on",
                (unsigned)streamed_ms);
    } else {
      voice_log(ESP_LOG_INFO, turn, "WAIT_PC", "paused TX after %u ms",
                (unsigned)streamed_ms);
    }

    uint8_t *resp = NULL;
    size_t resp_len = 0;
    bool skip = false;
    bool end_session = false;
    char eye_expr[16] = {0};
    char motion[192] = {0};
    err = nino_voice_ws_session_wait_reply(ws, AUX_REPLY_WAIT_MS, &resp, &resp_len,
                                           &skip, &end_session, eye_expr,
                                           sizeof(eye_expr), motion, sizeof(motion));
    if (err != ESP_OK) {
      voice_log(ESP_LOG_ERROR, turn, "FAIL", "stage=reply err=%s",
                esp_err_to_name(err));
      (void)nino_rgb_led_show(NINO_RGB_SHOW_ERROR);
      free(resp);
      break;
    }
    if (skip || resp == NULL || resp_len == 0) {
      voice_log(ESP_LOG_INFO, turn, "SKIP", "ASR skip — keep listening");
      free(resp);
      continue;
    }

    play_ws_reply_wav(resp, resp_len, eye_expr, motion, false);
    resp = NULL; /* queue owns the WAV; do not free after playback */
    voice_log(ESP_LOG_INFO, turn, "REPLY",
              "bytes=%u end_session=%d eye=%s", (unsigned)resp_len,
              end_session ? 1 : 0, eye_expr[0] ? eye_expr : "idle");
    nino_audio_queue_wait_idle(AUX_REPLY_WAIT_MS);
    aux_ignore_energy_for_ms(AUX_POST_SPEAKER_IGNORE_MS);
    if (end_session) {
      session_end = true;
      voice_log(ESP_LOG_INFO, turn, "SESSION", "ended after TTS id=%s", session_id);
    } else {
      (void)nino_rgb_led_show(NINO_RGB_SHOW_LISTEN);
    }
  }

session_done:
  /* Clear busy before WS teardown so listen cannot stay wedged if destroy lags. */
  s_query_busy = false;
  nino_music_pause_for_speech(false);
  nino_eye_idle();
  (void)nino_rgb_led_show(NINO_RGB_SHOW_IDLE);
  if (ws != NULL) {
    nino_voice_ws_session_close(ws);
  }
  /* GPIO 5 high only after goodbye TTS — keep mics open on stream/WS fail. */
  if (session_end) {
    sirena_mics_close_pulse();
  } else {
    gpio_set_level(SIRENA_MIC_CLOSE_GPIO, 0);
    voice_log(ESP_LOG_INFO, s_voice_turn, "GPIO5",
              "low — Sirena mics stay open (not goodbye)");
  }
  /* Camera off only after goodbye GPIO5 or a fully closed WS — not after hunt,
   * GREET, EOS/WAIT_PC, or a recoverable STREAM write-0. */
  session_camera_off();
  aux_ignore_energy_for_ms(AUX_POST_SPEAKER_IGNORE_MS);
}

static void aux_listen_task(void *arg) {
  (void)arg;

  while (!nino_mic_available()) {
    vTaskDelay(pdMS_TO_TICKS(500));
  }

  nino_audio_queue_wait_idle(5000);
  aux_ignore_energy_for_ms(AUX_POST_SPEAKER_IGNORE_MS);
  voice_log(ESP_LOG_INFO, 0, "ARMED",
            "stream session after Sirena wake start>=%u hold=%u ms gpio5=%d "
            "ignore_speaker=%u ms",
            (unsigned)AUX_MIN_START_ENERGY,
            (unsigned)(AUX_START_CONSECUTIVE_FRAMES * AUX_DETECT_FRAME_MS),
            (int)SIRENA_MIC_CLOSE_GPIO, (unsigned)AUX_POST_SPEAKER_IGNORE_MS);
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

    voice_log(ESP_LOG_INFO, s_voice_turn, "WAKE", "stream session led=blue");
    run_conversation_session(NULL, 0);

    wait_aux_quiet();
    vTaskDelay(pdMS_TO_TICKS(AUX_REARM_DELAY_MS));
    (void)nino_rgb_led_show(NINO_RGB_SHOW_IDLE);
    voice_log(ESP_LOG_INFO, s_voice_turn, "REARM",
              "noise=%" PRIu32 " th=%" PRIu32 " led=off waiting Ok Nino",
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
  sirena_gpio_init();
  BaseType_t ok =
      xTaskCreate(aux_listen_task, "aux_listen", LISTEN_LOOP_STACK, NULL, 3, NULL);
  if (ok != pdPASS) {
    s_listen_loop_started = false;
    ESP_LOGE(TAG, "Could not start Aux-in listen task");
  }
}

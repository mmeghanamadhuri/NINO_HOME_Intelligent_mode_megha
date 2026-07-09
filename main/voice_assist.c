#include "voice_assist.h"

#include <inttypes.h>
#include <stdlib.h>
#include <string.h>

#include "audio_capture.h"
#include "audio_playback.h"
#include "audio_queue.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "usb_mic.h"

#include "nino_eye.h"
#include "voice_wake.h"
#include "voice_ws_client.h"

extern const uint8_t beep_wav_start[] asm("_binary_beep_wav_start");
extern const uint8_t beep_wav_end[] asm("_binary_beep_wav_end");

static const char *TAG = "voice_ast";

#define VOICE_MIC_RATE 16000
#define WAV_HEADER_SIZE 44
#define VAD_FRAME_MS 20
#define VAD_LISTEN_TIMEOUT_MS 3000
#define VAD_TRAILING_SILENCE_MS 450
#define VAD_PRE_ROLL_MS 200
#define VAD_START_CONSECUTIVE_FRAMES 3
#define VAD_STATUS_LOG_PERIOD_MS 1000
#define VAD_SPEAKING_LOG_PERIOD_MS 1000
#define VAD_MIN_START_ENERGY 1800
#define VAD_MIN_CONTINUE_ENERGY 1200
#define VAD_NOISE_FLOOR_DEFAULT 220
/** USB mic ambient often sits above VAD_MIN_CONTINUE_ENERGY — end when energy
 * drops below this fraction of the utterance peak (percent). */
#define VAD_SILENCE_PEAK_RATIO_PCT 30
#define VAD_MIN_SPEECH_MS 300

#define VAD_FRAME_SAMPLES ((VOICE_MIC_RATE * VAD_FRAME_MS) / 1000)
#define VAD_FRAME_BYTES (VAD_FRAME_SAMPLES * (int)sizeof(int16_t))
#define VAD_LISTEN_FRAMES (VAD_LISTEN_TIMEOUT_MS / VAD_FRAME_MS)
#define VAD_SILENCE_STOP_FRAMES (VAD_TRAILING_SILENCE_MS / VAD_FRAME_MS)
#define VAD_PRE_ROLL_FRAMES (VAD_PRE_ROLL_MS / VAD_FRAME_MS)
#define VAD_MIN_SPEECH_FRAMES (VAD_MIN_SPEECH_MS / VAD_FRAME_MS)

#define VOICE_WS_URI_MAX 200
#define VOICE_QUERY_VAD_MAX_SEC 10
#define MED_ACK_VAD_MAX_SEC 8

static char s_ws_uri[VOICE_WS_URI_MAX];
static SemaphoreHandle_t s_ws_uri_mutex;

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

static void write_wav_header(uint8_t *hdr, uint32_t sample_rate, uint16_t channels,
                             uint16_t bits_per_sample, uint32_t data_bytes) {
  const uint32_t byte_rate = sample_rate * channels * (bits_per_sample / 8);
  const uint16_t block_align = channels * (bits_per_sample / 8);
  const uint32_t riff_size = 36 + data_bytes;

  memcpy(hdr + 0, "RIFF", 4);
  hdr[4] = (uint8_t)(riff_size & 0xFF);
  hdr[5] = (uint8_t)((riff_size >> 8) & 0xFF);
  hdr[6] = (uint8_t)((riff_size >> 16) & 0xFF);
  hdr[7] = (uint8_t)((riff_size >> 24) & 0xFF);
  memcpy(hdr + 8, "WAVE", 4);
  memcpy(hdr + 12, "fmt ", 4);
  hdr[16] = 16;
  hdr[17] = 0;
  hdr[18] = 0;
  hdr[19] = 0;
  hdr[20] = 1;
  hdr[21] = 0;
  hdr[22] = (uint8_t)(channels & 0xFF);
  hdr[23] = (uint8_t)((channels >> 8) & 0xFF);
  hdr[24] = (uint8_t)(sample_rate & 0xFF);
  hdr[25] = (uint8_t)((sample_rate >> 8) & 0xFF);
  hdr[26] = (uint8_t)((sample_rate >> 16) & 0xFF);
  hdr[27] = (uint8_t)((sample_rate >> 24) & 0xFF);
  hdr[28] = (uint8_t)(byte_rate & 0xFF);
  hdr[29] = (uint8_t)((byte_rate >> 8) & 0xFF);
  hdr[30] = (uint8_t)((byte_rate >> 16) & 0xFF);
  hdr[31] = (uint8_t)((byte_rate >> 24) & 0xFF);
  hdr[32] = (uint8_t)(block_align & 0xFF);
  hdr[33] = (uint8_t)((block_align >> 8) & 0xFF);
  hdr[34] = (uint8_t)(bits_per_sample & 0xFF);
  hdr[35] = (uint8_t)((bits_per_sample >> 8) & 0xFF);
  memcpy(hdr + 36, "data", 4);
  hdr[40] = (uint8_t)(data_bytes & 0xFF);
  hdr[41] = (uint8_t)((data_bytes >> 8) & 0xFF);
  hdr[42] = (uint8_t)((data_bytes >> 16) & 0xFF);
  hdr[43] = (uint8_t)((data_bytes >> 24) & 0xFF);
}

static uint32_t frame_mean_abs(const int16_t *samples, size_t count) {
  uint64_t total = 0;
  for (size_t i = 0; i < count; ++i) {
    total += (uint32_t)labs((long)samples[i]);
  }
  return (uint32_t)(total / count);
}

static inline uint32_t max_u32(uint32_t a, uint32_t b) { return a > b ? a : b; }

static uint32_t vad_start_threshold(uint32_t noise_floor) {
  return max_u32(VAD_MIN_START_ENERGY, noise_floor * 3U);
}

static uint32_t vad_continue_threshold(uint32_t noise_floor) {
  return max_u32(VAD_MIN_CONTINUE_ENERGY, noise_floor * 2U);
}

static uint32_t vad_silence_end_threshold(uint32_t peak_energy, uint32_t noise_floor) {
  const uint32_t from_peak = (peak_energy * (uint32_t)VAD_SILENCE_PEAK_RATIO_PCT) / 100U;
  return max_u32(vad_start_threshold(noise_floor), from_peak);
}

static void vad_update_noise_floor(uint32_t *noise_floor, uint32_t energy, bool speech_active) {
  const uint32_t weight = speech_active ? 63U : 31U;
  *noise_floor = ((*noise_floor * weight) + energy) / (weight + 1U);
}

typedef struct {
  uint8_t *wav;
  size_t wav_capacity;
  int16_t *frame;
  int16_t *preroll;
  size_t preroll_count;
  size_t preroll_head;
  uint32_t noise_floor;
  uint32_t peak_energy;
  uint32_t status_log_ms;
  uint32_t speaking_log_ms;
} vad_cap_t;

static void vad_preroll_push(vad_cap_t *cap, const int16_t *frame) {
  memcpy(&cap->preroll[cap->preroll_head * VAD_FRAME_SAMPLES], frame, VAD_FRAME_BYTES);
  cap->preroll_head = (cap->preroll_head + 1U) % VAD_PRE_ROLL_FRAMES;
  if (cap->preroll_count < VAD_PRE_ROLL_FRAMES) {
    cap->preroll_count++;
  }
}

static esp_err_t vad_copy_preroll(vad_cap_t *cap, size_t *pcm_samples) {
  const size_t valid_frames = cap->preroll_count;
  const size_t start_index =
      (cap->preroll_head + VAD_PRE_ROLL_FRAMES - valid_frames) % VAD_PRE_ROLL_FRAMES;

  for (size_t i = 0; i < valid_frames; ++i) {
    const size_t frame_index = (start_index + i) % VAD_PRE_ROLL_FRAMES;
    const int16_t *frame = &cap->preroll[frame_index * VAD_FRAME_SAMPLES];
    if ((*pcm_samples + VAD_FRAME_SAMPLES) > ((cap->wav_capacity - WAV_HEADER_SIZE) / sizeof(int16_t))) {
      return ESP_ERR_NO_MEM;
    }
    memcpy(&((int16_t *)(cap->wav + WAV_HEADER_SIZE))[*pcm_samples], frame, VAD_FRAME_BYTES);
    *pcm_samples += VAD_FRAME_SAMPLES;
  }
  return ESP_OK;
}

static esp_err_t vad_append_frame(vad_cap_t *cap, size_t *pcm_samples, const int16_t *frame) {
  if ((*pcm_samples + VAD_FRAME_SAMPLES) > ((cap->wav_capacity - WAV_HEADER_SIZE) / sizeof(int16_t))) {
    return ESP_ERR_NO_MEM;
  }
  memcpy(&((int16_t *)(cap->wav + WAV_HEADER_SIZE))[*pcm_samples], frame, VAD_FRAME_BYTES);
  *pcm_samples += VAD_FRAME_SAMPLES;
  return ESP_OK;
}

static int16_t *s_beep_pcm16 = NULL;
static size_t s_beep_pcm16_samples = 0;

/** Decode embedded main/beep.wav once; resample to 16 kHz mono for fast ES8311 playback. */
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

  const size_t out_frames = (in_frames * (size_t)VOICE_MIC_RATE) / dec.sample_rate_hz + 2U;
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

  ESP_LOGI(TAG, "beep.wav cached: %u samples @ %d Hz (from embedded WAV)", (unsigned)s_beep_pcm16_samples,
           VOICE_MIC_RATE);
  return ESP_OK;
}

static esp_err_t play_embedded_beep(void) {
  esp_err_t e = ensure_beep_pcm16();
  if (e != ESP_OK) {
    return e;
  }
  return nino_audio_play_chime_pcm16_mono(s_beep_pcm16, s_beep_pcm16_samples, (uint32_t)VOICE_MIC_RATE);
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

esp_err_t nino_voice_play_done_chime(void) {
  return play_embedded_beep();
}

esp_err_t nino_voice_capture_vad_wav(int max_seconds, uint8_t **out_wav, size_t *out_len) {
  if (out_wav == NULL || out_len == NULL || max_seconds < 1) {
    return ESP_ERR_INVALID_ARG;
  }
  *out_wav = NULL;
  *out_len = 0;

  vad_cap_t cap = {};
  const size_t max_pcm_bytes =
      (size_t)VOICE_MIC_RATE * (size_t)max_seconds * sizeof(int16_t);
  const size_t wav_capacity = WAV_HEADER_SIZE + max_pcm_bytes;
  size_t pcm_samples = 0;
  uint32_t listen_frames = 0;
  uint32_t speech_streak = 0;
  uint32_t silence_streak = 0;
  bool recording = false;
  esp_err_t result = ESP_FAIL;

  cap.wav = (uint8_t *)malloc(wav_capacity);
  cap.frame = (int16_t *)heap_caps_malloc(VAD_FRAME_BYTES, MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
  cap.preroll = (int16_t *)heap_caps_calloc(
      VAD_PRE_ROLL_FRAMES * VAD_FRAME_SAMPLES, sizeof(int16_t), MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
  if (cap.preroll == NULL) {
    cap.preroll = (int16_t *)calloc(VAD_PRE_ROLL_FRAMES * VAD_FRAME_SAMPLES, sizeof(int16_t));
  }
  if (cap.frame == NULL) {
    cap.frame = (int16_t *)calloc(VAD_FRAME_SAMPLES, sizeof(int16_t));
  }
  if (cap.wav == NULL || cap.frame == NULL || cap.preroll == NULL) {
    result = ESP_ERR_NO_MEM;
    goto cleanup;
  }
  cap.wav_capacity = wav_capacity;
  cap.noise_floor = VAD_NOISE_FLOOR_DEFAULT;

  if (!usb_mic_ready()) {
    ESP_LOGW(TAG, "USB mic not ready");
    result = ESP_ERR_INVALID_STATE;
    goto cleanup;
  }

  nino_voice_wake_set_mic_capture_hold(true);
  usb_mic_flush();

  /* Mic is live: show the listening face until this capture session ends. */
  nino_eye_listening();

  ESP_LOGI(TAG, "VAD armed (max %d s)", max_seconds);

  while (1) {
    esp_err_t rr = usb_mic_read(cap.frame, VAD_FRAME_SAMPLES);
    if (rr != ESP_OK) {
      ESP_LOGW(TAG, "USB mic read: %s", esp_err_to_name(rr));
      result = ESP_ERR_TIMEOUT;
      break;
    }

    const uint32_t energy = frame_mean_abs(cap.frame, VAD_FRAME_SAMPLES);
    const uint32_t threshold =
        recording ? vad_continue_threshold(cap.noise_floor) : vad_start_threshold(cap.noise_floor);
    const bool speech_frame = energy >= threshold;

    if (!recording) {
      vad_preroll_push(&cap, cap.frame);
      listen_frames++;
      cap.status_log_ms += VAD_FRAME_MS;

      if (speech_frame) {
        speech_streak++;
      } else {
        speech_streak = 0;
        vad_update_noise_floor(&cap.noise_floor, energy, false);
      }

      if (cap.status_log_ms >= VAD_STATUS_LOG_PERIOD_MS) {
        ESP_LOGI(TAG, "VAD listen energy=%" PRIu32 " noise=%" PRIu32 " th=%" PRIu32, energy,
                 cap.noise_floor, threshold);
        cap.status_log_ms = 0;
      }

      if (speech_streak >= VAD_START_CONSECUTIVE_FRAMES) {
        if (vad_copy_preroll(&cap, &pcm_samples) != ESP_OK) {
          result = ESP_ERR_NO_MEM;
          break;
        }
        recording = true;
        silence_streak = 0;
        cap.speaking_log_ms = 0;
        cap.peak_energy = energy;
        ESP_LOGI(TAG, "VAD speech start");
      } else if (listen_frames >= VAD_LISTEN_FRAMES) {
        ESP_LOGW(TAG, "VAD timeout (no speech)");
        result = ESP_ERR_NOT_FOUND;
        break;
      }
      continue;
    }

    if (vad_append_frame(&cap, &pcm_samples, cap.frame) != ESP_OK) {
      result = ESP_ERR_NO_MEM;
      break;
    }

    if (energy > cap.peak_energy) {
      cap.peak_energy = energy;
    }

    const uint32_t silence_end_th = vad_silence_end_threshold(cap.peak_energy, cap.noise_floor);
    const bool still_speaking = energy >= silence_end_th;

    if (still_speaking) {
      silence_streak = 0;
      cap.speaking_log_ms += VAD_FRAME_MS;
      if (cap.speaking_log_ms >= VAD_SPEAKING_LOG_PERIOD_MS) {
        ESP_LOGI(TAG, "VAD speaking energy=%" PRIu32 " peak=%" PRIu32 " end_th=%" PRIu32, energy,
                 cap.peak_energy, silence_end_th);
        cap.speaking_log_ms = 0;
      }
    } else {
      silence_streak++;
      vad_update_noise_floor(&cap.noise_floor, energy, true);
      cap.speaking_log_ms += VAD_FRAME_MS;
      if (cap.speaking_log_ms >= VAD_SPEAKING_LOG_PERIOD_MS) {
        ESP_LOGI(TAG, "VAD trailing energy=%" PRIu32 " peak=%" PRIu32 " end_th=%" PRIu32 " silence=%" PRIu32 "ms",
                 energy, cap.peak_energy, silence_end_th, silence_streak * VAD_FRAME_MS);
        cap.speaking_log_ms = 0;
      }
    }

    if (silence_streak >= VAD_SILENCE_STOP_FRAMES &&
        (pcm_samples / VAD_FRAME_SAMPLES) >= VAD_MIN_SPEECH_FRAMES) {
      ESP_LOGI(TAG, "VAD end on silence (peak=%" PRIu32 " end_th=%" PRIu32 ")", cap.peak_energy,
               silence_end_th);
      break;
    }

    if ((pcm_samples * sizeof(int16_t)) >= max_pcm_bytes) {
      ESP_LOGW(TAG, "VAD max duration");
      break;
    }
  }

  if (pcm_samples > 0) {
    write_wav_header(cap.wav, VOICE_MIC_RATE, 1, 16, (uint32_t)(pcm_samples * sizeof(int16_t)));
    *out_wav = cap.wav;
    *out_len = WAV_HEADER_SIZE + pcm_samples * sizeof(int16_t);
    cap.wav = NULL;
    result = ESP_OK;
    ESP_LOGI(TAG, "VAD WAV %" PRIu32 " ms", (uint32_t)(pcm_samples * 1000U / VOICE_MIC_RATE));
  } else if (result == ESP_FAIL) {
    result = ESP_ERR_NOT_FOUND;
  }

cleanup:
  /* Eyes stay in listening here: the captured audio still has to reach the
   * server. nino_voice_ws_exchange() (or the caller on failure) ends it. */
  nino_voice_wake_set_mic_capture_hold(false);
  free(cap.wav);
  free(cap.preroll);
  free(cap.frame);
  return result;
}

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
    vTaskDelete(NULL);
    return;
  }

  uint8_t *resp = NULL;
  size_t resp_len = 0;
  bool prompt_after = false;
  char eye_expr[16] = {0};
  const int64_t t_ws = esp_timer_get_time();
  esp_err_t e = nino_voice_ws_exchange(job->uri, job->cap, job->cap_len, &resp, &resp_len, 300000,
                                       &prompt_after, eye_expr, sizeof(eye_expr));
  ESP_LOGI(TAG, "latency: WS round-trip %" PRId64 " ms", (esp_timer_get_time() - t_ws) / 1000LL);
  nino_audio_capture_free(job->cap);
  const bool medical_ack = job->medical_ack_session;
  free(job);

  if (e != ESP_OK || resp == NULL || resp_len == 0) {
    ESP_LOGE(TAG, "WS exchange failed: %s", esp_err_to_name(e));
    free(resp);
    nino_eye_idle();
    vTaskDelete(NULL);
    return;
  }

  nino_eye_state_t eye_state = nino_eye_state_from_name(eye_expr);
  if (eye_state < NINO_EYE_STATE_COUNT) {
    ESP_LOGI(TAG, "Reply eye_expression=%s -> state %d", eye_expr, (int)eye_state);
  }
  nino_main_queue_audio_wav(resp, resp_len, true, prompt_after, eye_state);
  if (medical_ack && prompt_after) {
    ESP_LOGI(TAG, "Medical follow-up listen scheduled after reply");
  }
  vTaskDelete(NULL);
}

static esp_err_t spawn_voice_ws_job(uint8_t *cap, size_t cap_len, const char *uri,
                                    bool medical_ack_session) {
  voice_ws_job_t *job = (voice_ws_job_t *)malloc(sizeof(voice_ws_job_t));
  if (job == NULL) {
    nino_audio_capture_free(cap);
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
    ESP_LOGE(TAG, "Could not start voice_ws task");
    return ESP_ERR_NO_MEM;
  }
  ESP_LOGI(TAG, "Voice query running in background (wake already re-armed)");
  return ESP_OK;
}

static esp_err_t run_ws_and_queue(int max_seconds, bool medical_ack_session) {
  char uri[VOICE_WS_URI_MAX];
  if (s_ws_uri_mutex == NULL) {
    return ESP_ERR_INVALID_STATE;
  }
  xSemaphoreTake(s_ws_uri_mutex, portMAX_DELAY);
  strncpy(uri, s_ws_uri, sizeof(uri) - 1);
  uri[sizeof(uri) - 1] = '\0';
  xSemaphoreGive(s_ws_uri_mutex);

  if (uri[0] == '\0') {
    ESP_LOGW(TAG, "WS URI not set");
    nino_voice_wake_release_after_wake();
    return ESP_ERR_INVALID_STATE;
  }

  const int64_t t_query = esp_timer_get_time();

  uint8_t *cap = NULL;
  size_t cap_len = 0;
  esp_err_t e = nino_voice_capture_vad_wav(max_seconds, &cap, &cap_len);
  ESP_LOGI(TAG, "latency: VAD capture %" PRId64 " ms", (esp_timer_get_time() - t_query) / 1000LL);
  if (e != ESP_OK) {
    ESP_LOGE(TAG, "VAD capture failed: %s", esp_err_to_name(e));
    nino_eye_idle();
    nino_voice_wake_release_after_wake();
    return e;
  }

  nino_voice_wake_release_after_wake();
  return spawn_voice_ws_job(cap, cap_len, uri, medical_ack_session);
}

esp_err_t nino_voice_assist_run_query_only(void) {
  return run_ws_and_queue(VOICE_QUERY_VAD_MAX_SEC, false);
}

#define MED_ACK_TASK_STACK 20480

static void medical_ack_prompt_task(void *arg) {
  (void)arg;
  vTaskDelay(pdMS_TO_TICKS(500));
  if (!nino_voice_assist_has_ws_uri()) {
    ESP_LOGW(TAG,
             "Medical ack: PC voice not linked — serial: voice connect <PC_LAN_IP> 8000");
    vTaskDelete(NULL);
    return;
  }
  ESP_LOGI(TAG, "Medical reminder — listening (%d s)", MED_ACK_VAD_MAX_SEC);
  esp_err_t chime = nino_voice_play_wake_chime();
  if (chime != ESP_OK) {
    ESP_LOGW(TAG, "Medical ack chime failed: %s", esp_err_to_name(chime));
  }
  esp_err_t e = run_ws_and_queue(MED_ACK_VAD_MAX_SEC, true);
  if (e != ESP_OK) {
    ESP_LOGW(TAG, "Medical ack voice query failed: %s", esp_err_to_name(e));
  }
  vTaskDelete(NULL);
}

void nino_voice_assist_prompt_medical_ack(void) {
  BaseType_t ok = xTaskCreate(medical_ack_prompt_task, "med_ack", MED_ACK_TASK_STACK,
                              NULL, 3, NULL);
  if (ok != pdPASS) {
    ESP_LOGW(TAG, "Could not start medical ack listen task");
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

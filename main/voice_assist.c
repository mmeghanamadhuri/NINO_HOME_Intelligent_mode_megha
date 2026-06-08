#include "voice_assist.h"

#include <inttypes.h>
#include <stdlib.h>
#include <string.h>

#include "audio_capture.h"
#include "audio_playback.h"
#include "bsp/esp32_p4_function_ev_board.h"
#include "esp_codec_dev.h"
#include "esp_codec_dev_types.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"

#include "voice_wake.h"
#include "voice_ws_client.h"

extern const uint8_t beep_wav_start[] asm("_binary_beep_wav_start");
extern const uint8_t beep_wav_end[] asm("_binary_beep_wav_end");

static const char *TAG = "voice_ast";

#define VOICE_MIC_RATE 16000
#define WAV_HEADER_SIZE 44
#define VAD_FRAME_MS 20
#define VAD_LISTEN_TIMEOUT_MS 3000
#define VAD_TRAILING_SILENCE_MS 700
#define VAD_PRE_ROLL_MS 200
#define VAD_START_CONSECUTIVE_FRAMES 3
#define VAD_STATUS_LOG_PERIOD_MS 1000
#define VAD_SPEAKING_LOG_PERIOD_MS 1000
#define VAD_MIN_START_ENERGY 1800
#define VAD_MIN_CONTINUE_ENERGY 1200
#define VAD_NOISE_FLOOR_DEFAULT 220
#define VAD_MIC_GAIN_DB 35.0f

#define VAD_FRAME_SAMPLES ((VOICE_MIC_RATE * VAD_FRAME_MS) / 1000)
#define VAD_FRAME_BYTES (VAD_FRAME_SAMPLES * (int)sizeof(int16_t))
#define VAD_LISTEN_FRAMES (VAD_LISTEN_TIMEOUT_MS / VAD_FRAME_MS)
#define VAD_SILENCE_STOP_FRAMES (VAD_TRAILING_SILENCE_MS / VAD_FRAME_MS)
#define VAD_PRE_ROLL_FRAMES (VAD_PRE_ROLL_MS / VAD_FRAME_MS)

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

static esp_err_t play_embedded_beep(void) {
  const size_t len = (size_t)(beep_wav_end - beep_wav_start);
  if (len < WAV_HEADER_SIZE) {
    ESP_LOGE(TAG, "embedded beep.wav missing or too small");
    return ESP_ERR_INVALID_SIZE;
  }
  return nino_audio_play_wav(beep_wav_start, len);
}

esp_err_t nino_voice_play_wake_chime(void) {
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
  esp_codec_dev_handle_t mic = NULL;

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

  nino_audio_bus_lock();

  esp_err_t err = bsp_i2c_init();
  if (err != ESP_OK && err != ESP_ERR_INVALID_STATE) {
    ESP_LOGE(TAG, "bsp_i2c_init: %s", esp_err_to_name(err));
    result = err;
    goto unlock_cleanup;
  }

  mic = bsp_audio_codec_microphone_init();
  if (mic == NULL) {
    ESP_LOGE(TAG, "mic init failed");
    result = ESP_FAIL;
    goto unlock_cleanup;
  }

  esp_codec_dev_set_in_gain(mic, VAD_MIC_GAIN_DB);

  esp_codec_dev_sample_info_t fs = {
      .bits_per_sample = 16,
      .channel = 1,
      .channel_mask = 0,
      .sample_rate = VOICE_MIC_RATE,
      .mclk_multiple = 0,
  };

  int cr = esp_codec_dev_open(mic, &fs);
  if (cr != ESP_CODEC_DEV_OK) {
    ESP_LOGE(TAG, "mic open failed: %d", cr);
    result = ESP_FAIL;
    goto unlock_cleanup;
  }

  ESP_LOGI(TAG, "VAD armed (max %d s)", max_seconds);

  while (1) {
    cr = esp_codec_dev_read(mic, cap.frame, VAD_FRAME_BYTES);
    if (cr != ESP_CODEC_DEV_OK) {
      ESP_LOGE(TAG, "mic read failed: %d", cr);
      result = ESP_FAIL;
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

    if (speech_frame) {
      silence_streak = 0;
      cap.speaking_log_ms += VAD_FRAME_MS;
      if (cap.speaking_log_ms >= VAD_SPEAKING_LOG_PERIOD_MS) {
        ESP_LOGI(TAG, "VAD speaking energy=%" PRIu32, energy);
        cap.speaking_log_ms = 0;
      }
    } else {
      silence_streak++;
      vad_update_noise_floor(&cap.noise_floor, energy, true);
    }

    if (silence_streak >= VAD_SILENCE_STOP_FRAMES) {
      ESP_LOGI(TAG, "VAD end on silence");
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

unlock_cleanup:
  if (mic != NULL) {
    esp_codec_dev_close(mic);
    mic = NULL;
  }
  nino_voice_wake_drop_mic_locked();
  nino_audio_bus_unlock();

cleanup:
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

static esp_err_t run_ws_and_queue(int max_seconds, bool *prompt_after_out) {
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
    return ESP_ERR_INVALID_STATE;
  }

  uint8_t *cap = NULL;
  size_t cap_len = 0;
  esp_err_t e = nino_voice_capture_vad_wav(max_seconds, &cap, &cap_len);
  if (e != ESP_OK) {
    ESP_LOGE(TAG, "VAD capture failed: %s", esp_err_to_name(e));
    return e;
  }

  uint8_t *resp = NULL;
  size_t resp_len = 0;
  bool prompt_after = false;
  e = nino_voice_ws_exchange(uri, cap, cap_len, &resp, &resp_len, 300000, &prompt_after);
  nino_audio_capture_free(cap);

  if (e != ESP_OK || resp == NULL || resp_len == 0) {
    ESP_LOGE(TAG, "WS exchange failed: %s", esp_err_to_name(e));
    free(resp);
    return (e != ESP_OK) ? e : ESP_ERR_NOT_FOUND;
  }

  nino_main_queue_audio_wav(resp, resp_len, true, prompt_after);
  if (prompt_after_out != NULL) {
    *prompt_after_out = prompt_after;
  }
  return ESP_OK;
}

esp_err_t nino_voice_assist_run_query_only(void) {
  return run_ws_and_queue(VOICE_QUERY_VAD_MAX_SEC, NULL);
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
  bool prompt_after = false;
  esp_err_t e = run_ws_and_queue(MED_ACK_VAD_MAX_SEC, &prompt_after);
  if (e != ESP_OK) {
    ESP_LOGW(TAG, "Medical ack voice query failed: %s", esp_err_to_name(e));
  } else if (prompt_after) {
    ESP_LOGI(TAG, "Medical follow-up listen scheduled after reply");
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

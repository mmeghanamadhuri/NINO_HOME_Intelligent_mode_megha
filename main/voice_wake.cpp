#include "voice_wake.h"

#include <inttypes.h>
#include <stdint.h>
#include <string.h>

#include "sdkconfig.h"

extern "C" {
#include "audio_playback.h"
#include "model_path.h"
#include "nino_eye.h"
#include "usb_mic.h"
#include "voice_assist.h"
}

#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#include "esp_afe_config.h"
#include "esp_afe_sr_iface.h"
#include "esp_afe_sr_models.h"

static const char *TAG = "voice_wake";

#define WAKE_COOLDOWN_MS 2000
#define AFTER_WAKE_TASK_STACK 20480
#define WAKE_TASK_PRIO 3
#define WAKE_IDLE_DELAY_MS 50
#define WAKE_FETCH_DELAY_MS 12
#define WAKE_FETCH_FAIL_DELAY_MS 25

static afe_config_t *configure_wake_afe(srmodel_list_t *models) {
  afe_config_t *afe_cfg = afe_config_init("M", models, AFE_TYPE_SR, AFE_MODE_LOW_COST);
  if (afe_cfg == NULL) {
    return NULL;
  }
  /* USB mono mic: disable AFE front-end blocks meant for onboard multi-mic arrays.
   * Matches ESP-P4-UK-Demo USB-4-mic-wake-word branch (voice_wake.c). */
  afe_cfg->aec_init = false;
  afe_cfg->se_init = false;
  afe_cfg->ns_init = false;
  afe_cfg->vad_init = false;
  afe_cfg->agc_init = false;
  if (afe_cfg->afe_ringbuf_size > 8) {
    afe_cfg->afe_ringbuf_size = 8;
  }
#if CONFIG_SPIRAM
  afe_cfg->memory_alloc_mode = AFE_MEMORY_ALLOC_MORE_PSRAM;
#else
  afe_cfg->memory_alloc_mode = AFE_MEMORY_ALLOC_MORE_INTERNAL;
#endif
  afe_cfg = afe_config_check(afe_cfg);
  if (afe_cfg == NULL) {
    return NULL;
  }
  afe_cfg->aec_init = false;
  afe_cfg->se_init = false;
  afe_cfg->ns_init = false;
  afe_cfg->vad_init = false;
  afe_cfg->agc_init = false;
  if (afe_cfg->afe_ringbuf_size > 8) {
    afe_cfg->afe_ringbuf_size = 8;
  }
  return afe_cfg;
}

#if CONFIG_FREERTOS_NUMBER_OF_CORES > 1
#define WAKE_FEED_CORE 1
#define WAKE_FETCH_CORE 0
#else
#define WAKE_FEED_CORE tskNO_AFFINITY
#define WAKE_FETCH_CORE tskNO_AFFINITY
#endif

static srmodel_list_t *s_models = NULL;
static const esp_afe_sr_iface_t *s_afe = NULL;
static esp_afe_sr_data_t *s_afe_data = NULL;

static volatile bool s_wake_enabled = true;
static volatile bool s_wake_hw_ready = false;
static volatile bool s_after_wake_busy = false;
static volatile bool s_mic_capture_hold = false;
static int64_t s_last_wake_us = 0;

static void after_wake_task(void *arg) {
  (void)arg;
  esp_err_t beep_e = nino_voice_play_wake_chime();
  if (beep_e != ESP_OK) {
    ESP_LOGW(TAG, "wake chime failed: %s", esp_err_to_name(beep_e));
  }
  ESP_LOGI(TAG, "Hi ESP → VAD → server");
  nino_eye_listening();
  usb_mic_flush();
  esp_err_t e = nino_voice_assist_run_query_only();
  if (e != ESP_OK) {
    ESP_LOGW(TAG, "voice query failed: %s", esp_err_to_name(e));
    nino_eye_idle();
    nino_voice_wake_release_after_wake();
  }
  vTaskDelete(NULL);
}

static void wake_feed_task(void *arg) {
  esp_afe_sr_data_t *afe_data = (esp_afe_sr_data_t *)arg;
  const int feed_chunksize = s_afe->get_feed_chunksize(afe_data);
  const int feed_nch = s_afe->get_feed_channel_num(afe_data);
  const int sr = s_afe->get_samp_rate(afe_data);
  const int ms_chunk = (feed_chunksize * 1000) / (sr > 0 ? sr : 16000);
  const size_t feed_bytes = (size_t)feed_chunksize * (size_t)feed_nch * sizeof(int16_t);

  int16_t *buff = (int16_t *)malloc(feed_bytes);
  if (buff == NULL) {
    ESP_LOGE(TAG, "feed buffer alloc failed");
    vTaskDelete(NULL);
    return;
  }

  ESP_LOGI(TAG, "wake feed (USB mic): chunksize=%d nch=%d sr=%d", feed_chunksize, feed_nch, sr);

  int wait_ms = 0;
  while (!usb_mic_ready()) {
    if (wait_ms == 0 || (wait_ms % 5000) == 0) {
      ESP_LOGW(TAG, "Waiting for USB mic on GPIO 24/25 — WakeNet idle until streaming");
    }
    wait_ms += 100;
    vTaskDelay(pdMS_TO_TICKS(100));
  }
  ESP_LOGI(TAG, "USB mic streaming — say \"Hi ESP\"");

  while (true) {
    const bool armed = s_wake_enabled;
    /* Only pause feed while VAD owns the mic. Keep feeding during beep/WS so
     * WakeNet stays armed if the PC voice server is slow or unreachable. */
    const bool mic_exclusive = s_mic_capture_hold;

    if (!armed && !mic_exclusive) {
      vTaskDelay(pdMS_TO_TICKS(WAKE_IDLE_DELAY_MS));
      continue;
    }

    if (mic_exclusive) {
      vTaskDelay(pdMS_TO_TICKS(ms_chunk > 0 ? ms_chunk : 1));
      continue;
    }

    if (usb_mic_read(buff, feed_chunksize) != ESP_OK) {
      vTaskDelay(pdMS_TO_TICKS(10));
      continue;
    }
    s_afe->feed(afe_data, buff);
    vTaskDelay(pdMS_TO_TICKS(ms_chunk > 0 ? ms_chunk : 1));
  }
}

static void wake_fetch_task(void *arg) {
  esp_afe_sr_data_t *afe_data = (esp_afe_sr_data_t *)arg;
  ESP_LOGI(TAG, "wake fetch started — say \"Hi ESP\"");

  while (true) {
    if (!s_wake_enabled && !s_after_wake_busy && !s_mic_capture_hold) {
      vTaskDelay(pdMS_TO_TICKS(WAKE_IDLE_DELAY_MS));
      continue;
    }

    afe_fetch_result_t *res = s_afe->fetch(afe_data);
    TickType_t yield = pdMS_TO_TICKS(WAKE_FETCH_DELAY_MS);

    if (s_after_wake_busy || s_mic_capture_hold) {
      yield = pdMS_TO_TICKS(1);
    } else if (res == NULL || res->ret_value == ESP_FAIL) {
      yield = pdMS_TO_TICKS(WAKE_FETCH_FAIL_DELAY_MS);
    } else if (res->wakeup_state == WAKENET_DETECTED && s_wake_enabled &&
               nino_voice_assist_has_ws_uri() && !s_after_wake_busy) {
      const int64_t now = esp_timer_get_time();
      if (s_last_wake_us == 0 || (now - s_last_wake_us) >= (int64_t)WAKE_COOLDOWN_MS * 1000LL) {
        ESP_LOGI(TAG, "WakeNet detected \"Hi ESP\"");
        s_last_wake_us = now;
        s_after_wake_busy = true;
        BaseType_t ok = xTaskCreatePinnedToCore(after_wake_task, "voice_wake_q", AFTER_WAKE_TASK_STACK, NULL,
                                                WAKE_TASK_PRIO + 1, NULL, WAKE_FEED_CORE);
        if (ok != pdPASS) {
          ESP_LOGW(TAG, "could not start voice_wake_q task");
          s_after_wake_busy = false;
        }
      }
    }

    vTaskDelay(yield);
  }
}

extern "C" void nino_voice_wake_init(void) {
  s_models = esp_srmodel_init("model");
  if (s_models == NULL) {
    ESP_LOGW(TAG, "esp_srmodel_init failed (flash model partition / menuconfig WakeNet?)");
    return;
  }

  afe_config_t *afe_config = configure_wake_afe(s_models);
  if (afe_config == NULL) {
    ESP_LOGE(TAG, "afe_config_init failed");
    esp_srmodel_deinit(s_models);
    s_models = NULL;
    return;
  }

  if (afe_config->wakenet_model_name != NULL) {
    ESP_LOGI(TAG, "WakeNet model: %s (AFE: wake-only, no AEC/NS/AGC)", afe_config->wakenet_model_name);
  }

  s_afe = esp_afe_handle_from_config(afe_config);
  s_afe_data = s_afe->create_from_config(afe_config);
  afe_config_free(afe_config);

  if (s_afe_data == NULL) {
    ESP_LOGE(TAG, "AFE create failed");
    esp_srmodel_deinit(s_models);
    s_models = NULL;
    s_afe = NULL;
    return;
  }

  BaseType_t fetch_ok = xTaskCreatePinnedToCore(wake_fetch_task, "wake_fetch", 10240, s_afe_data, WAKE_TASK_PRIO,
                                                NULL, WAKE_FETCH_CORE);
  BaseType_t feed_ok = xTaskCreatePinnedToCore(wake_feed_task, "wake_feed", 10240, s_afe_data, WAKE_TASK_PRIO, NULL,
                                               WAKE_FEED_CORE);
  if (fetch_ok != pdPASS || feed_ok != pdPASS) {
    ESP_LOGE(TAG, "wake task create failed");
    s_wake_hw_ready = false;
    return;
  }

  s_wake_hw_ready = true;
  ESP_LOGI(TAG, "Wake word ready (USB mic) — say \"Hi ESP\"");
}

extern "C" void nino_voice_wake_drop_mic_locked(void) {
  (void)0;
}

extern "C" void nino_voice_wake_set_mic_capture_hold(bool hold) {
  s_mic_capture_hold = hold;
}

extern "C" void nino_voice_wake_release_after_wake(void) {
  if (!s_after_wake_busy) {
    return;
  }
  usb_mic_flush();
  s_after_wake_busy = false;
  ESP_LOGI(TAG, "wake armed — say \"Hi ESP\"");
}

extern "C" void nino_voice_wake_set_enabled(bool on) {
  s_wake_enabled = on;
  ESP_LOGI(TAG, "wake %s", on ? "on" : "off");
}

extern "C" bool nino_voice_wake_is_enabled(void) { return s_wake_enabled; }

extern "C" bool nino_voice_wake_hw_ready(void) {
  return s_wake_hw_ready && s_afe_data != NULL;
}

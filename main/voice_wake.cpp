#include "voice_wake.h"

#include <inttypes.h>
#include <stdint.h>
#include <string.h>

#include "sdkconfig.h"

extern "C" {
#include "audio_playback.h"
#include "mic_input.h"
#include "model_path.h"
#include "nino_eye.h"
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
/*
 * WakeNet9 models typically advertise ~0.5–0.65 as their operating threshold.
 * Keep a small margin above the default to reject weak speech-like matches
 * without making normal-distance "Hi ESP" / "Jarvis" wakes unreliable.
 * Index 1 = first model, index 2 = second (when both are enabled in menuconfig).
 */
#define WAKE_NET_THRESHOLD 0.65f
#define WAKE_PHRASE_HINT "\"Hi ESP\" or \"Jarvis\""

static afe_config_t *configure_wake_afe(srmodel_list_t *models) {
  afe_config_t *afe_cfg = afe_config_init("M", models, AFE_TYPE_SR, AFE_MODE_LOW_COST);
  if (afe_cfg == NULL) {
    return NULL;
  }
  /* The USB array provides already-beamformed 16 kHz mono input. WakeNet
   * needs only its recognition path, not a second AEC/NS/AGC pipeline. */
  afe_cfg->aec_init = false;
  afe_cfg->se_init = false;
  afe_cfg->ns_init = false;
  afe_cfg->vad_init = false;
  afe_cfg->agc_init = false;
  return afe_config_check(afe_cfg);
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
  const int64_t chime_started_us = esp_timer_get_time();
  ESP_LOGI(TAG, "Wake acknowledged — playing chime now");
  esp_err_t beep_e = nino_voice_play_wake_chime();
  if (beep_e != ESP_OK) {
    ESP_LOGW(TAG, "wake chime failed: %s", esp_err_to_name(beep_e));
  }
  ESP_LOGI(TAG, "Chime finished in %" PRId64 " ms; VAD recording now",
           (esp_timer_get_time() - chime_started_us) / 1000LL);
  nino_eye_listening();
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
  const int sample_rate_hz = s_afe->get_samp_rate(afe_data);
  const int frame_ms = (feed_chunksize * 1000) / (sample_rate_hz > 0 ? sample_rate_hz : 16000);
  const size_t feed_bytes = (size_t)feed_chunksize * (size_t)feed_nch * sizeof(int16_t);

  int16_t *buff = (int16_t *)malloc(feed_bytes);
  if (buff == NULL) {
    ESP_LOGE(TAG, "feed buffer alloc failed");
    vTaskDelete(NULL);
    return;
  }

  ESP_LOGI(TAG, "wake feed: chunksize=%d nch=%d sr=%d (USB preferred, ES8311 fallback)",
           feed_chunksize, feed_nch, sample_rate_hz);

  bool flush_before_feed = true;
  bool logged_mic_source = false;
  while (true) {
    const bool armed = s_wake_enabled;
    const bool mic_exclusive = s_mic_capture_hold;

    if (!armed && !mic_exclusive) {
      flush_before_feed = true;
      logged_mic_source = false;
      vTaskDelay(pdMS_TO_TICKS(WAKE_IDLE_DELAY_MS));
      continue;
    }

    if (mic_exclusive) {
      /* VAD owns the mic. Flush once when it returns so WakeNet restarts from
       * live frames, not the completed query. */
      flush_before_feed = true;
      logged_mic_source = false;
      vTaskDelay(pdMS_TO_TICKS(frame_ms > 0 ? frame_ms : 1));
      continue;
    }

    const int64_t cycle_started_us = esp_timer_get_time();
    if (flush_before_feed) {
      nino_mic_flush();
      flush_before_feed = false;
    }
    if (nino_mic_read(buff, feed_chunksize) != ESP_OK) {
      /* Speaker may have just reclaimed the shared ES8311 I2S; wait a beat
       * and reopen on the next pass instead of spinning the AFE empty. */
      logged_mic_source = false;
      vTaskDelay(pdMS_TO_TICKS(frame_ms > 0 ? frame_ms : 20));
      continue;
    }
    if (!logged_mic_source) {
      ESP_LOGI(TAG, "wake mic source: %s",
               nino_mic_source_name(nino_mic_preferred_source()));
      logged_mic_source = true;
    }
    s_afe->feed(afe_data, buff);

    /* nino_mic_read blocks when the USB stream is live. If it returned a
     * queued frame immediately, only wait for the remaining frame time so
     * AFE is not overfed and the old USB ring delay cannot build up. */
    const int64_t elapsed_us = esp_timer_get_time() - cycle_started_us;
    const int64_t remaining_us = (int64_t)frame_ms * 1000LL - elapsed_us;
    if (remaining_us > 0) {
      vTaskDelay(pdMS_TO_TICKS((uint32_t)((remaining_us + 999LL) / 1000LL)));
    }
  }
}

static void wake_fetch_task(void *arg) {
  esp_afe_sr_data_t *afe_data = (esp_afe_sr_data_t *)arg;
  ESP_LOGI(TAG, "wake fetch started — say " WAKE_PHRASE_HINT);

  while (true) {
    /* VAD owns the microphone during a query. Feed is paused in that state,
     * so fetching only produces empty-AFE errors and consumes stale state. */
    if (s_mic_capture_hold) {
      vTaskDelay(pdMS_TO_TICKS(10));
      continue;
    }

    if (!s_wake_enabled && !s_after_wake_busy && !s_mic_capture_hold) {
      vTaskDelay(pdMS_TO_TICKS(WAKE_IDLE_DELAY_MS));
      continue;
    }

    if (!nino_mic_available()) {
      vTaskDelay(pdMS_TO_TICKS(WAKE_IDLE_DELAY_MS));
      continue;
    }

    afe_fetch_result_t *res = s_afe->fetch(afe_data);
    TickType_t yield = pdMS_TO_TICKS(WAKE_FETCH_DELAY_MS);

    if (s_after_wake_busy) {
      yield = pdMS_TO_TICKS(1);
    } else if (res == NULL || res->ret_value == ESP_FAIL) {
      yield = pdMS_TO_TICKS(WAKE_FETCH_FAIL_DELAY_MS);
    } else if (res->wakeup_state == WAKENET_DETECTED && s_wake_enabled &&
               !s_after_wake_busy) {
      const int64_t now = esp_timer_get_time();
      if (s_last_wake_us == 0 || (now - s_last_wake_us) >= (int64_t)WAKE_COOLDOWN_MS * 1000LL) {
        s_last_wake_us = now;
        ESP_LOGI(TAG, "WakeNet detected (" WAKE_PHRASE_HINT ") via %s",
                 nino_mic_source_name(nino_mic_preferred_source()));
        if (!nino_voice_assist_has_ws_uri()) {
          /* Mic/WakeNet worked; erase-flash clears NVS so the PC URL is gone. */
          ESP_LOGW(TAG,
                   "Wake heard, but voice PC URL not set — serial: "
                   "voice connect <YOUR_PC_LAN_IP> 8000");
        } else {
          s_after_wake_busy = true;
          BaseType_t ok = xTaskCreatePinnedToCore(after_wake_task, "voice_wake_q", AFTER_WAKE_TASK_STACK, NULL,
                                                  WAKE_TASK_PRIO + 1, NULL, WAKE_FEED_CORE);
          if (ok != pdPASS) {
            ESP_LOGW(TAG, "could not start voice_wake_q task");
            s_after_wake_busy = false;
          }
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
    ESP_LOGI(TAG, "WakeNet model 1: %s (USB preferred; ES8311 fallback)",
             afe_config->wakenet_model_name);
  }
  if (afe_config->wakenet_model_name_2 != NULL) {
    ESP_LOGI(TAG, "WakeNet model 2: %s (USB preferred; ES8311 fallback)",
             afe_config->wakenet_model_name_2);
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
  if (s_afe->set_wakenet_threshold != NULL) {
    for (int wn_idx = 1; wn_idx <= 2; ++wn_idx) {
      const int threshold_result =
          s_afe->set_wakenet_threshold(s_afe_data, wn_idx, WAKE_NET_THRESHOLD);
      if (threshold_result > 0) {
        ESP_LOGI(TAG, "WakeNet threshold[%d] set to %.2f", wn_idx, (double)WAKE_NET_THRESHOLD);
      } else if (wn_idx == 1) {
        ESP_LOGW(TAG, "Could not set WakeNet threshold (result=%d)", threshold_result);
      }
    }
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
  ESP_LOGI(TAG, "Wake word ready (USB preferred; ES8311 fallback) — say " WAKE_PHRASE_HINT);
}

extern "C" void nino_voice_wake_drop_mic_locked(void) {
  /* Caller must hold nino_audio_bus_lock(); shared I2S with speaker / VAD. */
  nino_mic_drop_es8311_locked();
}

extern "C" void nino_voice_wake_set_mic_capture_hold(bool hold) {
  if (!hold && s_mic_capture_hold && s_afe != NULL && s_afe_data != NULL &&
      s_afe->reset_buffer != NULL) {
    /* Feed and fetch are still paused here. Clear partial wake-word frames
     * from the completed VAD session before accepting the next wake phrase. */
    (void)s_afe->reset_buffer(s_afe_data);
  }
  s_mic_capture_hold = hold;
}

extern "C" void nino_voice_wake_release_after_wake(void) {
  if (!s_after_wake_busy) {
    return;
  }
  s_after_wake_busy = false;
  ESP_LOGI(TAG, "wake armed — say " WAKE_PHRASE_HINT);
}

extern "C" void nino_voice_wake_set_enabled(bool on) {
  s_wake_enabled = on;
  ESP_LOGI(TAG, "wake %s", on ? "on" : "off");
}

extern "C" bool nino_voice_wake_is_enabled(void) { return s_wake_enabled; }

extern "C" bool nino_voice_wake_hw_ready(void) {
  return s_wake_hw_ready && s_afe_data != NULL;
}

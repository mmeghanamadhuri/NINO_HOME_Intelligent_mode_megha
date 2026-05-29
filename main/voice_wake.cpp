#include "voice_wake.h"

#include <inttypes.h>
#include <stdint.h>
#include <string.h>

#include "sdkconfig.h"

#include "bsp/esp32_p4_function_ev_board.h"
#include "esp_codec_dev.h"
#include "esp_codec_dev_types.h"

extern "C" {
#include "audio_playback.h"
#include "model_path.h"
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
#define WAKE_TASK_PRIO 3 /* below USB/SDIO (5) to avoid HP_INT_WDT under load */
#define WAKE_IDLE_DELAY_MS 50
#define WAKE_FETCH_DELAY_MS 12
#define WAKE_FETCH_FAIL_DELAY_MS 25

/* Feed runs I2S RX; fetch runs heavy AFE. SDIO/Wi-Fi hosted stack tends to sit on
 * CPU1 — keep fetch on CPU0 so INT watchdog does not fire from IRQ latency. */
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
static int64_t s_last_wake_us = 0;

static esp_codec_dev_handle_t s_mic = NULL;

static void close_wake_mic(void) {
  if (s_mic != NULL) {
    esp_codec_dev_close(s_mic);
    s_mic = NULL;
  }
}

static esp_err_t open_wake_mic(void) {
  if (s_mic != NULL) {
    return ESP_OK;
  }
  esp_err_t e = bsp_i2c_init();
  if (e != ESP_OK && e != ESP_ERR_INVALID_STATE) {
    return e;
  }
  s_mic = bsp_audio_codec_microphone_init();
  if (s_mic == NULL) {
    return ESP_ERR_INVALID_STATE;
  }
  esp_codec_dev_set_in_gain(s_mic, 30.0f);
  esp_codec_dev_sample_info_t fs = {
      .bits_per_sample = 16,
      .channel = 1,
      .channel_mask = 0,
      .sample_rate = 16000,
      .mclk_multiple = 0,
  };
  int r = esp_codec_dev_open(s_mic, &fs);
  if (r != ESP_CODEC_DEV_OK) {
    close_wake_mic();
    return ESP_FAIL;
  }
  return ESP_OK;
}

static void after_wake_task(void *arg) {
  (void)arg;
  ESP_LOGI(TAG, "Hi ESP detected → chime → VAD → PC server");
  if (!nino_voice_assist_has_ws_uri()) {
    ESP_LOGW(TAG, "Voice URL not set — on serial run: voice connect <YOUR_PC_LAN_IP> 8000");
    s_after_wake_busy = false;
    vTaskDelete(NULL);
    return;
  }
  esp_err_t chime = nino_voice_play_wake_chime();
  if (chime != ESP_OK) {
    ESP_LOGW(TAG, "wake chime failed (speaker busy?): %s", esp_err_to_name(chime));
  }
  esp_err_t e = nino_voice_assist_run_query_only();
  if (e != ESP_OK) {
    ESP_LOGW(TAG, "voice query failed: %s", esp_err_to_name(e));
  }
  s_after_wake_busy = false;
  vTaskDelete(NULL);
}

static void wake_feed_task(void *arg) {
  esp_afe_sr_data_t *afe_data = (esp_afe_sr_data_t *)arg;
  const int feed_chunksize = s_afe->get_feed_chunksize(afe_data);
  const int feed_nch = s_afe->get_feed_channel_num(afe_data);
  const size_t feed_bytes = (size_t)feed_chunksize * (size_t)feed_nch * sizeof(int16_t);
  int16_t *buff = (int16_t *)malloc(feed_bytes);
  if (buff == NULL) {
    ESP_LOGE(TAG, "feed buffer alloc failed");
    vTaskDelete(NULL);
    return;
  }

  ESP_LOGI(TAG, "wake feed: chunksize=%d nch=%d", feed_chunksize, feed_nch);

  while (true) {
    const bool armed = s_wake_enabled && !s_after_wake_busy;
    const bool busy = s_after_wake_busy;

    if (!armed && !busy) {
      nino_audio_bus_lock();
      close_wake_mic();
      nino_audio_bus_unlock();
      vTaskDelay(pdMS_TO_TICKS(WAKE_IDLE_DELAY_MS));
      continue;
    }

    bool read_ok = false;

    if (armed && !busy) {
      nino_audio_bus_lock();
      if (open_wake_mic() == ESP_OK) {
        int rr = esp_codec_dev_read(s_mic, buff, feed_bytes);
        read_ok = (rr == ESP_CODEC_DEV_OK);
        if (!read_ok) {
          memset(buff, 0, feed_bytes);
          close_wake_mic();
        }
      } else {
        memset(buff, 0, feed_bytes);
      }
      nino_audio_bus_unlock();
    } else {
      nino_audio_bus_lock();
      close_wake_mic();
      nino_audio_bus_unlock();
      memset(buff, 0, feed_bytes);
      vTaskDelay(pdMS_TO_TICKS(WAKE_IDLE_DELAY_MS));
    }

    s_afe->feed(afe_data, buff);

    if (read_ok) {
      vTaskDelay(pdMS_TO_TICKS(2));
    } else {
      const uint32_t chunk_ms = (uint32_t)((feed_chunksize * 1000U) / 16000U) + 2U;
      vTaskDelay(pdMS_TO_TICKS((int)chunk_ms));
    }
  }
}

static void wake_fetch_task(void *arg) {
  esp_afe_sr_data_t *afe_data = (esp_afe_sr_data_t *)arg;
  ESP_LOGI(TAG, "wake fetch started (say \"Hi ESP\")");

  while (true) {
    if (!s_wake_enabled && !s_after_wake_busy) {
      vTaskDelay(pdMS_TO_TICKS(WAKE_IDLE_DELAY_MS));
      continue;
    }

    afe_fetch_result_t *res = s_afe->fetch(afe_data);
    TickType_t yield = pdMS_TO_TICKS(WAKE_FETCH_DELAY_MS);

    if (res == NULL || res->ret_value == ESP_FAIL) {
      yield = pdMS_TO_TICKS(WAKE_FETCH_FAIL_DELAY_MS);
    } else if (res->wakeup_state == WAKENET_DETECTED && s_wake_enabled && !s_after_wake_busy) {
      const int64_t now = esp_timer_get_time();
      if (s_last_wake_us == 0 || (now - s_last_wake_us) >= (int64_t)WAKE_COOLDOWN_MS * 1000LL) {
        s_last_wake_us = now;
        s_after_wake_busy = true;
        BaseType_t ok = xTaskCreatePinnedToCore(after_wake_task, "voice_wake_q", AFTER_WAKE_TASK_STACK, NULL,
                                                WAKE_TASK_PRIO, NULL, WAKE_FEED_CORE);
        if (ok != pdPASS) {
          ESP_LOGW(TAG, "could not start voice_wake_q task");
          s_after_wake_busy = false;
        }
      }
    }

    /* fetch() returns very fast when the ring has data; without a delay the
     * loop pegs a core and the interrupt/task watchdog resets (HP_SYS_HP_WDT). */
    vTaskDelay(yield);
  }
}

extern "C" void nino_voice_wake_init(void) {
  s_models = esp_srmodel_init("model");
  if (s_models == NULL) {
    ESP_LOGW(TAG, "esp_srmodel_init failed (flash model partition / menuconfig WakeNet?)");
    return;
  }

  afe_config_t *afe_config = afe_config_init("M", s_models, AFE_TYPE_SR, AFE_MODE_LOW_COST);
  if (afe_config == NULL) {
    ESP_LOGE(TAG, "afe_config_init failed");
    esp_srmodel_deinit(s_models);
    s_models = NULL;
    return;
  }

  if (afe_config->wakenet_model_name != NULL) {
    ESP_LOGI(TAG, "WakeNet model: %s", afe_config->wakenet_model_name);
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

  BaseType_t f = xTaskCreatePinnedToCore(wake_feed_task, "wake_feed", 10240, s_afe_data, WAKE_TASK_PRIO, NULL,
                                         WAKE_FEED_CORE);
  BaseType_t g = xTaskCreatePinnedToCore(wake_fetch_task, "wake_fetch", 10240, s_afe_data, WAKE_TASK_PRIO, NULL,
                                         WAKE_FETCH_CORE);
  if (f != pdPASS || g != pdPASS) {
    ESP_LOGE(TAG, "wake task create failed");
    s_wake_hw_ready = false;
    return;
  }
  s_wake_hw_ready = true;
  ESP_LOGI(TAG, "Wake word ready — say \"Hi ESP\" (needs voice connect <PC_IP> 8000 for replies)");
}

extern "C" void nino_voice_wake_drop_mic_locked(void) {
  /* Caller must hold nino_audio_bus_lock(); same mutex as audio_playback / voice_assist. */
  close_wake_mic();
}

extern "C" void nino_voice_wake_set_enabled(bool on) {
  s_wake_enabled = on;
  ESP_LOGI(TAG, "wake %s", on ? "on" : "off");
}

extern "C" bool nino_voice_wake_is_enabled(void) { return s_wake_enabled; }

extern "C" bool nino_voice_wake_hw_ready(void) {
  return s_wake_hw_ready && s_afe_data != NULL;
}

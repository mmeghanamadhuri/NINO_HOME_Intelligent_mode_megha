#include "touch_sensor.h"

#include <inttypes.h>
#include <stdbool.h>
#include <stdint.h>

#include "audio_queue.h"
#include "bsp/esp32_p4_function_ev_board.h"
#include "bsp_qt2120.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

static const char *TAG = "nino_touch";

#define TOUCH_POLL_MS 30
#define TOUCH_STARTUP_SETTLE_MS 1200
#define TOUCH_DETECT_STABLE_MS 300
#define TOUCH_SPEAK_COOLDOWN_MS 500
#define TOUCH_RELEASE_STABLE_MS 300
#define TOUCH_TASK_STACK 4096
#define TOUCH_TASK_PRIO 5

extern const uint8_t pdtm_wav_start[] asm("_binary_PDTM_wav_start");
extern const uint8_t pdtm_wav_end[] asm("_binary_PDTM_wav_end");

static void touch_poll_task(void *arg) {
  (void)arg;

  esp_err_t err = bsp_i2c_init();
  if (err != ESP_OK && err != ESP_ERR_INVALID_STATE) {
    ESP_LOGE(TAG, "bsp_i2c_init failed: %s", esp_err_to_name(err));
    vTaskDelete(NULL);
    return;
  }

  i2c_master_bus_handle_t bus = bsp_i2c_get_handle();
  if (bus == NULL) {
    ESP_LOGE(TAG, "BSP I2C bus handle is NULL");
    vTaskDelete(NULL);
    return;
  }

  if (qt2120_init_with_bus(bus) != ESP_OK) {
    ESP_LOGE(TAG, "QT2120 init failed (check I2C wiring / sensor at 0x1C)");
    vTaskDelete(NULL);
    return;
  }

  ESP_LOGI(TAG, "Calibrating QT2120 — keep hands away from the sensor...");
  if (qt2120_calibrate() != ESP_OK) {
    ESP_LOGW(TAG, "QT2120 calibrate command failed");
  }
  vTaskDelay(pdMS_TO_TICKS(TOUCH_STARTUP_SETTLE_MS));

  uint16_t idle_keys = 0;
  if (qt2120_read_keys12(&idle_keys) != ESP_OK) {
    ESP_LOGE(TAG, "Failed to read QT2120 idle state");
    vTaskDelete(NULL);
    return;
  }

  const size_t wav_len = (size_t)(pdtm_wav_end - pdtm_wav_start);
  ESP_LOGI(TAG, "Touch sensor ready, idle mask 0x%03" PRIx16 ", warning clip %u bytes",
           idle_keys, (unsigned)wav_len);

  bool touch_armed = true;
  uint32_t detect_stable_ms = 0;
  uint32_t release_stable_ms = TOUCH_RELEASE_STABLE_MS;
  int64_t last_spoken_us = -((int64_t)TOUCH_SPEAK_COOLDOWN_MS * 1000);
  uint16_t last_raw_keys = idle_keys;
  uint16_t spoken_latch_keys = 0;

  while (true) {
    uint16_t raw_keys = 0;
    if (qt2120_read_keys12(&raw_keys) == ESP_OK) {
      uint16_t active_keys = raw_keys & (uint16_t)~idle_keys;
      bool is_touched = (active_keys != 0);
      int64_t now_us = esp_timer_get_time();
      bool cooldown_done =
          (now_us - last_spoken_us) >= ((int64_t)TOUCH_SPEAK_COOLDOWN_MS * 1000);

      if (raw_keys != last_raw_keys) {
        ESP_LOGI(TAG, "QT2120 raw=0x%03" PRIx16 " active=0x%03" PRIx16, raw_keys,
                 active_keys);
        last_raw_keys = raw_keys;
      }

      if (is_touched) {
        release_stable_ms = 0;
        if (detect_stable_ms < TOUCH_DETECT_STABLE_MS) {
          detect_stable_ms += TOUCH_POLL_MS;
        }
      } else if (release_stable_ms < TOUCH_RELEASE_STABLE_MS) {
        detect_stable_ms = 0;
        release_stable_ms += TOUCH_POLL_MS;
        if (release_stable_ms >= TOUCH_RELEASE_STABLE_MS) {
          touch_armed = true;
          spoken_latch_keys = 0;
          ESP_LOGD(TAG, "Touch released, re-armed");
        }
      } else {
        detect_stable_ms = 0;
      }

      bool new_unspoken_touch = (active_keys & (uint16_t)~spoken_latch_keys) != 0;
      if (is_touched && touch_armed &&
          detect_stable_ms >= TOUCH_DETECT_STABLE_MS && cooldown_done &&
          new_unspoken_touch) {
        touch_armed = false;
        spoken_latch_keys |= active_keys;
        ESP_LOGI(TAG, "Touch detected — queue warning");
        err = nino_audio_queue_wav_copy(pdtm_wav_start, wav_len, false, NINO_AUDIO_SERVO_NOD_LR);
        if (err != ESP_OK) {
          ESP_LOGW(TAG, "Warning queue failed: %s", esp_err_to_name(err));
        }
        last_spoken_us = esp_timer_get_time();
      }
    } else {
      ESP_LOGW(TAG, "Failed to read QT2120 keys");
    }

    vTaskDelay(pdMS_TO_TICKS(TOUCH_POLL_MS));
  }
}

esp_err_t nino_touch_sensor_start(void) {
  BaseType_t ok = xTaskCreate(touch_poll_task, "touch_poll", TOUCH_TASK_STACK, NULL,
                              TOUCH_TASK_PRIO, NULL);
  if (ok != pdPASS) {
    ESP_LOGE(TAG, "Failed to create touch poll task");
    return ESP_ERR_NO_MEM;
  }
  ESP_LOGI(TAG, "Touch poll task started");
  return ESP_OK;
}

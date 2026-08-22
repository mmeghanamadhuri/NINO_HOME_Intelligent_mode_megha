#include "push_buttons.h"

#include <stdbool.h>
#include <stdint.h>

#include "audio_queue.h"
#include "audio_playback.h"
#include "battery_adc.h"
#include "battery_endurance.h"
#include "driver/gpio.h"
#include "esp_attr.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/task.h"
#include "rgb_led.h"
#include "voice_assist.h"
#include "wifi_config.h"

static const char *TAG = "push_btn";

/*
 * Wiring (ESP32-P4-Function-EV-Board J1, active-low to GND):
 *  - GPIO48 (J1 pin 33): short press = toggle Wi-Fi setup + guide WAV;
 *    hold 5 s = DEMO_main.wav
 *  - GPIO47 (J1 pin 37): single press = Aux-in / mic mute on/off
 *
 * Do NOT use:
 *  - GPIO7 / GPIO8  — BSP I2C SDA/SCL (ES8311).
 *  - GPIO53         — BSP_POWER_AMP_IO (speaker amp enable).
 *  - GPIO9–13       — I2S to the codec.
 */
#define BTN_SETUP_GPIO GPIO_NUM_48
#define BTN_MUTE_GPIO GPIO_NUM_47
#define BTN_ACTIVE_LEVEL 0

#define BTN_POLL_MS 20
#define BTN_DEBOUNCE_MS 40
#define BTN_DEMO_HOLD_MS 5000
#define BTN_TASK_STACK 3072
#define BTN_WORKER_STACK 6144
#define BTN_TASK_PRIO 5

typedef enum {
  BTN_EVT_DEMO = 1,
  BTN_EVT_SETUP = 2,
  BTN_EVT_MUTE = 4,
} btn_evt_t;

extern const uint8_t demo_wav_start[] asm("_binary_DEMO_main_wav_start");
extern const uint8_t demo_wav_end[] asm("_binary_DEMO_main_wav_end");

extern const uint8_t nino_home_wifi_wav_start[] asm("_binary_NiNO_Home_Wifi_wav_start");
extern const uint8_t nino_home_wifi_wav_end[] asm("_binary_NiNO_Home_Wifi_wav_end");

static QueueHandle_t s_btn_evt_q;
static volatile bool s_demo_busy;
static int64_t s_last_mute_us;

typedef struct {
  gpio_num_t gpio;
  const char *name;
  bool stable_pressed;
  bool armed;
  bool hold_fired;
  uint32_t debounce_ms;
  uint32_t held_ms;
} btn_state_t;

static bool play_demo_clip(void) {
  const size_t wav_len = (size_t)(demo_wav_end - demo_wav_start);
  if (wav_len < 44) {
    ESP_LOGW(TAG, "Embedded DEMO_main.wav missing or too small");
    return false;
  }

  esp_err_t err = nino_audio_queue_wav_copy(demo_wav_start, wav_len, false,
                                            NINO_AUDIO_SERVO_NONE, false);
  if (err != ESP_OK) {
    ESP_LOGW(TAG, "Failed to queue DEMO_main.wav: %s", esp_err_to_name(err));
    return false;
  }

  ESP_LOGI(TAG, "Queued DEMO_main.wav (%u bytes)", (unsigned)wav_len);
  return true;
}

static bool play_setup_clip(void) {
  const size_t wav_len =
      (size_t)(nino_home_wifi_wav_end - nino_home_wifi_wav_start);
  if (wav_len < 44) {
    ESP_LOGW(TAG, "Embedded NiNO-Home_Wifi.wav missing or too small");
    return false;
  }

  esp_err_t err = nino_audio_queue_wav_copy(
      nino_home_wifi_wav_start, wav_len, false, NINO_AUDIO_SERVO_PRIORITY_NONE,
      false);
  if (err != ESP_OK) {
    ESP_LOGW(TAG, "Failed to queue NiNO-Home_Wifi.wav: %s",
             esp_err_to_name(err));
    return false;
  }

  ESP_LOGI(TAG, "Queued NiNO-Home_Wifi.wav (%u bytes): setup mode",
           (unsigned)wav_len);
  return true;
}

static void post_evt(btn_evt_t evt) {
  if (s_btn_evt_q == NULL) {
    return;
  }
  if (evt == BTN_EVT_DEMO && s_demo_busy) {
    ESP_LOGI(TAG, "Demo already playing — ignore extra request");
    return;
  }
  if (xQueueSend(s_btn_evt_q, &evt, 0) != pdPASS) {
    ESP_LOGW(TAG, "Button event queue full (evt=%d)", (int)evt);
  }
}

esp_err_t nino_push_buttons_trigger_demo(void) {
  if (s_btn_evt_q == NULL) {
    ESP_LOGW(TAG, "trigger_demo before button subsystem started");
    return ESP_ERR_INVALID_STATE;
  }
  ESP_LOGI(TAG, "App request → play DEMO_main.wav");
  post_evt(BTN_EVT_DEMO);
  return ESP_OK;
}

void nino_mute_set(bool muted) {
  (void)nino_audio_set_muted(muted);
}

static void toggle_aux_mute(void) {
  const int64_t now = esp_timer_get_time();
  if (s_last_mute_us != 0 && (now - s_last_mute_us) < 300000) {
    return;
  }
  s_last_mute_us = now;
  const bool next = !nino_voice_assist_aux_is_muted();
  nino_voice_assist_set_aux_muted(next);
  ESP_LOGI(TAG, "Mute toggle → Aux-in %s led=%s gpio5=%s",
           next ? "MUTED" : "unmuted", next ? "red" : "off",
           next ? "high" : "low");
}

void nino_push_buttons_trigger_mute(void) {
  post_evt(BTN_EVT_MUTE);
}

static void IRAM_ATTR mute_isr(void *arg) {
  (void)arg;
  if (s_btn_evt_q == NULL) {
    return;
  }
  btn_evt_t evt = BTN_EVT_MUTE;
  BaseType_t woke = pdFALSE;
  (void)xQueueSendFromISR(s_btn_evt_q, &evt, &woke);
  if (woke == pdTRUE) {
    portYIELD_FROM_ISR();
  }
}

static void btn_worker_task(void *arg) {
  (void)arg;
  btn_evt_t evt;
  while (true) {
    if (xQueueReceive(s_btn_evt_q, &evt, portMAX_DELAY) != pdPASS) {
      continue;
    }
    if (evt == BTN_EVT_DEMO) {
      ESP_LOGI(TAG, "Action: Demo mode — play DEMO_main.wav");
      s_demo_busy = true;
      (void)play_demo_clip();
      vTaskDelay(pdMS_TO_TICKS(500));
      s_demo_busy = false;
    } else if (evt == BTN_EVT_SETUP) {
      s_demo_busy = false;
      if (nino_battery_endurance_is_active()) {
        nino_battery_endurance_stop();
      }
      if (wifi_config_is_setup_mode()) {
        ESP_LOGI(TAG, "Action: leave Wi-Fi setup — restore previous network");
        nino_audio_queue_stop();
        esp_err_t err = wifi_config_exit_setup_mode();
        if (err != ESP_OK && err != ESP_ERR_NOT_FOUND) {
          ESP_LOGW(TAG, "Exit setup mode failed: %s", esp_err_to_name(err));
        }
      } else {
        ESP_LOGI(TAG, "Action: Wi-Fi setup + app guide");
        esp_err_t err = wifi_config_enter_setup_mode();
        if (err != ESP_OK) {
          ESP_LOGW(TAG, "Enter setup mode failed: %s", esp_err_to_name(err));
        }
        (void)play_setup_clip();
      }
    } else if (evt == BTN_EVT_MUTE) {
      toggle_aux_mute();
    }
  }
}

static void btn_update(btn_state_t *btn) {
  const int level = gpio_get_level(btn->gpio);
  const bool raw_pressed = (level == BTN_ACTIVE_LEVEL);

  if (raw_pressed == btn->stable_pressed) {
    btn->debounce_ms = 0;
  } else {
    btn->debounce_ms += BTN_POLL_MS;
    if (btn->debounce_ms >= BTN_DEBOUNCE_MS) {
      const bool was_pressed = btn->stable_pressed;
      btn->stable_pressed = raw_pressed;
      btn->debounce_ms = 0;
      ESP_LOGI(TAG, "GPIO%d (%s) %s (level=%d)", (int)btn->gpio, btn->name,
               raw_pressed ? "DOWN" : "UP", level);

      if (raw_pressed && !was_pressed) {
        btn->held_ms = 0;
        btn->hold_fired = false;
        if (!btn->armed) {
          btn->armed = true;
        } else if (btn->gpio == BTN_MUTE_GPIO) {
          ESP_LOGI(TAG, "GPIO%d press → Aux-in mute toggle", (int)btn->gpio);
          post_evt(BTN_EVT_MUTE);
        }
      } else if (!raw_pressed && was_pressed) {
        if (!btn->armed) {
          btn->armed = true;
        } else if (btn->gpio == BTN_SETUP_GPIO && !btn->hold_fired) {
          ESP_LOGI(TAG, "GPIO%d short press → Wi-Fi setup toggle", (int)btn->gpio);
          post_evt(BTN_EVT_SETUP);
        }
        btn->held_ms = 0;
        btn->hold_fired = false;
      }
    }
  }

  if (btn->stable_pressed && btn->armed && btn->gpio == BTN_SETUP_GPIO) {
    btn->held_ms += BTN_POLL_MS;
    if (!btn->hold_fired && btn->held_ms >= BTN_DEMO_HOLD_MS) {
      btn->hold_fired = true;
      ESP_LOGI(TAG, "GPIO%d held %u ms → Demo", (int)btn->gpio,
               (unsigned)btn->held_ms);
      post_evt(BTN_EVT_DEMO);
    }
  }
}

static void push_buttons_task(void *arg) {
  (void)arg;

  btn_state_t setup = {
      .gpio = BTN_SETUP_GPIO,
      .name = "setup/demo",
      .stable_pressed = false,
      .armed = true,
      .hold_fired = false,
      .debounce_ms = 0,
      .held_ms = 0,
  };
  btn_state_t mute = {
      .gpio = BTN_MUTE_GPIO,
      .name = "aux-mute",
      .stable_pressed = false,
      .armed = true,
      .hold_fired = false,
      .debounce_ms = 0,
      .held_ms = 0,
  };

  if (gpio_get_level(BTN_SETUP_GPIO) == BTN_ACTIVE_LEVEL) {
    setup.stable_pressed = true;
    setup.armed = false;
  }
  if (gpio_get_level(BTN_MUTE_GPIO) == BTN_ACTIVE_LEVEL) {
    mute.stable_pressed = true;
    mute.armed = false;
  }

  ESP_LOGI(TAG,
           "Buttons ready: GPIO%d short=Wi-Fi setup toggle hold5s=Demo; "
           "GPIO%d Aux-in mute (level48=%d level47=%d, 0=pressed)",
           (int)BTN_SETUP_GPIO, (int)BTN_MUTE_GPIO, gpio_get_level(BTN_SETUP_GPIO),
           gpio_get_level(BTN_MUTE_GPIO));

  int last46 = gpio_get_level(GPIO_NUM_46);
  int last53 = gpio_get_level(GPIO_NUM_53);
  uint32_t hunt_ms = 0;

  while (true) {
    btn_update(&setup);
    btn_update(&mute);
    const int lv46 = gpio_get_level(GPIO_NUM_46);
    const int lv53 = gpio_get_level(GPIO_NUM_53);
    if (lv46 != last46) {
      ESP_LOGW(TAG, "GPIO46 changed %d -> %d (not the mute pin; J1 pin 36)", last46,
               lv46);
      last46 = lv46;
    }
    if (lv53 != last53) {
      ESP_LOGW(TAG, "GPIO53 changed %d -> %d (speaker PA — do not use for mute)",
               last53, lv53);
      last53 = lv53;
    }
    hunt_ms += BTN_POLL_MS;
    if (hunt_ms >= 2000) {
      hunt_ms = 0;
      ESP_LOGI(TAG, "btn levels gpio48=%d gpio47=%d gpio46=%d gpio53=%d mute=%d",
               gpio_get_level(BTN_SETUP_GPIO), gpio_get_level(BTN_MUTE_GPIO),
               gpio_get_level(GPIO_NUM_46), gpio_get_level(GPIO_NUM_53),
               nino_voice_assist_aux_is_muted() ? 1 : 0);
    }
    vTaskDelay(pdMS_TO_TICKS(BTN_POLL_MS));
  }
}

esp_err_t nino_push_buttons_start(void) {
  gpio_reset_pin(BTN_SETUP_GPIO);
  gpio_reset_pin(BTN_MUTE_GPIO);
  const gpio_config_t io = {
      .pin_bit_mask = (1ULL << BTN_SETUP_GPIO) | (1ULL << BTN_MUTE_GPIO),
      .mode = GPIO_MODE_INPUT,
      .pull_up_en = GPIO_PULLUP_ENABLE,
      .pull_down_en = GPIO_PULLDOWN_DISABLE,
      .intr_type = GPIO_INTR_DISABLE,
  };
  esp_err_t err = gpio_config(&io);
  if (err != ESP_OK) {
    ESP_LOGE(TAG, "gpio_config failed: %s", esp_err_to_name(err));
    return err;
  }
  (void)gpio_set_pull_mode(BTN_MUTE_GPIO, GPIO_PULLUP_ONLY);
  (void)gpio_set_intr_type(BTN_MUTE_GPIO, GPIO_INTR_NEGEDGE);
  err = gpio_install_isr_service(0);
  if (err != ESP_OK && err != ESP_ERR_INVALID_STATE) {
    ESP_LOGW(TAG, "gpio_install_isr_service: %s", esp_err_to_name(err));
  } else {
    err = gpio_isr_handler_add(BTN_MUTE_GPIO, mute_isr, NULL);
    if (err != ESP_OK) {
      ESP_LOGW(TAG, "GPIO47 ISR not installed: %s", esp_err_to_name(err));
    } else {
      ESP_LOGI(TAG, "GPIO47 mute ISR on falling edge");
    }
  }

  s_btn_evt_q = xQueueCreate(4, sizeof(btn_evt_t));
  if (s_btn_evt_q == NULL) {
    ESP_LOGE(TAG, "Failed to create button event queue");
    return ESP_ERR_NO_MEM;
  }

  if (xTaskCreate(btn_worker_task, "btn_work", BTN_WORKER_STACK, NULL,
                  BTN_TASK_PRIO, NULL) != pdPASS) {
    ESP_LOGE(TAG, "Failed to create button worker task");
    return ESP_ERR_NO_MEM;
  }

  if (xTaskCreate(push_buttons_task, "push_btn", BTN_TASK_STACK, NULL,
                  BTN_TASK_PRIO, NULL) != pdPASS) {
    ESP_LOGE(TAG, "Failed to create push button task");
    return ESP_ERR_NO_MEM;
  }

  ESP_LOGI(TAG, "Push button tasks started");
  return ESP_OK;
}

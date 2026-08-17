#include "push_buttons.h"

#include <stdbool.h>
#include <stdint.h>

#include "audio_queue.h"
#include "driver/gpio.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/task.h"
#include "wifi_config.h"

static const char *TAG = "push_btn";

/*
 * Wiring (ESP32-P4-Function-EV-Board J1, active-low to GND):
 *  - GPIO48 (J1 pin 33): double press = DEMO_main.wav;
 *    triple press = erase Wi-Fi + BLE setup
 *
 * Do NOT use:
 *  - GPIO7 / GPIO8  — BSP I2C SDA/SCL (ES8311). Pressing GPIO7
 *    shorts SDA and kills the speaker codec (I2C_If Fail to … dev 30).
 *  - GPIO53         — BSP_POWER_AMP_IO (speaker amp enable).
 *  - GPIO9–13       — I2S to the codec.
 */
#define BTN_DEMO_GPIO GPIO_NUM_48
#define BTN_ACTIVE_LEVEL 0

#define BTN_POLL_MS 20
#define BTN_DEBOUNCE_MS 40
/* Max quiet time after the last release before a click sequence is evaluated. */
#define BTN_MULTI_GAP_MS 350
#define BTN_CLICKS_DEMO 2
#define BTN_CLICKS_SETUP 3
#define BTN_TASK_STACK 3072
#define BTN_WORKER_STACK 6144
#define BTN_TASK_PRIO 5

typedef enum {
  BTN_EVT_DEMO = 1,
  BTN_EVT_SETUP = 2,
} btn_evt_t;

extern const uint8_t demo_wav_start[] asm("_binary_DEMO_main_wav_start");
extern const uint8_t demo_wav_end[] asm("_binary_DEMO_main_wav_end");

extern const uint8_t nino_home_wifi_wav_start[] asm("_binary_NiNO_Home_Wifi_wav_start");
extern const uint8_t nino_home_wifi_wav_end[] asm("_binary_NiNO_Home_Wifi_wav_end");

static QueueHandle_t s_btn_evt_q;
static volatile bool s_demo_busy;

typedef struct {
  gpio_num_t gpio;
  const char *name;
  bool stable_pressed;
  bool armed;
  uint32_t debounce_ms;
  uint8_t click_count;
  uint32_t gap_ms;
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
    ESP_LOGI(TAG, "Demo already playing — ignore extra double press");
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

static void btn_worker_task(void *arg) {
  (void)arg;
  btn_evt_t evt;
  while (true) {
    if (xQueueReceive(s_btn_evt_q, &evt, portMAX_DELAY) != pdPASS) {
      continue;
    }
    if (evt == BTN_EVT_DEMO) {
      ESP_LOGI(TAG, "Action: play DEMO_main.wav");
      s_demo_busy = true;
      (void)play_demo_clip();
      vTaskDelay(pdMS_TO_TICKS(500));
      s_demo_busy = false;
    } else if (evt == BTN_EVT_SETUP) {
      ESP_LOGI(TAG, "Action: erase Wi-Fi + enter setup mode + BLE");
      s_demo_busy = false;
      esp_err_t err = wifi_config_enter_setup_mode();
      if (err != ESP_OK) {
        ESP_LOGW(TAG, "Enter setup mode failed: %s", esp_err_to_name(err));
      }
      (void)play_setup_clip();
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

      if (!raw_pressed && was_pressed) {
        /* A press→release finishes one click. Skip the very first release
         * after a boot-held press so it does not start a phantom sequence. */
        if (!btn->armed) {
          btn->armed = true;
        } else if (btn->click_count < 200) {
          btn->click_count++;
        }
        btn->gap_ms = 0;
      }
    }
  }

  /* Evaluate the sequence once the button has been idle long enough that no
   * further click is coming. Time only accrues while released. */
  if (btn->armed && btn->click_count > 0 && !btn->stable_pressed) {
    btn->gap_ms += BTN_POLL_MS;
    if (btn->gap_ms >= BTN_MULTI_GAP_MS) {
      const uint8_t clicks = btn->click_count;
      btn->click_count = 0;
      btn->gap_ms = 0;
      if (clicks >= BTN_CLICKS_SETUP) {
        ESP_LOGI(TAG, "GPIO%d (%s) triple press → setup + BLE", (int)btn->gpio,
                 btn->name);
        post_evt(BTN_EVT_SETUP);
      } else if (clicks == BTN_CLICKS_DEMO) {
        ESP_LOGI(TAG, "GPIO%d (%s) double press → demo audio", (int)btn->gpio,
                 btn->name);
        post_evt(BTN_EVT_DEMO);
      } else {
        ESP_LOGI(TAG, "GPIO%d (%s) %u press ignored (need 2=demo, 3=setup)",
                 (int)btn->gpio, btn->name, (unsigned)clicks);
      }
    }
  }
}

static void push_buttons_task(void *arg) {
  (void)arg;

  btn_state_t demo = {
      .gpio = BTN_DEMO_GPIO,
      .name = "demo/setup",
      .stable_pressed = false,
      .armed = true,
      .debounce_ms = 0,
      .click_count = 0,
      .gap_ms = 0,
  };

  if (gpio_get_level(BTN_DEMO_GPIO) == BTN_ACTIVE_LEVEL) {
    demo.stable_pressed = true;
    demo.armed = false;
  }

  ESP_LOGI(TAG,
           "Button ready: GPIO%d double=Demo, triple=erase Wi-Fi + BLE "
           "(level=%d, 0=pressed)",
           (int)BTN_DEMO_GPIO, gpio_get_level(BTN_DEMO_GPIO));

  while (true) {
    btn_update(&demo);
    vTaskDelay(pdMS_TO_TICKS(BTN_POLL_MS));
  }
}

esp_err_t nino_push_buttons_start(void) {
  const gpio_config_t io = {
      .pin_bit_mask = (1ULL << BTN_DEMO_GPIO),
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

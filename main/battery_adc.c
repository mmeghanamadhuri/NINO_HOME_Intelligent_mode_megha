#include "battery_adc.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "audio_playback.h"
#include "audio_queue.h"
#include "driver/gpio.h"
#include "esp_adc/adc_cali.h"
#include "esp_adc/adc_cali_scheme.h"
#include "esp_adc/adc_oneshot.h"
#include "esp_console.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "rgb_led.h"

static const char *TAG = "battery_adc";

#define BATT_ADC_UNIT ADC_UNIT_1
#define BATT_ADC_CHANNEL ADC_CHANNEL_4 /* GPIO20 */
#define BATT_ADC_ATTEN ADC_ATTEN_DB_12
#define BATT_ADC_GPIO 20

/* TFT eyes: SCK 23, MOSI 22, DC 21, RST 6, CS 32/33. GPIO19 is SDIO CMD. */
_Static_assert(BATT_ADC_GPIO != 23 && BATT_ADC_GPIO != 22 && BATT_ADC_GPIO != 21 &&
                   BATT_ADC_GPIO != 6 && BATT_ADC_GPIO != 32 && BATT_ADC_GPIO != 33 &&
                   BATT_ADC_GPIO != 19,
               "battery ADC GPIO must not steal TFT SPI or SDIO CMD");

#define DIV_RTOP_OHM 22000
#define DIV_RBOT_OHM 6600 /* 3.3k + 3.3k in series */

#define CELL_EMPTY_MV 3300
#define CELL_FULL_MV 4200
#define PACK_2S_DETECT_MV 5500
#define PACK_3S_DETECT_MV 9500

#define SAMPLE_COUNT 16
#define OPEN_WARN_MV 80
#define RAW_MAX 4095
#define ATTEN12_FS_MV 3300
/* Pin ≥ ~3.10 V maps to ≥ ~13.4 V pack — above a 3S 4.2 V/cell full charge
 * and typical of GPIO20 sitting on the 3.3 V rail (no divider / leftover RST). */
#define PIN_RAIL_MV 3100
#define RAW_RAIL_MIN 3850
/* 16 back-to-back samples of a real pack stay tight; a floating pad jumps. */
#define RAW_UNSTABLE_SPAN 120
#define LOW_ENTER_CONFIRM 2

#define LOG_TASK_STACK 3072
#define LOG_TASK_PRIO 4
#define LOG_PERIOD_DEFAULT_MS 1000
#define LOG_PERIOD_MIN_MS 200
#define LOG_PERIOD_MAX_MS 10000

#define LOW_ENTER_MV 10000
#define LOW_CLEAR_MV 10400
#define LOW_MIN_VALID_MV 8000
/* 3S at 4.4 V/cell. Above this is ADC clip / a 5V rail scaled as a pack. */
#define LOW_MAX_VALID_MV 13200
#define LOW_POLL_MS 2000
#define LOW_SAY_MS 20000
#define LOW_TASK_STACK 4096
#define LOW_TASK_PRIO 5

extern const uint8_t low_battery_wav_start[] asm("_binary_low_battery_wav_start");
extern const uint8_t low_battery_wav_end[] asm("_binary_low_battery_wav_end");

static adc_oneshot_unit_handle_t s_adc;
static adc_cali_handle_t s_cali;
static adc_unit_t s_unit = BATT_ADC_UNIT;
static adc_channel_t s_channel = BATT_ADC_CHANNEL;
static SemaphoreHandle_t s_lock;
static bool s_ready;
static TaskHandle_t s_log_task;
static volatile bool s_log_run;
static uint32_t s_log_period_ms = LOG_PERIOD_DEFAULT_MS;
static TaskHandle_t s_low_task;
static volatile bool s_low_alert;

static void start_low_monitor(void);

static int32_t adc_to_battery_mv(int32_t adc_mv) {
  return (int32_t)(((int64_t)adc_mv * (DIV_RTOP_OHM + DIV_RBOT_OHM)) /
                   DIV_RBOT_OHM);
}

static int pack_cells(int32_t battery_mv) {
  if (battery_mv >= PACK_3S_DETECT_MV) {
    return 3;
  }
  if (battery_mv >= PACK_2S_DETECT_MV) {
    return 2;
  }
  return 1;
}

static uint8_t battery_percent(int32_t battery_mv) {
  int cells = pack_cells(battery_mv);
  int32_t empty = CELL_EMPTY_MV * cells;
  int32_t full = CELL_FULL_MV * cells;
  if (battery_mv <= empty) {
    return 0;
  }
  if (battery_mv >= full) {
    return 100;
  }
  return (uint8_t)(((battery_mv - empty) * 100) / (full - empty));
}

static const char *pack_name(int32_t battery_mv) {
  int cells = pack_cells(battery_mv);
  if (cells == 3) {
    return "3S";
  }
  if (cells == 2) {
    return "2S";
  }
  return "1S";
}

static void print_mv(int32_t mv) {
  printf("%ld.%03ld V", (long)(mv / 1000), (long)(labs((long)mv) % 1000));
}

static int32_t raw_to_pin_mv(int raw) {
  if (s_cali != NULL) {
    int pin_mv = 0;
    if (adc_cali_raw_to_voltage(s_cali, raw, &pin_mv) == ESP_OK) {
      return pin_mv;
    }
  }
  return (raw * ATTEN12_FS_MV) / RAW_MAX;
}

static void fill_sample(nino_battery_sample_t *out, int raw, int raw_span) {
  int32_t adc_mv = raw_to_pin_mv(raw);
  int32_t battery_mv = adc_to_battery_mv(adc_mv);
  out->raw = (int16_t)raw;
  out->raw_span = (int16_t)raw_span;
  out->adc_mv = adc_mv;
  out->battery_mv = battery_mv;
  out->percent = battery_percent(battery_mv);
}

/*
 * A connected 3S pack through 22k/6.6k sits around 2.3–2.9 V at GPIO20.
 * No-pack / 5V USB typically reads ~0 V (open divider), ~5 V mapped (USB on
 * the pack input), ADC rail, or a noisy mid-scale float that looks like 8–10 V.
 */
static const char *pack_absent_reason(bool read_ok, const nino_battery_sample_t *s) {
  if (!read_ok || s == NULL) {
    return "adc_read_failed";
  }
  if (s->adc_mv > -OPEN_WARN_MV && s->adc_mv < OPEN_WARN_MV) {
    return "open_divider";
  }
  if (s->raw >= RAW_RAIL_MIN || s->adc_mv >= PIN_RAIL_MV) {
    return "adc_rail";
  }
  if (s->raw_span >= RAW_UNSTABLE_SPAN) {
    return "floating_gpio";
  }
  if (s->battery_mv < LOW_MIN_VALID_MV) {
    return "below_8v_5v_or_unplugged";
  }
  if (s->battery_mv > LOW_MAX_VALID_MV) {
    return "implausible_high";
  }
  return NULL;
}

static void warn_if_open(int32_t pin_mv) {
  if (pin_mv > -OPEN_WARN_MV && pin_mv < OPEN_WARN_MV) {
    ESP_LOGW(TAG,
             "GPIO%d is %ld mV. Divider midpoint is not seeing the pack. "
             "Meter GPIO%d-to-GND should be ~2.77 V for 12 V with 22k / 6.6k. "
             "Never put pack voltage on GPIO%d.",
             BATT_ADC_GPIO, (long)pin_mv, BATT_ADC_GPIO, BATT_ADC_GPIO);
  }
}

static bool lock_take(void) {
  if (s_lock == NULL) {
    return false;
  }
  return xSemaphoreTake(s_lock, pdMS_TO_TICKS(200)) == pdTRUE;
}

static void lock_give(void) {
  if (s_lock != NULL) {
    xSemaphoreGive(s_lock);
  }
}

static esp_err_t read_raw_avg(int *raw_out, int *span_out) {
  int32_t sum = 0;
  int got = 0;
  int min_raw = RAW_MAX;
  int max_raw = 0;
  for (int i = 0; i < SAMPLE_COUNT; i++) {
    int raw = 0;
    esp_err_t err = adc_oneshot_read(s_adc, s_channel, &raw);
    if (err != ESP_OK) {
      return err;
    }
    if (raw < min_raw) {
      min_raw = raw;
    }
    if (raw > max_raw) {
      max_raw = raw;
    }
    sum += raw;
    got++;
  }
  *raw_out = sum / got;
  if (span_out != NULL) {
    *span_out = (got > 0) ? (max_raw - min_raw) : 0;
  }
  return ESP_OK;
}

static bool init_calibration(void) {
#if ADC_CALI_SCHEME_CURVE_FITTING_SUPPORTED
  adc_cali_curve_fitting_config_t cali_cfg = {
      .unit_id = s_unit,
      .chan = s_channel,
      .atten = BATT_ADC_ATTEN,
      .bitwidth = ADC_BITWIDTH_DEFAULT,
  };
  esp_err_t err = adc_cali_create_scheme_curve_fitting(&cali_cfg, &s_cali);
  if (err == ESP_OK) {
    ESP_LOGI(TAG, "ADC curve-fitting calibration ready");
    return true;
  }
  ESP_LOGW(TAG, "ADC calibration unavailable (%s); using 0-3300 mV scale",
           esp_err_to_name(err));
  s_cali = NULL;
#else
  ESP_LOGW(TAG, "ADC curve-fitting not supported; using 0-3300 mV scale");
  s_cali = NULL;
#endif
  return false;
}

esp_err_t nino_battery_adc_init(void) {
  if (s_ready) {
    return ESP_OK;
  }

  if (s_lock == NULL) {
    s_lock = xSemaphoreCreateMutex();
    if (s_lock == NULL) {
      return ESP_ERR_NO_MEM;
    }
  }

  gpio_reset_pin((gpio_num_t)BATT_ADC_GPIO);
  gpio_set_direction((gpio_num_t)BATT_ADC_GPIO, GPIO_MODE_INPUT);
  gpio_pullup_dis((gpio_num_t)BATT_ADC_GPIO);
  gpio_pulldown_dis((gpio_num_t)BATT_ADC_GPIO);
  vTaskDelay(pdMS_TO_TICKS(20));
  const int pad_level = gpio_get_level((gpio_num_t)BATT_ADC_GPIO);
  ESP_LOGI(TAG,
           "GPIO%d pad digital=%d (1=high ~battery present, 0=pad is ~0 V)",
           BATT_ADC_GPIO, pad_level);

  adc_unit_t unit = BATT_ADC_UNIT;
  adc_channel_t channel = BATT_ADC_CHANNEL;
  esp_err_t map_err =
      adc_oneshot_io_to_channel(BATT_ADC_GPIO, &unit, &channel);
  if (map_err != ESP_OK) {
    ESP_LOGE(TAG, "GPIO%d is not an ADC pin: %s", BATT_ADC_GPIO,
             esp_err_to_name(map_err));
    return map_err;
  }
  ESP_LOGI(TAG, "GPIO%d maps to ADC%d CH%d", BATT_ADC_GPIO, (int)unit + 1,
           (int)channel);
  s_unit = unit;
  s_channel = channel;

  adc_oneshot_unit_init_cfg_t unit_cfg = {
      .unit_id = unit,
      .ulp_mode = ADC_ULP_MODE_DISABLE,
  };
  esp_err_t err = adc_oneshot_new_unit(&unit_cfg, &s_adc);
  if (err != ESP_OK) {
    ESP_LOGE(TAG, "ADC unit init failed: %s", esp_err_to_name(err));
    return err;
  }

  adc_oneshot_chan_cfg_t chan_cfg = {
      .bitwidth = ADC_BITWIDTH_DEFAULT,
      .atten = BATT_ADC_ATTEN,
  };
  err = adc_oneshot_config_channel(s_adc, channel, &chan_cfg);
  if (err != ESP_OK) {
    ESP_LOGE(TAG, "GPIO%d ADC config failed: %s", BATT_ADC_GPIO,
             esp_err_to_name(err));
    (void)adc_oneshot_del_unit(s_adc);
    s_adc = NULL;
    return err;
  }

  (void)init_calibration();
  s_ready = true;
  ESP_LOGI(TAG, "GPIO%d ADC%d_CH%d divider 22k / 6.6k (2x 3.3k) scale %d/%d",
           BATT_ADC_GPIO, (int)s_unit + 1, (int)s_channel,
           DIV_RTOP_OHM + DIV_RBOT_OHM, DIV_RBOT_OHM);

  nino_battery_sample_t sample;
  if (nino_battery_adc_read(&sample) == ESP_OK) {
    ESP_LOGI(TAG, "boot GPIO%d raw=%d span=%d pin=%ld mV vin=%ld mV %u%% %s",
             BATT_ADC_GPIO, (int)sample.raw, (int)sample.raw_span,
             (long)sample.adc_mv, (long)sample.battery_mv,
             (unsigned)sample.percent, pack_name(sample.battery_mv));
    warn_if_open(sample.adc_mv);
    const char *absent = pack_absent_reason(true, &sample);
    if (absent != NULL) {
      ESP_LOGI(TAG,
               "low-battery check skipped: no pack or 5V supply "
               "(vin=%ld mV pin=%ld mV raw=%d span=%d reason=%s)",
               (long)sample.battery_mv, (long)sample.adc_mv, (int)sample.raw,
               (int)sample.raw_span, absent);
    } else {
      ESP_LOGI(TAG,
               "pack present vin=%ld mV — low-battery monitor armed (enter ≤10.0 V)",
               (long)sample.battery_mv);
    }
  }
  start_low_monitor();
  return ESP_OK;
}

bool nino_battery_adc_ready(void) { return s_ready; }

bool nino_battery_low_alert_active(void) { return s_low_alert; }

static void say_low_battery(void) {
  const size_t wav_len = (size_t)(low_battery_wav_end - low_battery_wav_start);
  if (wav_len < 44) {
    ESP_LOGW(TAG, "low_battery.wav missing");
    return;
  }
  /* Priority queue so the charge prompt is heard even if another clip is playing. */
  esp_err_t err = nino_audio_queue_wav_copy(low_battery_wav_start, wav_len, false,
                                            NINO_AUDIO_SERVO_PRIORITY_NONE, false);
  if (err != ESP_OK) {
    ESP_LOGW(TAG, "low-battery clip not queued: %s", esp_err_to_name(err));
  }
}

static void enter_low_battery(int32_t battery_mv) {
  s_low_alert = true;
  nino_audio_refresh_mute();
  (void)nino_rgb_led_show(NINO_RGB_SHOW_BATTERY);
  ESP_LOGW(TAG,
           "LOW BATTERY %ld mV — pack is connected and low; red LED + charge prompt",
           (long)battery_mv);
  say_low_battery();
}

static void exit_low_battery(int32_t battery_mv, const char *why) {
  s_low_alert = false;
  nino_audio_refresh_mute();
  if (nino_audio_is_muted()) {
    (void)nino_rgb_led_show(NINO_RGB_SHOW_MUTE);
  } else {
    (void)nino_rgb_led_show(NINO_RGB_SHOW_IDLE);
  }
  ESP_LOGI(TAG, "battery recovered %ld mV (%s)", (long)battery_mv,
           why != NULL ? why : "cleared");
}

static void log_skip_if_changed(const char *reason, const nino_battery_sample_t *s) {
  static char s_last_reason[32];
  if (reason == NULL) {
    if (s_last_reason[0] != '\0') {
      ESP_LOGI(TAG, "pack present vin=%ld mV — low-battery monitor armed",
               s != NULL ? (long)s->battery_mv : 0);
      s_last_reason[0] = '\0';
    }
    return;
  }
  if (strncmp(s_last_reason, reason, sizeof(s_last_reason)) == 0) {
    return;
  }
  snprintf(s_last_reason, sizeof(s_last_reason), "%s", reason);
  ESP_LOGI(TAG,
           "low-battery check skipped: no pack or 5V supply "
           "(vin=%ld mV pin=%ld mV raw=%d span=%d reason=%s)",
           s != NULL ? (long)s->battery_mv : 0,
           s != NULL ? (long)s->adc_mv : 0,
           s != NULL ? (int)s->raw : 0,
           s != NULL ? (int)s->raw_span : 0, reason);
}

static void low_battery_task(void *arg) {
  (void)arg;
  /* ADC inits before the speaker queue; wait so the first prompt can play. */
  vTaskDelay(pdMS_TO_TICKS(8000));
  uint32_t since_say_ms = LOW_SAY_MS;
  int low_streak = 0;
  while (true) {
    nino_battery_sample_t sample = {};
    const bool ok = (nino_battery_adc_read(&sample) == ESP_OK);
    const char *absent = pack_absent_reason(ok, &sample);
    const bool pack_present = (absent == NULL);
    const bool is_low = pack_present && sample.battery_mv <= LOW_ENTER_MV;
    const bool recovered = pack_present && sample.battery_mv >= LOW_CLEAR_MV;

    log_skip_if_changed(absent, &sample);

    if (is_low) {
      if (low_streak < LOW_ENTER_CONFIRM) {
        low_streak++;
      }
    } else {
      low_streak = 0;
    }

    if (!s_low_alert && is_low && low_streak >= LOW_ENTER_CONFIRM) {
      enter_low_battery(sample.battery_mv);
      since_say_ms = 0;
    } else if (s_low_alert && (recovered || !pack_present)) {
      exit_low_battery(ok ? sample.battery_mv : 0,
                       !pack_present ? (absent != NULL ? absent : "pack_removed")
                                     : "charged");
      since_say_ms = LOW_SAY_MS;
    } else if (s_low_alert) {
      if (nino_rgb_led_current() != NINO_RGB_SHOW_BATTERY) {
        (void)nino_rgb_led_show(NINO_RGB_SHOW_BATTERY);
      }
      since_say_ms += LOW_POLL_MS;
      if (since_say_ms >= LOW_SAY_MS) {
        since_say_ms = 0;
        say_low_battery();
      }
    }

    vTaskDelay(pdMS_TO_TICKS(LOW_POLL_MS));
  }
}

static void start_low_monitor(void) {
  if (s_low_task != NULL) {
    return;
  }
  BaseType_t ok = xTaskCreate(low_battery_task, "batt_low", LOW_TASK_STACK, NULL,
                              LOW_TASK_PRIO, &s_low_task);
  if (ok != pdPASS) {
    s_low_task = NULL;
    ESP_LOGW(TAG, "low-battery monitor not started");
  }
}

esp_err_t nino_battery_adc_read(nino_battery_sample_t *out) {
  if (!s_ready || s_adc == NULL || out == NULL) {
    return ESP_ERR_INVALID_STATE;
  }
  if (!lock_take()) {
    return ESP_ERR_TIMEOUT;
  }

  int raw = 0;
  int span = 0;
  esp_err_t err = read_raw_avg(&raw, &span);
  lock_give();
  if (err != ESP_OK) {
    return err;
  }
  fill_sample(out, raw, span);
  return ESP_OK;
}

static void log_sample(const nino_battery_sample_t *s) {
  const char *absent = pack_absent_reason(true, s);
  ESP_LOGI(TAG, "GPIO%d raw=%d span=%d pin=%ld mV vin=%ld mV %u%% %s%s%s",
           BATT_ADC_GPIO, (int)s->raw, (int)s->raw_span, (long)s->adc_mv,
           (long)s->battery_mv, (unsigned)s->percent, pack_name(s->battery_mv),
           absent != NULL ? " skip=" : "", absent != NULL ? absent : "");
}

static void print_sample(const nino_battery_sample_t *s) {
  printf("GPIO%d (22k / 2x3.3k) raw=%d span=%d  pin=", BATT_ADC_GPIO,
         (int)s->raw, (int)s->raw_span);
  print_mv(s->adc_mv);
  printf("  pack=");
  print_mv(s->battery_mv);
  printf("  %u%% (%s)\n", (unsigned)s->percent, pack_name(s->battery_mv));
  if (s->adc_mv > -OPEN_WARN_MV && s->adc_mv < OPEN_WARN_MV) {
    printf("GPIO%d is ~0 V. Put the 22k / 6.6k midpoint on GPIO%d. "
           "Meter GPIO%d-GND must be ~2.77 V at 12 V, never 12 V.\n",
           BATT_ADC_GPIO, BATT_ADC_GPIO, BATT_ADC_GPIO);
  }
}

static void log_task(void *arg) {
  (void)arg;
  while (s_log_run) {
    nino_battery_sample_t sample;
    if (nino_battery_adc_read(&sample) == ESP_OK) {
      log_sample(&sample);
      warn_if_open(sample.adc_mv);
    } else {
      ESP_LOGW(TAG, "read failed");
    }
    vTaskDelay(pdMS_TO_TICKS(s_log_period_ms));
  }
  s_log_task = NULL;
  vTaskDelete(NULL);
}

static int start_log(uint32_t period_ms) {
  if (!s_ready) {
    printf("Battery ADC not ready. Type: adc\n");
    return 1;
  }
  if (period_ms < LOG_PERIOD_MIN_MS) {
    period_ms = LOG_PERIOD_MIN_MS;
  }
  if (period_ms > LOG_PERIOD_MAX_MS) {
    period_ms = LOG_PERIOD_MAX_MS;
  }
  s_log_period_ms = period_ms;
  if (s_log_task != NULL) {
    printf("ADC log already running every %u ms. Type 'adc stop' to halt.\n",
           (unsigned)s_log_period_ms);
    return 0;
  }
  s_log_run = true;
  BaseType_t ok = xTaskCreate(log_task, "adc_log", LOG_TASK_STACK, NULL,
                              LOG_TASK_PRIO, &s_log_task);
  if (ok != pdPASS) {
    s_log_run = false;
    s_log_task = NULL;
    printf("Failed to start ADC log task\n");
    return 1;
  }
  printf("ADC log every %u ms (GPIO%d, 22k / 6.6k). Type 'adc stop' to halt.\n",
         (unsigned)s_log_period_ms, BATT_ADC_GPIO);
  return 0;
}

static void stop_log(void) {
  if (s_log_task == NULL) {
    printf("ADC log is not running\n");
    return;
  }
  s_log_run = false;
  printf("ADC log stopping...\n");
}

static int cmd_adc(int argc, char **argv) {
  if (argc >= 2 && strcmp(argv[1], "stop") == 0) {
    stop_log();
    return 0;
  }

  if (argc >= 2 && (strcmp(argv[1], "log") == 0 || strcmp(argv[1], "on") == 0)) {
    uint32_t period_ms = LOG_PERIOD_DEFAULT_MS;
    if (argc >= 3) {
      int ms = atoi(argv[2]);
      if (ms <= 0) {
        printf("Usage: adc log [ms]\n");
        return 1;
      }
      period_ms = (uint32_t)ms;
    }
    return start_log(period_ms);
  }

  if (!s_ready) {
    printf("Battery ADC not initialized. Trying again...\n");
    if (nino_battery_adc_init() != ESP_OK) {
      printf("GPIO%d ADC init failed.\n", BATT_ADC_GPIO);
      return 1;
    }
  }

  nino_battery_sample_t sample;
  esp_err_t err = nino_battery_adc_read(&sample);
  if (err != ESP_OK) {
    printf("Battery ADC read failed: %s\n", esp_err_to_name(err));
    return 1;
  }
  print_sample(&sample);
  const char *absent = pack_absent_reason(true, &sample);
  if (absent != NULL) {
    printf("No pack / 5V supply (reason=%s) — low-battery check skipped "
           "(no WAV, LED blink, or unmute).\n",
           absent);
  } else if (s_low_alert) {
    printf("LOW BATTERY alert active (red blink + charge prompt). Clears above 10.4 V.\n");
  } else if (sample.battery_mv <= LOW_ENTER_MV) {
    printf("Pack is at or below 10.0 V — low-battery alert should start within a few seconds.\n");
  }
  return 0;
}

void nino_battery_adc_cli_register(void) {
  const esp_console_cmd_t cmd = {
      .command = "adc",
      .help = "adc | adc log | adc stop — GPIO20 pack voltage (22k / 2x3.3k)",
      .hint = NULL,
      .func = &cmd_adc,
      .argtable = NULL,
  };
  ESP_ERROR_CHECK(esp_console_cmd_register(&cmd));
}

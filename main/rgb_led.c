#include "rgb_led.h"

#include <ctype.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "driver/ledc.h"
#include "esp_check.h"
#include "esp_console.h"
#include "esp_log.h"
#include "esp_timer.h"

#include "audio_playback.h"
#include "battery_adc.h"

static const char *TAG = "rgb_led";

#define RGB_PIN_RED GPIO_NUM_2
#define RGB_PIN_GREEN GPIO_NUM_3
#define RGB_PIN_BLUE GPIO_NUM_4

#define RGB_LEDC_MODE LEDC_LOW_SPEED_MODE
#define RGB_LEDC_TIMER LEDC_TIMER_0
#define RGB_LEDC_DUTY_BITS LEDC_TIMER_8_BIT
#define RGB_LEDC_FREQ_HZ 5000
#define RGB_LEDC_MAX_DUTY RGB_LED_LEVEL_MAX

typedef struct {
  const char *name;
  gpio_num_t pin;
  ledc_channel_t channel;
  uint8_t level; /* 0-255 before global brightness */
} rgb_channel_t;

typedef struct {
  const char *name;
  uint8_t red;
  uint8_t green;
  uint8_t blue;
} rgb_named_color_t;

static rgb_channel_t s_channels[] = {
    {"red", RGB_PIN_RED, LEDC_CHANNEL_0, 0},
    {"green", RGB_PIN_GREEN, LEDC_CHANNEL_1, 0},
    {"blue", RGB_PIN_BLUE, LEDC_CHANNEL_2, 0},
};

static const rgb_named_color_t s_named_colors[] = {
    {"red", 255, 0, 0},
    {"green", 0, 255, 0},
    {"blue", 0, 0, 255},
    {"yellow", 255, 255, 0},
    {"cyan", 0, 255, 255},
    {"aqua", 0, 204, 204},
    {"magenta", 255, 0, 255},
    {"white", 255, 255, 255},
    {"orange", 255, 102, 0},
    {"purple", 153, 0, 255},
    {"violet", 102, 0, 204},
    {"pink", 255, 51, 102},
    {"warm", 255, 153, 51},
    {"cool", 51, 153, 255},
    {"lime", 128, 255, 0},
};

static uint8_t s_global_brightness = RGB_LED_LEVEL_MAX;
static nino_rgb_show_t s_show = NINO_RGB_SHOW_IDLE;
static esp_timer_handle_t s_blink_timer;
static bool s_blink_on;
static int s_blink_remaining; /* on-phases left; -1 = forever */
static uint8_t s_blink_r;
static uint8_t s_blink_g;
static uint8_t s_blink_b;

const char *nino_rgb_led_show_name(nino_rgb_show_t show)
{
  switch (show) {
  case NINO_RGB_SHOW_IDLE:
    return "idle";
  case NINO_RGB_SHOW_LISTEN:
    return "listen";
  case NINO_RGB_SHOW_TTS:
    return "tts";
  case NINO_RGB_SHOW_BATTERY:
    return "battery";
  case NINO_RGB_SHOW_MUTE:
    return "mute";
  case NINO_RGB_SHOW_OTA:
    return "ota";
  case NINO_RGB_SHOW_ERROR:
    return "error";
  case NINO_RGB_SHOW_WIFI_WAIT:
    return "wifi-wait";
  case NINO_RGB_SHOW_SERVER_WAIT:
    return "server-wait";
  default:
    return "unknown";
  }
}

static rgb_channel_t *find_channel(const char *name) {
  for (size_t i = 0; i < sizeof(s_channels) / sizeof(s_channels[0]); i++) {
    if (strcmp(name, s_channels[i].name) == 0) {
      return &s_channels[i];
    }
  }
  return NULL;
}

static const rgb_named_color_t *find_named_color(const char *name) {
  for (size_t i = 0; i < sizeof(s_named_colors) / sizeof(s_named_colors[0]); i++) {
    if (strcmp(name, s_named_colors[i].name) == 0) {
      return &s_named_colors[i];
    }
  }
  return NULL;
}

static uint8_t clamp_level(int value) {
  if (value < 0) {
    return 0;
  }
  if (value > RGB_LED_LEVEL_MAX) {
    return RGB_LED_LEVEL_MAX;
  }
  return (uint8_t)value;
}

static uint8_t scale_level(uint8_t level, uint8_t intensity) {
  return (uint8_t)(((uint16_t)level * intensity) / RGB_LED_LEVEL_MAX);
}

static uint32_t level_to_duty(uint8_t level) {
  return ((uint32_t)level * (uint32_t)s_global_brightness) / RGB_LED_LEVEL_MAX;
}

static esp_err_t apply_channel(rgb_channel_t *ch) {
  const uint32_t duty = level_to_duty(ch->level);
  ESP_RETURN_ON_ERROR(
      ledc_set_duty(RGB_LEDC_MODE, ch->channel, duty), TAG, "set duty failed");
  ESP_RETURN_ON_ERROR(ledc_update_duty(RGB_LEDC_MODE, ch->channel), TAG,
                      "update duty failed");
  return ESP_OK;
}

static esp_err_t apply_all(void) {
  for (size_t i = 0; i < sizeof(s_channels) / sizeof(s_channels[0]); i++) {
    ESP_RETURN_ON_ERROR(apply_channel(&s_channels[i]), TAG, "apply failed");
  }
  return ESP_OK;
}

static void blink_timer_stop(void)
{
  if (s_blink_timer != NULL) {
    (void)esp_timer_stop(s_blink_timer);
  }
}

static void blink_cb(void *arg)
{
  (void)arg;
  s_blink_on = !s_blink_on;
  if (s_blink_on) {
    (void)nino_rgb_led_set_rgb(s_blink_r, s_blink_g, s_blink_b);
    return;
  }
  (void)nino_rgb_led_set_rgb(0, 0, 0);
  if (s_blink_remaining < 0) {
    return;
  }
  s_blink_remaining--;
  if (s_blink_remaining <= 0) {
    blink_timer_stop();
    s_show = NINO_RGB_SHOW_IDLE;
  }
}

static esp_err_t blink_timer_start(uint64_t period_us, int on_count)
{
  if (s_blink_timer == NULL) {
    return ESP_ERR_INVALID_STATE;
  }
  s_blink_on = false;
  s_blink_remaining = on_count;
  blink_timer_stop();
  s_blink_on = true;
  (void)nino_rgb_led_set_rgb(s_blink_r, s_blink_g, s_blink_b);
  return esp_timer_start_periodic(s_blink_timer, period_us);
}

nino_rgb_show_t nino_rgb_led_current(void) { return s_show; }

esp_err_t nino_rgb_led_show(nino_rgb_show_t show)
{
  if (nino_battery_low_alert_active() && show != NINO_RGB_SHOW_BATTERY) {
    return ESP_OK;
  }
  if (nino_audio_is_muted() && show != NINO_RGB_SHOW_MUTE &&
      show != NINO_RGB_SHOW_BATTERY) {
    return ESP_OK;
  }
  if (s_blink_timer == NULL) {
    return ESP_ERR_INVALID_STATE;
  }
  blink_timer_stop();
  s_show = show;

  switch (show) {
  case NINO_RGB_SHOW_IDLE:
    nino_rgb_led_all_off();
    break;
  case NINO_RGB_SHOW_LISTEN:
    (void)nino_rgb_led_set_named("blue", RGB_LED_LEVEL_MAX);
    break;
  case NINO_RGB_SHOW_TTS:
    (void)nino_rgb_led_set_named("green", RGB_LED_LEVEL_MAX);
    break;
  case NINO_RGB_SHOW_BATTERY:
    /* Continuous red while pack is at or below 10 V (not mute solid). */
    s_blink_r = 255;
    s_blink_g = 0;
    s_blink_b = 0;
    ESP_RETURN_ON_ERROR(blink_timer_start(400 * 1000, -1), TAG, "battery blink failed");
    break;
  case NINO_RGB_SHOW_MUTE:
    (void)nino_rgb_led_set_named("red", RGB_LED_LEVEL_MAX);
    break;
  case NINO_RGB_SHOW_OTA:
    (void)nino_rgb_led_set_named("purple", RGB_LED_LEVEL_MAX);
    break;
  case NINO_RGB_SHOW_ERROR:
    (void)nino_rgb_led_set_named("red", RGB_LED_LEVEL_MAX);
    break;
  case NINO_RGB_SHOW_WIFI_WAIT:
    s_blink_r = 255;
    s_blink_g = 255;
    s_blink_b = 255;
    ESP_RETURN_ON_ERROR(blink_timer_start(400 * 1000, -1), TAG, "wifi blink failed");
    break;
  case NINO_RGB_SHOW_SERVER_WAIT:
    s_blink_r = 0;
    s_blink_g = 255;
    s_blink_b = 0;
    ESP_RETURN_ON_ERROR(blink_timer_start(400 * 1000, -1), TAG,
                        "server blink failed");
    break;
  default:
    return ESP_ERR_INVALID_ARG;
  }

  ESP_LOGI(TAG, "show -> %s", nino_rgb_led_show_name(show));
  return ESP_OK;
}

esp_err_t nino_rgb_led_init(void) {
  const ledc_timer_config_t timer = {
      .speed_mode = RGB_LEDC_MODE,
      .duty_resolution = RGB_LEDC_DUTY_BITS,
      .timer_num = RGB_LEDC_TIMER,
      .freq_hz = RGB_LEDC_FREQ_HZ,
      .clk_cfg = LEDC_AUTO_CLK,
  };
  ESP_RETURN_ON_ERROR(ledc_timer_config(&timer), TAG, "timer config failed");

  if (s_blink_timer == NULL) {
    const esp_timer_create_args_t blink_args = {
        .callback = blink_cb,
        .name = "rgb_blink",
    };
    ESP_RETURN_ON_ERROR(esp_timer_create(&blink_args, &s_blink_timer), TAG,
                        "blink timer create failed");
  }

  for (size_t i = 0; i < sizeof(s_channels) / sizeof(s_channels[0]); i++) {
    const ledc_channel_config_t ch = {
        .gpio_num = s_channels[i].pin,
        .speed_mode = RGB_LEDC_MODE,
        .channel = s_channels[i].channel,
        .intr_type = LEDC_INTR_DISABLE,
        .timer_sel = RGB_LEDC_TIMER,
        .duty = 0,
        .hpoint = 0,
        .flags =
            {
                .output_invert = 1, /* common anode: duty 0 = off, 255 = full on */
            },
    };
    ESP_RETURN_ON_ERROR(ledc_channel_config(&ch), TAG, "channel config failed");
  }

  nino_rgb_led_all_off();
  ESP_LOGI(TAG,
           "RGB PWM ready on GPIO%d/%d/%d (common anode, LEDC %d Hz, 0-%d)",
           (int)RGB_PIN_RED, (int)RGB_PIN_GREEN, (int)RGB_PIN_BLUE,
           RGB_LEDC_FREQ_HZ, RGB_LED_LEVEL_MAX);
  return ESP_OK;
}

esp_err_t nino_rgb_led_set_rgb(uint8_t red, uint8_t green, uint8_t blue) {
  s_channels[0].level = red;
  s_channels[1].level = green;
  s_channels[2].level = blue;
  return apply_all();
}

esp_err_t nino_rgb_led_set_channel_level(const char *color, uint8_t level) {
  rgb_channel_t *ch = find_channel(color);
  if (ch == NULL) {
    return ESP_ERR_INVALID_ARG;
  }
  ch->level = level;
  ESP_LOGI(TAG, "%s -> %u (global %u)", ch->name, level, s_global_brightness);
  return apply_channel(ch);
}

esp_err_t nino_rgb_led_set_brightness(uint8_t level) {
  s_global_brightness = clamp_level(level);
  ESP_LOGI(TAG, "global brightness -> %u", s_global_brightness);
  return apply_all();
}

uint8_t nino_rgb_led_get_brightness(void) { return s_global_brightness; }

esp_err_t nino_rgb_led_set_named(const char *name, uint8_t intensity) {
  const rgb_named_color_t *color = find_named_color(name);
  if (color == NULL) {
    return ESP_ERR_INVALID_ARG;
  }
  return nino_rgb_led_set_rgb(scale_level(color->red, intensity),
                              scale_level(color->green, intensity),
                              scale_level(color->blue, intensity));
}

void nino_rgb_led_all_off(void) {
  (void)nino_rgb_led_set_rgb(0, 0, 0);
}

static bool parse_level_token(const char *token, uint8_t *out_level) {
  if (token == NULL || out_level == NULL) {
    return false;
  }
  if (strcmp(token, "on") == 0 || strcmp(token, "max") == 0) {
    *out_level = RGB_LED_LEVEL_MAX;
    return true;
  }
  if (strcmp(token, "off") == 0) {
    *out_level = 0;
    return true;
  }
  char *end = NULL;
  const long value = strtol(token, &end, 10);
  if (end == token || *end != '\0' || value < 0 || value > RGB_LED_LEVEL_MAX) {
    return false;
  }
  *out_level = (uint8_t)value;
  return true;
}

static bool parse_hex_color(const char *text, uint8_t *r, uint8_t *g, uint8_t *b) {
  if (text == NULL || text[0] == '\0') {
    return false;
  }
  if (text[0] == '#') {
    text++;
  }
  if (strlen(text) != 6) {
    return false;
  }
  for (int i = 0; i < 6; i++) {
    if (!isxdigit((unsigned char)text[i])) {
      return false;
    }
  }
  char buf[3] = {0};
  buf[0] = text[0];
  buf[1] = text[1];
  *r = (uint8_t)strtoul(buf, NULL, 16);
  buf[0] = text[2];
  buf[1] = text[3];
  *g = (uint8_t)strtoul(buf, NULL, 16);
  buf[0] = text[4];
  buf[1] = text[5];
  *b = (uint8_t)strtoul(buf, NULL, 16);
  return true;
}

static unsigned level_to_percent(uint8_t level) {
  return (unsigned)((level * 100U + (RGB_LED_LEVEL_MAX / 2U)) / RGB_LED_LEVEL_MAX);
}

static void print_status(void) {
  printf("RGB PWM (GPIO 2/3/4, common anode), global brightness %u/255 (~%u%%)\n",
         s_global_brightness, level_to_percent(s_global_brightness));
  printf("  red:   %u/255 (~%u%%)\n", s_channels[0].level,
         level_to_percent(s_channels[0].level));
  printf("  green: %u/255 (~%u%%)\n", s_channels[1].level,
         level_to_percent(s_channels[1].level));
  printf("  blue:  %u/255 (~%u%%)\n", s_channels[2].level,
         level_to_percent(s_channels[2].level));
}

static void print_help(void) {
  print_status();
  printf("\nIntensity range: 0-255 (255 = maximum PWM brightness)\n");
  printf("\nUsage:\n");
  printf("  rgb status\n");
  printf("  rgb off\n");
  printf("  rgb show <scene>   — preview / force a status light\n");
  printf("      listen       solid blue      (STREAM / listen)\n");
  printf("      tts          solid green     (voice reply playing)\n");
  printf("      idle         off\n");
  printf("      battery      blink red 400 ms (low battery)\n");
  printf("      mute         solid red        (speaker muted)\n");
  printf("      ota          solid purple     (firmware update)\n");
  printf("      error        solid red       (capture/WS/error)\n");
  printf("      wifi-wait    blink white     (boot / Wi-Fi connecting)\n");
  printf("      server-wait  blink green     (Wi-Fi up, voice server down)\n");
  printf("  rgb brightness [0-255]\n");
  printf("  rgb red|green|blue [0-255|on|off|max]\n");
  printf("  rgb color <name> [0-255]   — yellow cyan magenta white orange ...\n");
  printf("  rgb mix <r> <g> <b>       — each 0-255\n");
  printf("  rgb #RRGGBB               — hex color\n");
  printf("\nNamed colors: ");
  for (size_t i = 0; i < sizeof(s_named_colors) / sizeof(s_named_colors[0]); i++) {
    printf("%s%s", i ? ", " : "", s_named_colors[i].name);
  }
  printf("\n");
}

static int cmd_rgb(int argc, char **argv) {
  if (argc < 2) {
    print_help();
    return 0;
  }

  if (strcmp(argv[1], "status") == 0) {
    print_status();
    printf("scene: %s\n", nino_rgb_led_show_name(s_show));
    return 0;
  }

  if (strcmp(argv[1], "off") == 0 || strcmp(argv[1], "idle") == 0) {
    (void)nino_rgb_led_show(NINO_RGB_SHOW_IDLE);
    printf("RGB: idle (off)\n");
    return 0;
  }

  if (strcmp(argv[1], "show") == 0) {
    if (argc < 3 || strcmp(argv[2], "list") == 0) {
      print_help();
      return 0;
    }
    nino_rgb_show_t show = NINO_RGB_SHOW_IDLE;
    if (strcmp(argv[2], "listen") == 0) {
      show = NINO_RGB_SHOW_LISTEN;
    } else if (strcmp(argv[2], "tts") == 0) {
      show = NINO_RGB_SHOW_TTS;
    } else if (strcmp(argv[2], "idle") == 0) {
      show = NINO_RGB_SHOW_IDLE;
    } else if (strcmp(argv[2], "battery") == 0) {
      show = NINO_RGB_SHOW_BATTERY;
    } else if (strcmp(argv[2], "mute") == 0) {
      show = NINO_RGB_SHOW_MUTE;
    } else if (strcmp(argv[2], "ota") == 0) {
      show = NINO_RGB_SHOW_OTA;
    } else if (strcmp(argv[2], "error") == 0) {
      show = NINO_RGB_SHOW_ERROR;
    } else if (strcmp(argv[2], "wifi-wait") == 0) {
      show = NINO_RGB_SHOW_WIFI_WAIT;
    } else if (strcmp(argv[2], "server-wait") == 0) {
      show = NINO_RGB_SHOW_SERVER_WAIT;
    } else {
      printf("Unknown scene '%s'\n", argv[2]);
      print_help();
      return 1;
    }
    esp_err_t err = nino_rgb_led_show(show);
    if (err != ESP_OK) {
      printf("rgb show failed: %s\n", esp_err_to_name(err));
      return 1;
    }
    printf("RGB scene: %s\n", nino_rgb_led_show_name(show));
    return 0;
  }

  if (strcmp(argv[1], "brightness") == 0) {
    if (argc >= 3) {
      uint8_t level = 0;
      if (!parse_level_token(argv[2], &level)) {
        printf("Usage: rgb brightness [0-255]\n");
        return 1;
      }
      esp_err_t err = nino_rgb_led_set_brightness(level);
      if (err != ESP_OK) {
        printf("brightness set failed: %s\n", esp_err_to_name(err));
        return 1;
      }
    }
    printf("global brightness: %u/255 (~%u%%)\n", nino_rgb_led_get_brightness(),
           level_to_percent(nino_rgb_led_get_brightness()));
    return 0;
  }

  if (strcmp(argv[1], "mix") == 0) {
    if (argc < 5) {
      printf("Usage: rgb mix <red> <green> <blue>   (each 0-255)\n");
      return 1;
    }
    uint8_t r = 0;
    uint8_t g = 0;
    uint8_t b = 0;
    if (!parse_level_token(argv[2], &r) || !parse_level_token(argv[3], &g) ||
        !parse_level_token(argv[4], &b)) {
      printf("mix values must be 0-255\n");
      return 1;
    }
    esp_err_t err = nino_rgb_led_set_rgb(r, g, b);
    if (err != ESP_OK) {
      printf("mix failed: %s\n", esp_err_to_name(err));
      return 1;
    }
    printf("RGB mix: r=%u g=%u b=%u\n", r, g, b);
    return 0;
  }

  if (strcmp(argv[1], "color") == 0) {
    if (argc < 3) {
      printf("Usage: rgb color <name> [0-255]\n");
      return 1;
    }
    uint8_t intensity = RGB_LED_LEVEL_MAX;
    if (argc >= 4 && !parse_level_token(argv[3], &intensity)) {
      printf("Usage: rgb color <name> [0-255|on|off|max]\n");
      return 1;
    }
    esp_err_t err = nino_rgb_led_set_named(argv[2], intensity);
    if (err == ESP_ERR_INVALID_ARG) {
      printf("Unknown color '%s'\n", argv[2]);
      print_help();
      return 1;
    }
    if (err != ESP_OK) {
      printf("color failed: %s\n", esp_err_to_name(err));
      return 1;
    }
    printf("RGB color %s @ %u/255\n", argv[2], intensity);
    return 0;
  }

  if (argv[1][0] == '#') {
    uint8_t r = 0;
    uint8_t g = 0;
    uint8_t b = 0;
    if (!parse_hex_color(argv[1], &r, &g, &b)) {
      printf("Usage: rgb #RRGGBB\n");
      return 1;
    }
    esp_err_t err = nino_rgb_led_set_rgb(r, g, b);
    if (err != ESP_OK) {
      printf("hex color failed: %s\n", esp_err_to_name(err));
      return 1;
    }
    printf("RGB hex %s -> r=%u g=%u b=%u\n", argv[1], r, g, b);
    return 0;
  }

  if (strlen(argv[1]) == 6) {
    uint8_t r = 0;
    uint8_t g = 0;
    uint8_t b = 0;
    if (parse_hex_color(argv[1], &r, &g, &b)) {
      esp_err_t err = nino_rgb_led_set_rgb(r, g, b);
      if (err != ESP_OK) {
        printf("hex color failed: %s\n", esp_err_to_name(err));
        return 1;
      }
      printf("RGB hex %s -> r=%u g=%u b=%u\n", argv[1], r, g, b);
      return 0;
    }
  }

  const rgb_named_color_t *named = find_named_color(argv[1]);
  rgb_channel_t *primary = find_channel(argv[1]);

  if (primary != NULL) {
    uint8_t level = RGB_LED_LEVEL_MAX;
    if (argc >= 3 && !parse_level_token(argv[2], &level)) {
      printf("Usage: rgb %s [0-255|on|off|max]\n", argv[1]);
      return 1;
    }
    esp_err_t err = nino_rgb_led_set_channel_level(argv[1], level);
    if (err != ESP_OK) {
      printf("channel failed: %s\n", esp_err_to_name(err));
      return 1;
    }
    printf("RGB %s -> %u/255 (~%u%%)\n", argv[1], level, level_to_percent(level));
    return 0;
  }

  if (named != NULL) {
    uint8_t intensity = RGB_LED_LEVEL_MAX;
    if (argc >= 3 && !parse_level_token(argv[2], &intensity)) {
      printf("Usage: rgb %s [0-255|on|off|max]\n", argv[1]);
      return 1;
    }
    esp_err_t err = nino_rgb_led_set_named(argv[1], intensity);
    if (err != ESP_OK) {
      printf("color failed: %s\n", esp_err_to_name(err));
      return 1;
    }
    printf("RGB %s @ %u/255\n", argv[1], intensity);
    return 0;
  }

  printf("Unknown rgb command '%s'\n", argv[1]);
  print_help();
  return 1;
}

void nino_rgb_led_cli_register(void) {
  const esp_console_cmd_t cmd = {
      .command = "rgb",
      .help =
          "rgb show listen|tts|idle|battery|mute|ota|error|wifi-wait|server-wait | "
          "rgb off | rgb status",
      .hint = NULL,
      .func = &cmd_rgb,
      .argtable = NULL,
  };
  ESP_ERROR_CHECK(esp_console_cmd_register(&cmd));
}

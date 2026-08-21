#pragma once

#include "esp_err.h"
#include "sdkconfig.h"

#if CONFIG_NINO_EYE_DISPLAY_TFT
#include "st7735.h"
#define nino_display_init()     st7735_init()
#define NINO_DISPLAY_LABEL      "ST7735 TFT 128x128"
#else
#include "ssd1351.h"
#define nino_display_init()     ssd1351_init()
#define NINO_DISPLAY_LABEL      "SSD1351 OLED 128x96"
#endif

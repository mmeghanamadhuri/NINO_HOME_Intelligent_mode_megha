#pragma once

#include <stdint.h>
#include "esp_err.h"

/*
 * 1.44" ST7735 SPI TFT (128×128 RGB565), dual-panel eye bus.
 *
 * ESP32-P4 wiring (shared SPI2, independent CS):
 *   SCK  GPIO 23    MOSI GPIO 22    DC GPIO 21    RST GPIO 20
 *   BL   GPIO 19    CS0  GPIO 32 (left)    CS1 GPIO 33 (right)
 *
 * Tie BL/LED to 3.3 V if GPIO 19 is unused — panel stays black without backlight.
 *
 * If the image is shifted 1–2 px, adjust ST7735_XSTART / ST7735_YSTART.
 * If upside-down, change ST7735_MADCTL in st7735.c (0x00 vs 0xC0).
 */
#define TFT_PIN_SCLK    23
#define TFT_PIN_MOSI    22
#define TFT_PIN_DC      21
#define TFT_PIN_RST     20
#define TFT_PIN_BL      19
#define TFT_PIN_CS0     32
#define TFT_PIN_CS1     33

#define TFT_COUNT       2
#define TFT_WIDTH       128
#define TFT_HEIGHT      128

#ifndef ST7735_XSTART
#define ST7735_XSTART   0
#endif
#ifndef ST7735_YSTART
#define ST7735_YSTART   0
#endif
#ifndef ST7735_SWAP_RB
#define ST7735_SWAP_RB  0
#endif

#define ST7735_TARGET_ALL    (-1)
#define ST7735_TARGET_LEFT   0
#define ST7735_TARGET_RIGHT  1

esp_err_t st7735_init(void);

void st7735_target(int target);
int  st7735_get_target(void);

void st7735_fill_screen(uint16_t color);
void st7735_fill_rect(int x, int y, int w, int h, uint16_t color);
void st7735_draw_pixel(int x, int y, uint16_t color);
void st7735_draw_bitmap(int x, int y, int w, int h, const uint16_t *colors);
void st7735_draw_bitmap_stride(int x, int y, int w, int h,
                               const uint16_t *colors, int stride_px);

static inline void st7735_present(void) {}
static inline void st7735_present_full(void) {}

static inline uint16_t st7735_color(uint8_t r, uint8_t g, uint8_t b)
{
#if ST7735_SWAP_RB
    uint8_t t = r;
    r = b;
    b = t;
#endif
    return (uint16_t)(((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3));
}

/* Aliases so nino_eye.c keeps calling ssd1351_* unchanged. */
#define OLED_PIN_SCLK           TFT_PIN_SCLK
#define OLED_PIN_MOSI           TFT_PIN_MOSI
#define OLED_PIN_DC             TFT_PIN_DC
#define OLED_PIN_RST            TFT_PIN_RST
#define OLED_PIN_CS0            TFT_PIN_CS0
#define OLED_PIN_CS1            TFT_PIN_CS1
#define OLED_COUNT              TFT_COUNT
#define OLED_WIDTH              TFT_WIDTH
#define OLED_HEIGHT             TFT_HEIGHT

#define SSD1351_TARGET_ALL      ST7735_TARGET_ALL
#define SSD1351_TARGET_LEFT     ST7735_TARGET_LEFT
#define SSD1351_TARGET_RIGHT    ST7735_TARGET_RIGHT

#define ssd1351_init            st7735_init
#define ssd1351_target          st7735_target
#define ssd1351_get_target      st7735_get_target
#define ssd1351_fill_screen     st7735_fill_screen
#define ssd1351_fill_rect       st7735_fill_rect
#define ssd1351_draw_pixel      st7735_draw_pixel
#define ssd1351_draw_bitmap     st7735_draw_bitmap
#define ssd1351_draw_bitmap_stride st7735_draw_bitmap_stride
#define ssd1351_present         st7735_present
#define ssd1351_present_full    st7735_present_full
#define ssd1351_color           st7735_color

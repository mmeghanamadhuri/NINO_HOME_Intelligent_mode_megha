#pragma once

#include <stdint.h>
#include "esp_err.h"

/*
 * Waveshare 1.27" RGB OLED Module (SSD1351, 128x96, 4-wire SPI).
 * Two panels share one SPI bus; only CS differs per display.
 *
 * ESP32-P4-Function-EV-Board J1 header wiring (all free I/O on this board):
 *   CLK -> GPIO23 (J1 pin 7), DIN -> GPIO22 (pin 12), DC -> GPIO21 (pin 11),
 *   RST -> GPIO20 (pin 13), CS left -> GPIO32, CS right -> GPIO33.
 *
 * CS was moved off GPIO 26/27 because those are the ESP32-P4 USB OTG FS PHY
 * D-/D+ pads; enabling the header mic (CONFIG_USB4MIC_USB_PHY_ON_HEADER) claims
 * them and blanked the eyes. GPIO 32/33 are plain digital I/O with no USB
 * overlap, so eyes and the USB mic now run together with no workaround.
 */
#define OLED_PIN_SCLK   23   /* shared CLK -> both displays */
#define OLED_PIN_MOSI   22   /* shared DIN -> both displays */
#define OLED_PIN_DC     21   /* shared DC  -> both displays */
#define OLED_PIN_RST    20   /* shared RST -> both displays */
#define OLED_PIN_CS0    32   /* CS for display 0 (left eye)  */
#define OLED_PIN_CS1    33   /* CS for display 1 (right eye) */

/* Number of OLED panels on the shared bus. */
#define OLED_COUNT      2

/* Draw target values for ssd1351_target(). */
#define SSD1351_TARGET_ALL   (-1)
#define SSD1351_TARGET_LEFT  0
#define SSD1351_TARGET_RIGHT 1

/* SSD1351 RAM is 128x128; the 1.27" panel shows 128x96. */
#define OLED_WIDTH      128
#define OLED_HEIGHT     96

/*
 * The Waveshare remap (0x74) uses a swapped colour sub-order (BGR).
 * Keeping this at 1 makes ssd1351_color(255,0,0) appear red on screen.
 * If red/blue look swapped on your panel, set this to 0 and rebuild.
 */
#ifndef SSD1351_SWAP_RB
#define SSD1351_SWAP_RB 0
#endif

esp_err_t ssd1351_init(void);

/*
 * Select which panel(s) subsequent draw calls target:
 *   SSD1351_TARGET_ALL   -> both eyes (mirrored, default)
 *   SSD1351_TARGET_LEFT  -> display 0 only
 *   SSD1351_TARGET_RIGHT -> display 1 only
 */
void ssd1351_target(int target);
int  ssd1351_get_target(void);

void ssd1351_fill_screen(uint16_t color);
void ssd1351_fill_rect(int x, int y, int w, int h, uint16_t color);
void ssd1351_draw_pixel(int x, int y, uint16_t color);
void ssd1351_draw_bitmap(int x, int y, int w, int h, const uint16_t *colors);

static inline uint16_t ssd1351_color(uint8_t r, uint8_t g, uint8_t b)
{
#if SSD1351_SWAP_RB
    uint8_t t = r;
    r = b;
    b = t;
#endif
    return (uint16_t)(((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3));
}

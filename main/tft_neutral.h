#pragma once

#include <stdbool.h>

/*
 * Standalone TFT neutral / idle eye — oval blink, direct ST7735 SPI.
 * No OLED shadow framebuffer, no nino_eye draw helpers.
 *
 * Algorithm (ESP8266/ESP12E style):
 *   1) White screen once on entry
 *   2) Draw black oval
 *   3) Blink by painting white lid rows from top + bottom (never clear a box)
 *   4) Re-open by painting black oval rows
 */
typedef bool (*tft_neutral_should_run_fn)(void);

/** Blocks until @p should_run returns false (state change / restart). */
void tft_neutral_run(tft_neutral_should_run_fn should_run);

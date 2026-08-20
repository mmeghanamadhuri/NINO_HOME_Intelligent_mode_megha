# NiNO firmware — U2D2 USB-C, USB hub, GPIO20 ADC, TFT RST GPIO6, low battery, mute LED

**Date:** 20 August 2026  
**Board:** ESP32-P4 Function EV (USB OTG host)  
**Scope:** Everything implemented in this work session, with **complete source** of every related file.

This document is generated from the live tree. Every fenced block labelled “complete file” is the full current contents of that path, not a summary.

A shorter U2D2-only note also exists at [`docs/U2D2_LATEST_AND_HUB.md`](U2D2_LATEST_AND_HUB.md). This file is the full record (U2D2 + hub + ADC + pin move + low battery + mute LED).

---

## Table of contents

1. [What was done](#1-what-was-done)
2. [File map](#2-file-map)
3. [Hardware and pin map](#3-hardware-and-pin-map)
4. [Latest USB-C U2D2](#4-latest-usb-c-u2d2)
5. [USB hub: camera + U2D2 at the same time](#5-usb-hub-camera--u2d2-at-the-same-time)
6. [TFT / OLED reset moved GPIO20 → GPIO6](#6-tft--oled-reset-moved-gpio20--gpio6)
7. [On-chip ADC battery monitor on GPIO20](#7-on-chip-adc-battery-monitor-on-gpio20)
8. [Low-battery alert](#8-low-battery-alert)
9. [Mute button with solid red LED](#9-mute-button-with-solid-red-led)
10. [Boot order in `app_main`](#10-boot-order-in-app_main)
11. [Console commands](#11-console-commands)
12. [How to test](#12-how-to-test)
13. [Complete source files](#13-complete-source-files)

---

## 1. What was done

Six pieces of work, all in firmware:

| # | Work | Result |
|---|------|--------|
| 1 | Latest **USB-C U2D2** (`VID 0x16D0` `PID 0x06A7`) | Treated as **FTDI SIO** (same FT232HL path as the old Micro-B `0403:6014`). Older firmware treated `16d0:06a7` as CDC ACM, so baud/reset never ran and the adapter never attached. |
| 2 | **USB hub** (onboard CH334, J18, or an external USB 2.0 hub) | Camera and U2D2 stay up together from the **HOST** port. The servo client scans the whole USB tree, skips hub/camera addresses, and starts **before** UVC. |
| 3 | **TFT/OLED RESET** | Moved from **GPIO20** to **GPIO6** so GPIO20 is free for ADC. |
| 4 | **On-chip ADC** | ESP32-P4 ADC1 channel on **GPIO20**, 22 kΩ / (3.3 kΩ + 3.3 kΩ) divider from the pack. |
| 5 | **Low battery** | At ≤ **10.0 V** the RGB LED blinks red and `low_battery.wav` plays every 20 s (even if the speaker is muted). Clears above **10.4 V**. |
| 6 | **Mute button** | **GPIO47** single press toggles speaker mute. While muted the RGB LED is **solid red** (not the battery blink). CLI: `speaker mute`. |

GPIO48 hardware-test (`hwtest`) is part of the same batch: one press runs motors + camera stream + RGB + TFT so hub + U2D2 + camera can be soak-tested.

---

## 2. File map

| File | Role in this work | In this doc |
|------|-------------------|-------------|
| `main/servo_dxl.c` | Identify U2D2, skip hub/camera, FTDI SIO for USB-C, bus scan, attach-before-UVC, Dynamixel worker | complete file |
| `main/servo_dxl.h` | Public servo / U2D2 API | complete file |
| `main/main.c` | USB host install, hub event loop, U2D2 before UVC, ADC/buttons init, speaker mute CLI | complete file |
| `sdkconfig.defaults` | Multi-level hubs, longer hub-port reset | complete file |
| `main/st7735.h` | TFT RST = GPIO6 | complete file |
| `main/st7735.c` | Uses `TFT_PIN_RST` for the reset pulse | complete file |
| `main/ssd1351.h` | OLED RST = GPIO6 | complete file |
| `main/ssd1351.c` | Uses `OLED_PIN_RST` for the reset pulse | complete file |
| `main/battery_adc.h` | GPIO20 ADC API + divider notes | complete file |
| `main/battery_adc.c` | ADC oneshot, pack math, low-battery task | complete file |
| `main/low_battery.wav` | Spoken “charge me” clip, embedded in firmware | binary (not text) |
| `main/CMakeLists.txt` | `esp_adc`, `battery_adc.c`, embed `low_battery.wav` | complete file |
| `main/push_buttons.h` | GPIO48 hwtest / GPIO47 mute API | complete file |
| `main/push_buttons.c` | Debounce, mute toggle, LED hand-off vs battery | complete file |
| `main/rgb_led.h` | Scenes including `BATTERY` and `MUTE` | complete file |
| `main/rgb_led.c` | Solid red mute, blinking red battery, scene priority | complete file |
| `main/audio_playback.h` | `nino_audio_set_muted` / refresh | complete file |
| `main/audio_playback.c` | Codec mute; low-battery still audible | complete file |
| `main/battery_endurance.h` | GPIO48 soak test API | complete file |
| `main/battery_endurance.c` | Motors + cam + RGB + TFT loop | complete file |
| `main/nino_eye.c` | Do not steal eyes during hwtest | complete file |

---

## 3. Hardware and pin map

### USB host

- USB OTG jumper on **HOST**.
- Camera and U2D2 on the **same USB 2.0 hub** (onboard ports 2–4, J18, or an external hub), **or** U2D2 on the direct HOST Type-A jack.
- USB-C U2D2 needs a **data** cable (one that enumerates as a COM port on a PC).
- Dynamixel side of the U2D2 still needs servo power.

ESP-IDF has **no USB Transaction Translator**. A Full-Speed-only adapter behind a High-Speed hub can fail. Supported case: U2D2 enumerates as **High Speed** next to a High-Speed camera.

### Displays vs battery ADC

| Signal | Old GPIO | New GPIO | Notes |
|--------|----------|----------|--------|
| TFT/OLED RST (shared) | **20** | **6** | Freed GPIO20 for ADC |
| Battery divider midpoint | — | **20** | ADC1_CH4, analog only |
| TFT SCK | 23 | 23 | unchanged |
| TFT MOSI | 22 | 22 | unchanged |
| TFT DC | 21 | 21 | unchanged |
| TFT CS left / right | 32 / 33 | 32 / 33 | unchanged |
| TFT BL | 19 or 3.3 V | same | Waveshare SDIO CMD uses 19 |
| RGB red / green / blue | 2 / 3 / 4 | same | common anode to 3.3 V |
| Hardware-test button | 48 | 48 | single / double / triple |
| Mute button | — | **47** | active-low to GND, internal pull-up |

**Do not use GPIO54 for the battery divider.** That pad is ESP32-C6 slave reset; its pull-up lifts the reading (~9.8 V pack looks like ~11.5 V).

**Do not wire buttons to GPIO7 (I2C SDA) or GPIO53 (speaker PA).**

### Battery divider

```
Battery+ -- 22k --+-- GPIO20
                  |
               3.3k
                  |
               3.3k
                  |
Battery- ---------+-- ESP GND
```

Formula used in firmware:

`battery_mv = adc_mv * (22000 + 6600) / 6600`

A **12 V** pack should measure about **2.77 V** from GPIO20 to GND. Never put pack voltage straight on GPIO20.

---

## 4. Latest USB-C U2D2

ROBOTIS kept the same **FT232HL**. They changed the connector to USB-C and the EEPROM ID to **`16d0:06a7`**.

| Adapter | VID:PID | Protocol in this firmware |
|---------|---------|---------------------------|
| Older Micro-B U2D2 | `0403:6014` | FTDI SIO (already worked) |
| Latest USB-C U2D2 | `16d0:06a7` | **FTDI SIO** (fix: was wrongly CDC ACM) |

`usb_device_uses_ftdi_sio()` returns true for any FTDI VID, and for ROBOTIS `16d0:06a7` unless the interface really is CDC ACM.

Success logs:

```text
U2D2 candidate 16d0:06a7 class=0xff proto=FTDI-SIO speed=HS parent=hub ...
USB serial candidate ready: ... vid=16d0 pid=06a7 proto=FTDI-SIO ...
U2D2 attached addr=... vid=16d0 pid=06a7
```

If you see `ESP-IDF has no Transaction Translator` and speed is `FS` on a hub, that hub cannot talk to this Full-Speed device. Use a hub that keeps FT232H at High Speed, or plug U2D2 into the direct HOST Type-A.

---

## 5. USB hub: camera + U2D2 at the same time

### What was broken

The servo client used to open the **first** USB address. On a hub that is usually the **hub chip** or the **UVC camera** → `device open failed` / `ESP_ERR_INVALID_STATE` / “U2D2 not connected.”

UVC was also started **before** the servo client, so isochronous camera traffic could take host channels before FTDI bulk endpoints were claimed.

### The fix

1. Peek VID/PID/class for every address. Skip hubs (`USB_CLASS_HUB`), UVC (`USB_CLASS_VIDEO`), and known hub/camera vendors.
2. Prefer ROBOTIS `16d0:06a7` over other serial adapters.
3. If another client already opened an address (`ESP_ERR_INVALID_STATE`), skip it and remember it.
4. Keep scanning after the hub enumerates children (`DXL_HUB_SETTLE_ATTEMPTS` = 80 × 150 ms).
5. Start the U2D2 USB client **before** `uvc_host_install()`.
6. Enable multi-level hubs and give downstream ports more reset / power-on time in `sdkconfig.defaults`.
7. Hub event loop (`usb_lib_task`) must run **before** any client.

Vendor skip list (hubs / cameras, not U2D2): `046d` Logitech, `03eb` Atmel, `1a40` Terminus, `05e3` Genesys, `2109` VIA, `0bda` Realtek, `1a86` QinHeng, `0424` SMSC, `174c` ASMedia, `2357` TP-Link, `214b` Huasheng.

---

## 6. TFT / OLED reset moved GPIO20 → GPIO6

`TFT_PIN_RST` / `OLED_PIN_RST` are **6**. Both drivers pulse that pin in `hardware_reset()` during `*_init()`. No other display wiring changed.

---

## 7. On-chip ADC battery monitor on GPIO20

- Unit: ADC1, mapped from GPIO20 (`adc_oneshot_io_to_channel`).
- Attenuation: 12 dB (~0–3.3 V at the pin).
- Average of 16 oneshot samples.
- Calibration: curve-fitting when the chip supports it; otherwise `raw * 3300 / 4095`.
- Pack detect: ≥ 9.5 V → 3S, ≥ 5.5 V → 2S, else 1S.
- Percent: linear between 3.3 V/cell empty and 4.2 V/cell full.
- Console: `adc`, `adc log [ms]`, `adc stop`.

---

## 8. Low-battery alert

| Constant | Value | Meaning |
|----------|-------|---------|
| `LOW_ENTER_MV` | 10000 | Enter alert at ≤ 10.0 V |
| `LOW_CLEAR_MV` | 10400 | Leave alert at ≥ 10.4 V (hysteresis) |
| `LOW_MIN_VALID_MV` | 8000 | Ignore open/unplugged divider |
| `LOW_POLL_MS` | 2000 | ADC poll |
| `LOW_SAY_MS` | 20000 | Repeat `low_battery.wav` |

On enter: set `s_low_alert`, **unmute the codec for this prompt**, RGB scene `NINO_RGB_SHOW_BATTERY` (red blink 400 ms), queue `low_battery.wav` with priority so it is heard even if another clip is playing.

On exit: clear alert, restore user mute, RGB back to mute-solid-red or idle.

User mute does **not** silence the charge prompt: `esp_codec_dev_set_out_mute` is true only when `s_user_muted && !nino_battery_low_alert_active()`.

`main/low_battery.wav` is a binary WAV. CMake embeds it as `_binary_low_battery_wav_start` / `_end`.

Embedded WAV size on disk: **109876 bytes**.

---

## 9. Mute button with solid red LED

- **GPIO47**, active-low to GND, internal pull-up.
- Single press toggles mute. Mute is evaluated immediately on release (no multi-click wait).
- While muted: RGB **solid red** (`NINO_RGB_SHOW_MUTE`).
- Low battery **wins** over mute for the LED (blink vs solid). After recovery, mute LED returns if still muted.
- Other RGB scenes (listen green, Wi-Fi, etc.) are refused while muted, except battery.
- CLI: `speaker mute`, `speaker mute on|off|toggle`, `speaker unmute`.

GPIO48 is unchanged: 1 = hardware test, 2 = DEMO_main.wav, 3 = erase Wi-Fi + BLE setup.

---

## 10. Boot order in `app_main`

1. Speaker init  
2. `nino_battery_adc_init()` (GPIO20 + low-battery task; first spoken prompt waits 8 s so the audio queue exists)  
3. Audio queue + push buttons (GPIO47/48) + hwtest init  
4. `usb_host_install`  
5. `usb_lib_task` (hub events) + 300 ms  
6. `nino_servo_dxl_start()` — U2D2 client  
7. Face-track task  
8. `uvc_host_install()` — camera last so FTDI already claimed bulk endpoints  

---

## 11. Console commands

| Command | Action |
|---------|--------|
| `adc` | One-shot GPIO20 pack voltage |
| `adc log [ms]` | Periodic log (default 1000 ms) |
| `adc stop` | Stop log |
| `speaker mute [on\|off\|toggle]` | Mute + solid red LED |
| `speaker unmute` | Unmute |
| `rgb show mute` | Preview solid red |
| `rgb show battery` | Preview blinking red |
| `hwtest` / `hwtest on\|off\|status` | GPIO48 soak (motors + cam + RGB + TFT) |

---

## 12. How to test

1. Flash firmware. HOST jumper on.  
2. Plug camera **and** USB-C U2D2 into the same USB 2.0 hub (or U2D2 on the direct HOST jack).  
3. Serial: `proto=FTDI-SIO`, `U2D2 attached`, and UVC `/stream` still opens.  
4. Meter GPIO20-to-GND ≈ 2.77 V at 12 V pack. `adc` matches.  
5. Eyes still reset (RST on GPIO6).  
6. GPIO47 mute → speaker silent, solid red LED. Unmute → LED off.  
7. Pack ≤ 10 V → red blink + charge clip (also while muted). Charge above 10.4 V → alert ends.  
8. Optional: GPIO48 once for hub soak (`cam=STREAMING`, `motors=READY`).

---

## 13. Complete source files

The rest of this document is the **full current source** of every file listed above, plus the exact `main.c` functions that wire it up.

## 13.1 `sdkconfig.defaults` (USB hub + TFT comment)

USB hub keys are lines 14–25. Line 33 still mentions GPIO 20 in a comment from before the RST move; the live pin is GPIO **6** (`TFT_PIN_RST` in `st7735.h`).

### `sdkconfig.defaults` — complete file (35 lines)

Copied from the tree as of this document. Do not edit here; edit the source file.

```ini
# Minimal defaults for ESP32-P4 USB host camera bring-up.
CONFIG_FREERTOS_HZ=1000
CONFIG_COMPILER_OPTIMIZATION_PERF=y
CONFIG_SPIRAM=y
CONFIG_LOG_COLORS=y
CONFIG_ESPTOOLPY_FLASHSIZE_16MB=y
CONFIG_ESP_WIFI_SOFTAP_SUPPORT=y
# CONFIG_PARTITION_TABLE_SINGLE_APP_LARGE is not set
CONFIG_PARTITION_TABLE_CUSTOM=y
CONFIG_PARTITION_TABLE_CUSTOM_FILENAME="partitions.csv"
CONFIG_PARTITION_TABLE_FILENAME="partitions.csv"
CONFIG_FREERTOS_GENERATE_RUN_TIME_STATS=y
CONFIG_FREERTOS_VTASKLIST_INCLUDE_COREID=y
CONFIG_USB_HOST_CONTROL_TRANSFER_MAX_SIZE=4096
CONFIG_USB_HOST_HW_BUFFER_BIAS_BALANCED=y
CONFIG_USB_HOST_HUBS_SUPPORTED=y
CONFIG_USB_HOST_HUB_MULTI_LEVEL=y
# Give flaky hub children more time (CHECK_SHORT_DEV_DESC / downstream reset).
CONFIG_USB_HOST_DEBOUNCE_DELAY_MS=500
CONFIG_USB_HOST_RESET_HOLD_MS=100
CONFIG_USB_HOST_RESET_RECOVERY_MS=100
CONFIG_USB_HOST_EXT_PORT_RESET_ATTEMPTS=3
CONFIG_USB_HOST_EXT_PORT_RESET_RECOVERY_DELAY_MS=100
CONFIG_USB_HOST_EXT_PORT_CUSTOM_POWER_ON_DELAY_ENABLE=y
CONFIG_USB_HOST_EXT_PORT_CUSTOM_POWER_ON_DELAY_MS=250
CONFIG_IDF_EXPERIMENTAL_FEATURES=y
CONFIG_ESP_HOSTED_MEMPOOL_PREFER_SPIRAM=y
CONFIG_ESP_HOSTED_SDIO_TX_Q_SIZE=10
CONFIG_ESP_HOSTED_SDIO_RX_Q_SIZE=10
CONFIG_ESP_HOSTED_DFLT_TASK_FROM_SPIRAM=y
CONFIG_ESP_CONSOLE_SECONDARY_NONE=y

# ST7735 TFT eyes (GPIO 23/22/21/20/32/33). BL tied to 3.3 V — GPIO 19 is SDIO CMD on Waveshare.
CONFIG_NINO_EYE_DISPLAY_TFT=y
CONFIG_NINO_ST7735_BL_HARDCODED_3V3=y
```

## 13.2 Build registration

`esp_adc` is required. `battery_adc.c` and `battery_endurance.c` are compiled. `low_battery.wav` is in `EMBED_FILES`.

### `main/CMakeLists.txt` — complete file (26 lines)

Copied from the tree as of this document. Do not edit here; edit the source file.

```cmake
set(main_requires esp_psram esp_wifi esp_event esp_netif nvs_flash esp_http_server esp_http_client mdns
    console esp32_p4_function_ev_board esp_websocket_client driver usb
    esp_hosted esp_adc)

set(main_priv_requires bt)

set(main_srcs
    servo_recplay.c mic_input.c wifi_prov_ble.c servo_dxl.c servo_motion.c
    face_tracker.c face_detect.cpp main.c audio_playback.c audio_capture.c audio_queue.c
    voice_ws_client.c voice_assist.c nino_eye.c push_buttons.c rgb_led.c
    music_stream.c battery_adc.c battery_endurance.c)

if(CONFIG_NINO_EYE_DISPLAY_TFT)
    list(APPEND main_srcs st7735.c tft_neutral.c)
else()
    list(APPEND main_srcs ssd1351.c)
endif()

idf_component_register(
    SRCS ${main_srcs}
    INCLUDE_DIRS "."
    EMBED_FILES "beep.wav" "WIFI.wav" "Hello-home.wav" "Wifi_Unable.wav" "NiNO-Home_Wifi.wav"
                "schedule_dinnner.wav" "Bday_Surprise.wav" "DEMO_main.wav" "low_battery.wav"
    REQUIRES ${main_requires}
    PRIV_REQUIRES ${main_priv_requires}
)
```

## 13.3 Display reset pin (GPIO6)

### `main/st7735.h` — complete file (95 lines)

Copied from the tree as of this document. Do not edit here; edit the source file.

```c
#pragma once

#include <stdint.h>
#include "esp_err.h"

/*
 * 1.44" ST7735 SPI TFT (128×128 RGB565), dual-panel eye bus.
 *
 * ESP32-P4 wiring (shared SPI2, independent CS):
 *   SCK  GPIO 23    MOSI GPIO 22    DC GPIO 21    RST GPIO 6
 *   BL   GPIO 19    CS0  GPIO 32 (left)    CS1 GPIO 33 (right)
 * Battery ADC is GPIO20; keep TFT RST on GPIO6. GPIO54 is C6 slave reset.
 *
 * Tie BL/LED to 3.3 V if GPIO 19 is unused — panel stays black without backlight.
 *
 * If the image is shifted 1–2 px, adjust ST7735_XSTART / ST7735_YSTART.
 * If upside-down, change ST7735_MADCTL in st7735.c (0x00 vs 0xC0).
 */
#define TFT_PIN_SCLK    23
#define TFT_PIN_MOSI    22
#define TFT_PIN_DC      21
#define TFT_PIN_RST     6
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
```

### `main/st7735.c` — complete file (442 lines)

Copied from the tree as of this document. Do not edit here; edit the source file.

```c
#include "st7735.h"

#include <string.h>
#include "sdkconfig.h"
#include "driver/gpio.h"
#include "driver/spi_master.h"
#include "esp_check.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

static const char *TAG = "st7735";

/* Memory access control — 0x00 portrait; use 0xC0 (MX|MY) if upside-down. */
#define ST7735_MADCTL           0x00

#define ST7735_CMD_SWRESET      0x01
#define ST7735_CMD_SLPOUT       0x11
#define ST7735_CMD_NORON        0x13
#define ST7735_CMD_INVOFF       0x20
#define ST7735_CMD_DISPON       0x29
#define ST7735_CMD_CASET        0x2A
#define ST7735_CMD_RASET        0x2B
#define ST7735_CMD_RAMWR        0x2C
#define ST7735_CMD_MADCTL       0x36
#define ST7735_CMD_COLMOD       0x3A
#define ST7735_CMD_FRMCTR1      0xB1
#define ST7735_CMD_FRMCTR2      0xB2
#define ST7735_CMD_FRMCTR3      0xB3
#define ST7735_CMD_INVCTR       0xB4
#define ST7735_CMD_PWCTR1       0xC0
#define ST7735_CMD_PWCTR2       0xC1
#define ST7735_CMD_PWCTR3       0xC2
#define ST7735_CMD_PWCTR4       0xC3
#define ST7735_CMD_PWCTR5       0xC4
#define ST7735_CMD_VMCTR1       0xC5
#define ST7735_CMD_GMCTRP1      0xE0
#define ST7735_CMD_GMCTRN1      0xE1

#define SPI_HOST_ID             SPI2_HOST
#define SPI_CLOCK_HZ            (26 * 1000 * 1000)
#define CHUNK_PIXELS            2048

static spi_device_handle_t s_spi;
static int s_target = ST7735_TARGET_ALL;
static uint8_t s_color_chunk[CHUNK_PIXELS * 2];

static void cs_idle(void)
{
    gpio_set_level(TFT_PIN_CS0, 1);
    gpio_set_level(TFT_PIN_CS1, 1);
}

static void cs_begin(void)
{
    if (s_target == ST7735_TARGET_ALL) {
        gpio_set_level(TFT_PIN_CS0, 0);
        gpio_set_level(TFT_PIN_CS1, 0);
    } else if (s_target == ST7735_TARGET_LEFT) {
        gpio_set_level(TFT_PIN_CS0, 0);
        gpio_set_level(TFT_PIN_CS1, 1);
    } else {
        gpio_set_level(TFT_PIN_CS0, 1);
        gpio_set_level(TFT_PIN_CS1, 0);
    }
}

static void cs_end(void)
{
    cs_idle();
}

static void bus_tx(const void *data, size_t bits, int dc_level)
{
    gpio_set_level(TFT_PIN_DC, dc_level);
    spi_transaction_t trans = {
        .length = bits,
        .tx_buffer = data,
    };
    cs_begin();
    ESP_ERROR_CHECK(spi_device_polling_transmit(s_spi, &trans));
    cs_end();
}

static void dev_write_cmd(uint8_t cmd)
{
    bus_tx(&cmd, 8, 0);
}

static void dev_write_data(const uint8_t *data, size_t len)
{
    if (len == 0) {
        return;
    }
    bus_tx(data, len * 8, 1);
}

static void dev_write_data_byte(uint8_t value)
{
    dev_write_data(&value, 1);
}

static void dev_set_window(int x0, int y0, int x1, int y1)
{
    x0 += ST7735_XSTART;
    x1 += ST7735_XSTART;
    y0 += ST7735_YSTART;
    y1 += ST7735_YSTART;

    dev_write_cmd(ST7735_CMD_CASET);
    dev_write_data_byte(0x00);
    dev_write_data_byte((uint8_t)x0);
    dev_write_data_byte(0x00);
    dev_write_data_byte((uint8_t)x1);

    dev_write_cmd(ST7735_CMD_RASET);
    dev_write_data_byte(0x00);
    dev_write_data_byte((uint8_t)y0);
    dev_write_data_byte(0x00);
    dev_write_data_byte((uint8_t)y1);

    dev_write_cmd(ST7735_CMD_RAMWR);
}

void st7735_target(int target)
{
    if (target != ST7735_TARGET_ALL && (target < 0 || target >= TFT_COUNT)) {
        return;
    }
    s_target = target;
}

int st7735_get_target(void)
{
    return s_target;
}

static esp_err_t init_spi_bus(void)
{
    gpio_config_t cs_io = {
        .pin_bit_mask = (1ULL << TFT_PIN_CS0) | (1ULL << TFT_PIN_CS1),
        .mode = GPIO_MODE_OUTPUT,
        .pull_up_en = GPIO_PULLUP_DISABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type = GPIO_INTR_DISABLE,
    };
    ESP_RETURN_ON_ERROR(gpio_config(&cs_io), TAG, "cs gpio failed");
    cs_idle();

    spi_bus_config_t buscfg = {
        .mosi_io_num = TFT_PIN_MOSI,
        .miso_io_num = -1,
        .sclk_io_num = TFT_PIN_SCLK,
        .quadwp_io_num = -1,
        .quadhd_io_num = -1,
        .max_transfer_sz = TFT_WIDTH * TFT_HEIGHT * 2,
    };

    esp_err_t err = spi_bus_initialize(SPI_HOST_ID, &buscfg, SPI_DMA_CH_AUTO);
    if (err != ESP_OK && err != ESP_ERR_INVALID_STATE) {
        return err;
    }

    if (s_spi != NULL) {
        spi_bus_remove_device(s_spi);
        s_spi = NULL;
    }

    spi_device_interface_config_t devcfg = {
        .clock_speed_hz = SPI_CLOCK_HZ,
        .mode = 0,
        .spics_io_num = -1,
        .queue_size = 1,
        .flags = SPI_DEVICE_NO_DUMMY,
    };
    return spi_bus_add_device(SPI_HOST_ID, &devcfg, &s_spi);
}

static void hardware_reset(void)
{
    gpio_set_level(TFT_PIN_RST, 1);
    vTaskDelay(pdMS_TO_TICKS(10));
    gpio_set_level(TFT_PIN_RST, 0);
    vTaskDelay(pdMS_TO_TICKS(20));
    gpio_set_level(TFT_PIN_RST, 1);
    vTaskDelay(pdMS_TO_TICKS(120));
}

static void backlight_on(void)
{
#if !CONFIG_NINO_ST7735_BL_HARDCODED_3V3
    gpio_set_level(TFT_PIN_BL, 1);
#endif
}

static void init_panel(void)
{
    dev_write_cmd(ST7735_CMD_SWRESET);
    vTaskDelay(pdMS_TO_TICKS(150));

    dev_write_cmd(ST7735_CMD_SLPOUT);
    vTaskDelay(pdMS_TO_TICKS(120));

    dev_write_cmd(ST7735_CMD_MADCTL);
    dev_write_data_byte(ST7735_MADCTL);

    dev_write_cmd(ST7735_CMD_COLMOD);
    dev_write_data_byte(0x05);

    dev_write_cmd(ST7735_CMD_FRMCTR1);
    dev_write_data_byte(0x01);
    dev_write_data_byte(0x2C);
    dev_write_data_byte(0x2D);

    dev_write_cmd(ST7735_CMD_FRMCTR2);
    dev_write_data_byte(0x01);
    dev_write_data_byte(0x2C);
    dev_write_data_byte(0x2D);

    dev_write_cmd(ST7735_CMD_FRMCTR3);
    dev_write_data_byte(0x01);
    dev_write_data_byte(0x2C);
    dev_write_data_byte(0x2D);
    dev_write_data_byte(0x01);
    dev_write_data_byte(0x2C);
    dev_write_data_byte(0x2D);

    dev_write_cmd(ST7735_CMD_INVCTR);
    dev_write_data_byte(0x07);

    dev_write_cmd(ST7735_CMD_PWCTR1);
    dev_write_data_byte(0xA2);
    dev_write_data_byte(0x02);
    dev_write_data_byte(0x84);

    dev_write_cmd(ST7735_CMD_PWCTR2);
    dev_write_data_byte(0xC5);

    dev_write_cmd(ST7735_CMD_PWCTR3);
    dev_write_data_byte(0x0A);
    dev_write_data_byte(0x00);

    dev_write_cmd(ST7735_CMD_PWCTR4);
    dev_write_data_byte(0x8A);
    dev_write_data_byte(0x2A);

    dev_write_cmd(ST7735_CMD_PWCTR5);
    dev_write_data_byte(0x8A);
    dev_write_data_byte(0xEE);

    dev_write_cmd(ST7735_CMD_VMCTR1);
    dev_write_data_byte(0x0E);

    dev_write_cmd(ST7735_CMD_INVOFF);

    dev_write_cmd(ST7735_CMD_GMCTRP1);
    dev_write_data((const uint8_t[]){
        0x02, 0x1C, 0x07, 0x12, 0x37, 0x32, 0x29, 0x2D,
        0x29, 0x25, 0x2B, 0x39, 0x00, 0x01, 0x03, 0x10,
    }, 16);

    dev_write_cmd(ST7735_CMD_GMCTRN1);
    dev_write_data((const uint8_t[]){
        0x03, 0x1D, 0x07, 0x06, 0x2E, 0x2C, 0x29, 0x2D,
        0x2E, 0x2E, 0x37, 0x3F, 0x00, 0x00, 0x02, 0x10,
    }, 16);

    dev_write_cmd(ST7735_CMD_NORON);
    vTaskDelay(pdMS_TO_TICKS(10));

    dev_write_cmd(ST7735_CMD_DISPON);
    vTaskDelay(pdMS_TO_TICKS(100));
}

esp_err_t st7735_init(void)
{
    uint64_t gpio_mask = (1ULL << TFT_PIN_DC) | (1ULL << TFT_PIN_RST);
#if !CONFIG_NINO_ST7735_BL_HARDCODED_3V3
    gpio_mask |= (1ULL << TFT_PIN_BL);
#endif
    gpio_config_t io = {
        .pin_bit_mask = gpio_mask,
        .mode = GPIO_MODE_OUTPUT,
        .pull_up_en = GPIO_PULLUP_DISABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type = GPIO_INTR_DISABLE,
    };
    ESP_RETURN_ON_ERROR(gpio_config(&io), TAG, "gpio config failed");

    gpio_set_level(TFT_PIN_DC, 1);
    gpio_set_level(TFT_PIN_RST, 1);
#if !CONFIG_NINO_ST7735_BL_HARDCODED_3V3
    gpio_set_level(TFT_PIN_BL, 0);
#endif

    ESP_RETURN_ON_ERROR(init_spi_bus(), TAG, "spi init failed");

    hardware_reset();

    for (int i = 0; i < TFT_COUNT; i++) {
        const int saved = s_target;
        s_target = i;
        init_panel();
        s_target = saved;
        ESP_LOGI(TAG, "panel %d init done", i);
    }

    backlight_on();

    s_target = ST7735_TARGET_ALL;
    st7735_fill_screen(0x0000);

#if CONFIG_NINO_ST7735_BL_HARDCODED_3V3
    ESP_LOGI(TAG, "ST7735 ready: %d panel(s) %dx%d (BL hardwired 3.3 V, GPIO%d free for SDIO)",
             TFT_COUNT, TFT_WIDTH, TFT_HEIGHT, TFT_PIN_BL);
#else
    ESP_LOGI(TAG, "ST7735 ready: %d panel(s) %dx%d (BL GPIO%d)",
             TFT_COUNT, TFT_WIDTH, TFT_HEIGHT, TFT_PIN_BL);
#endif
    return ESP_OK;
}

void st7735_fill_rect(int x, int y, int w, int h, uint16_t color)
{
    if (w <= 0 || h <= 0) {
        return;
    }

    if (x < 0) {
        w += x;
        x = 0;
    }
    if (y < 0) {
        h += y;
        y = 0;
    }
    if (x + w > TFT_WIDTH) {
        w = TFT_WIDTH - x;
    }
    if (y + h > TFT_HEIGHT) {
        h = TFT_HEIGHT - y;
    }
    if (w <= 0 || h <= 0) {
        return;
    }

    const uint8_t hi = (uint8_t)(color >> 8);
    const uint8_t lo = (uint8_t)(color & 0xFF);

    const size_t total = (size_t)w * (size_t)h;
    size_t prefill = total > CHUNK_PIXELS ? CHUNK_PIXELS : total;
    for (size_t i = 0; i < prefill; i++) {
        s_color_chunk[i * 2] = hi;
        s_color_chunk[(i * 2) + 1] = lo;
    }

    dev_set_window(x, y, x + w - 1, y + h - 1);

    size_t remaining = total;
    while (remaining > 0) {
        size_t batch = remaining > CHUNK_PIXELS ? CHUNK_PIXELS : remaining;
        dev_write_data(s_color_chunk, batch * 2);
        remaining -= batch;
    }
}

void st7735_fill_screen(uint16_t color)
{
    st7735_fill_rect(0, 0, TFT_WIDTH, TFT_HEIGHT, color);
}

void st7735_draw_pixel(int x, int y, uint16_t color)
{
    if (x < 0 || y < 0 || x >= TFT_WIDTH || y >= TFT_HEIGHT) {
        return;
    }

    const uint8_t bytes[2] = { (uint8_t)(color >> 8), (uint8_t)(color & 0xFF) };
    dev_set_window(x, y, x, y);
    dev_write_data(bytes, sizeof(bytes));
}

void st7735_draw_bitmap(int x, int y, int w, int h, const uint16_t *colors)
{
    if (colors == NULL || w <= 0 || h <= 0) {
        return;
    }

    if (x < 0 || y < 0 || x + w > TFT_WIDTH || y + h > TFT_HEIGHT) {
        return;
    }

    dev_set_window(x, y, x + w - 1, y + h - 1);

    for (int row = 0; row < h; row++) {
        const uint16_t *src = colors + (size_t)row * (size_t)w;
        size_t remaining = (size_t)w;
        size_t source_offset = 0;
        while (remaining > 0) {
            size_t batch = remaining > CHUNK_PIXELS ? CHUNK_PIXELS : remaining;
            for (size_t i = 0; i < batch; i++) {
                uint16_t c = src[source_offset + i];
                s_color_chunk[i * 2] = (uint8_t)(c >> 8);
                s_color_chunk[(i * 2) + 1] = (uint8_t)(c & 0xFF);
            }
            dev_write_data(s_color_chunk, batch * 2);
            source_offset += batch;
            remaining -= batch;
        }
    }
}

void st7735_draw_bitmap_stride(int x, int y, int w, int h,
                               const uint16_t *colors, int stride_px)
{
    if (colors == NULL || w <= 0 || h <= 0 || stride_px < w) {
        return;
    }

    if (x < 0 || y < 0 || x + w > TFT_WIDTH || y + h > TFT_HEIGHT) {
        return;
    }

    dev_set_window(x, y, x + w - 1, y + h - 1);

    for (int row = 0; row < h; row++) {
        const uint16_t *src = colors + (size_t)row * (size_t)stride_px;
        size_t remaining = (size_t)w;
        size_t source_offset = 0;
        while (remaining > 0) {
            size_t batch = remaining > CHUNK_PIXELS ? CHUNK_PIXELS : remaining;
            for (size_t i = 0; i < batch; i++) {
                uint16_t c = src[source_offset + i];
                s_color_chunk[i * 2] = (uint8_t)(c >> 8);
                s_color_chunk[(i * 2) + 1] = (uint8_t)(c & 0xFF);
            }
            dev_write_data(s_color_chunk, batch * 2);
            source_offset += batch;
            remaining -= batch;
        }
    }
}
```

### `main/ssd1351.h` — complete file (70 lines)

Copied from the tree as of this document. Do not edit here; edit the source file.

```c
#pragma once

#include <stdint.h>
#include "esp_err.h"

/*
 * Waveshare 1.27" RGB OLED Module (SSD1351, 128x96, 4-wire SPI).
 * Two panels share one SPI bus; only CS differs per display.
 *
 * ESP32-P4-Function-EV-Board J1 header wiring (all free I/O on this board):
 *   CLK -> GPIO23 (J1 pin 7), DIN -> GPIO22 (pin 12), DC -> GPIO21 (pin 11),
 *   RST -> GPIO6, CS left -> GPIO32, CS right -> GPIO33.
 * Battery ADC is GPIO20; keep OLED RST on GPIO6. GPIO54 is C6 slave reset.
 *
 * CS was moved off GPIO 26/27 because those are the ESP32-P4 USB OTG FS PHY
 * D-/D+ pads. GPIO 32/33 are plain digital I/O with no USB overlap.
 */
#define OLED_PIN_SCLK   23   /* shared CLK -> both displays */
#define OLED_PIN_MOSI   22   /* shared DIN -> both displays */
#define OLED_PIN_DC     21   /* shared DC  -> both displays */
#define OLED_PIN_RST    6    /* shared RST -> both displays */
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
```

### `main/ssd1351.c` — complete file (415 lines)

Copied from the tree as of this document. Do not edit here; edit the source file.

```c
#include "ssd1351.h"

#include <string.h>
#include "driver/gpio.h"
#include "driver/spi_master.h"
#include "esp_check.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

static const char *TAG = "ssd1351";

/* SSD1351 command set */
#define SSD1351_CMD_SETCOLUMN     0x15
#define SSD1351_CMD_SETROW        0x75
#define SSD1351_CMD_WRITERAM      0x5C
#define SSD1351_CMD_SETREMAP      0xA0
#define SSD1351_CMD_STARTLINE     0xA1
#define SSD1351_CMD_DISPLAYOFFSET 0xA2
#define SSD1351_CMD_NORMALDISPLAY 0xA6
#define SSD1351_CMD_DISPLAYALLOFF 0xA4
#define SSD1351_CMD_DISPLAYOFF    0xAE
#define SSD1351_CMD_DISPLAYON     0xAF
#define SSD1351_CMD_FUNCTIONSEL   0xAB
#define SSD1351_CMD_PRECHARGE     0xB1
#define SSD1351_CMD_DISPLAYENH    0xB2
#define SSD1351_CMD_CLOCKDIV      0xB3
#define SSD1351_CMD_SETVSL        0xB4
#define SSD1351_CMD_SETGPIO       0xB5
#define SSD1351_CMD_PRECHARGE2    0xB6
#define SSD1351_CMD_VCOMH         0xBE
#define SSD1351_CMD_PRECHARGEV    0xBB
#define SSD1351_CMD_CONTRASTABC   0xC1
#define SSD1351_CMD_CONTRASTMAST  0xC7
#define SSD1351_CMD_MUXRATIO      0xCA
#define SSD1351_CMD_COMMANDLOCK   0xFD

#define SPI_HOST_ID     SPI2_HOST
#define SPI_CLOCK_HZ    (20 * 1000 * 1000)
#define CHUNK_PIXELS    2048

static const int s_cs_pins[OLED_COUNT] = { OLED_PIN_CS0, OLED_PIN_CS1 };
static spi_device_handle_t s_spi[OLED_COUNT];
static int s_target = SSD1351_TARGET_ALL;
static uint8_t s_color_chunk[CHUNK_PIXELS * 2];
static bool s_bus_ready = false;

static void dev_write_cmd(spi_device_handle_t dev, uint8_t cmd)
{
    gpio_set_level(OLED_PIN_DC, 0);

    spi_transaction_t trans = {
        .length = 8,
        .tx_buffer = &cmd,
    };
    ESP_ERROR_CHECK(spi_device_polling_transmit(dev, &trans));
}

static void dev_write_data(spi_device_handle_t dev, const uint8_t *data, size_t len)
{
    if (len == 0) {
        return;
    }

    gpio_set_level(OLED_PIN_DC, 1);

    spi_transaction_t trans = {
        .length = len * 8,
        .tx_buffer = data,
    };
    ESP_ERROR_CHECK(spi_device_polling_transmit(dev, &trans));
}

static void dev_write_data_byte(spi_device_handle_t dev, uint8_t value)
{
    dev_write_data(dev, &value, 1);
}

static void dev_set_window(spi_device_handle_t dev, int x0, int y0, int x1, int y1)
{
    dev_write_cmd(dev, SSD1351_CMD_SETCOLUMN);
    dev_write_data_byte(dev, (uint8_t)x0);
    dev_write_data_byte(dev, (uint8_t)x1);

    dev_write_cmd(dev, SSD1351_CMD_SETROW);
    dev_write_data_byte(dev, (uint8_t)y0);
    dev_write_data_byte(dev, (uint8_t)y1);

    dev_write_cmd(dev, SSD1351_CMD_WRITERAM);
}

static int target_first(void)
{
    return (s_target == SSD1351_TARGET_ALL) ? 0 : s_target;
}

static int target_last(void)
{
    return (s_target == SSD1351_TARGET_ALL) ? (OLED_COUNT - 1) : s_target;
}

void ssd1351_target(int target)
{
    if (target != SSD1351_TARGET_ALL && (target < 0 || target >= OLED_COUNT)) {
        return;
    }
    s_target = target;
}

int ssd1351_get_target(void)
{
    return s_target;
}

static esp_err_t attach_spi_devices(void)
{
    for (int i = 0; i < OLED_COUNT; i++) {
        if (s_spi[i] != NULL) {
            spi_bus_remove_device(s_spi[i]);
            s_spi[i] = NULL;
        }

        spi_device_interface_config_t devcfg = {
            .clock_speed_hz = SPI_CLOCK_HZ,
            .mode = 0,
            .spics_io_num = s_cs_pins[i],
            .queue_size = 1,
            .flags = SPI_DEVICE_NO_DUMMY,
        };

        esp_err_t err = spi_bus_add_device(SPI_HOST_ID, &devcfg, &s_spi[i]);
        if (err != ESP_OK) {
            return err;
        }
    }

    return ESP_OK;
}

static esp_err_t init_spi_bus(void)
{
    spi_bus_config_t buscfg = {
        .mosi_io_num = OLED_PIN_MOSI,
        .miso_io_num = -1,
        .sclk_io_num = OLED_PIN_SCLK,
        .quadwp_io_num = -1,
        .quadhd_io_num = -1,
        .max_transfer_sz = OLED_WIDTH * OLED_HEIGHT * 2,
    };

    esp_err_t err = spi_bus_initialize(SPI_HOST_ID, &buscfg, SPI_DMA_CH_AUTO);
    if (err != ESP_OK && err != ESP_ERR_INVALID_STATE) {
        return err;
    }

    return attach_spi_devices();
}

static void hardware_reset(void)
{
    gpio_set_level(OLED_PIN_RST, 1);
    vTaskDelay(pdMS_TO_TICKS(10));
    gpio_set_level(OLED_PIN_RST, 0);
    vTaskDelay(pdMS_TO_TICKS(20));
    gpio_set_level(OLED_PIN_RST, 1);
    vTaskDelay(pdMS_TO_TICKS(120));
}

static void init_panel(spi_device_handle_t dev)
{
    dev_write_cmd(dev, SSD1351_CMD_COMMANDLOCK);
    dev_write_data_byte(dev, 0x12);
    dev_write_cmd(dev, SSD1351_CMD_COMMANDLOCK);
    dev_write_data_byte(dev, 0xB1);

    dev_write_cmd(dev, SSD1351_CMD_DISPLAYOFF);

    dev_write_cmd(dev, SSD1351_CMD_CLOCKDIV);
    dev_write_data_byte(dev, 0xF1);

    /*
     * 1.27" 128x96 panel mapping (matches the known-good fbtft/Waveshare
     * sequence for this module): full 128 MUX, display start line = 96, and
     * zero display offset. This aligns RAM rows 0..95 to the visible 96 rows
     * top-to-bottom (earlier MUX 0x5F + offset 0x60 squeezed it into a band).
     */
    dev_write_cmd(dev, SSD1351_CMD_MUXRATIO);
    dev_write_data_byte(dev, 0x7F);

    dev_write_cmd(dev, SSD1351_CMD_DISPLAYOFFSET);
    dev_write_data_byte(dev, 0x00);

    dev_write_cmd(dev, SSD1351_CMD_STARTLINE);
    dev_write_data_byte(dev, 0x60);

    /* 0x74: 65k colour, horizontal increment, COM split + scan as per Waveshare. */
    dev_write_cmd(dev, SSD1351_CMD_SETREMAP);
    dev_write_data_byte(dev, 0x74);

    dev_write_cmd(dev, SSD1351_CMD_SETGPIO);
    dev_write_data_byte(dev, 0x00);

    dev_write_cmd(dev, SSD1351_CMD_FUNCTIONSEL);
    dev_write_data_byte(dev, 0x01); /* internal VDD regulator */

    dev_write_cmd(dev, SSD1351_CMD_PRECHARGE);
    dev_write_data_byte(dev, 0x32);

    dev_write_cmd(dev, SSD1351_CMD_DISPLAYENH);
    dev_write_data_byte(dev, 0xA4);
    dev_write_data_byte(dev, 0x00);
    dev_write_data_byte(dev, 0x00);

    dev_write_cmd(dev, SSD1351_CMD_SETVSL);
    dev_write_data_byte(dev, 0xA0);
    dev_write_data_byte(dev, 0xB5);
    dev_write_data_byte(dev, 0x55);

    dev_write_cmd(dev, SSD1351_CMD_PRECHARGEV);
    dev_write_data_byte(dev, 0x17);

    dev_write_cmd(dev, SSD1351_CMD_PRECHARGE2);
    dev_write_data_byte(dev, 0x01);

    dev_write_cmd(dev, SSD1351_CMD_VCOMH);
    dev_write_data_byte(dev, 0x05);

    dev_write_cmd(dev, SSD1351_CMD_CONTRASTABC);
    dev_write_data_byte(dev, 0xC8);
    dev_write_data_byte(dev, 0x80);
    dev_write_data_byte(dev, 0xC8);

    dev_write_cmd(dev, SSD1351_CMD_CONTRASTMAST);
    dev_write_data_byte(dev, 0x0F);

    dev_write_cmd(dev, SSD1351_CMD_NORMALDISPLAY);

    dev_write_cmd(dev, SSD1351_CMD_DISPLAYON);
    vTaskDelay(pdMS_TO_TICKS(50));
}

esp_err_t ssd1351_init(void)
{
    gpio_config_t io = {
        .pin_bit_mask = (1ULL << OLED_PIN_DC) | (1ULL << OLED_PIN_RST),
        .mode = GPIO_MODE_OUTPUT,
        .pull_up_en = GPIO_PULLUP_DISABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type = GPIO_INTR_DISABLE,
    };
    ESP_RETURN_ON_ERROR(gpio_config(&io), TAG, "gpio config failed");

    gpio_set_level(OLED_PIN_DC, 1);
    gpio_set_level(OLED_PIN_RST, 1);

    ESP_RETURN_ON_ERROR(init_spi_bus(), TAG, "spi init failed");

    /* RST is shared, so one reset pulse covers all panels. */
    hardware_reset();
    for (int i = 0; i < OLED_COUNT; i++) {
        init_panel(s_spi[i]);
    }

    s_target = SSD1351_TARGET_ALL;
    ssd1351_fill_screen(0x0000);

    s_bus_ready = true;
    ESP_LOGI(TAG, "SSD1351 ready: %d panel(s) %dx%d", OLED_COUNT, OLED_WIDTH, OLED_HEIGHT);
    return ESP_OK;
}

void ssd1351_fill_rect(int x, int y, int w, int h, uint16_t color)
{
    if (w <= 0 || h <= 0) {
        return;
    }

    if (x < 0) {
        w += x;
        x = 0;
    }
    if (y < 0) {
        h += y;
        y = 0;
    }
    if (x + w > OLED_WIDTH) {
        w = OLED_WIDTH - x;
    }
    if (y + h > OLED_HEIGHT) {
        h = OLED_HEIGHT - y;
    }
    if (w <= 0 || h <= 0) {
        return;
    }

    const uint8_t hi = (uint8_t)(color >> 8);
    const uint8_t lo = (uint8_t)(color & 0xFF);

    const size_t total = (size_t)w * (size_t)h;
    size_t prefill = total > CHUNK_PIXELS ? CHUNK_PIXELS : total;
    for (size_t i = 0; i < prefill; i++) {
        s_color_chunk[i * 2] = hi;
        s_color_chunk[(i * 2) + 1] = lo;
    }

    for (int d = target_first(); d <= target_last(); d++) {
        spi_device_handle_t dev = s_spi[d];
        dev_set_window(dev, x, y, x + w - 1, y + h - 1);

        size_t remaining = total;
        while (remaining > 0) {
            size_t batch = remaining > CHUNK_PIXELS ? CHUNK_PIXELS : remaining;

            gpio_set_level(OLED_PIN_DC, 1);
            spi_transaction_t trans = {
                .length = batch * 16,
                .tx_buffer = s_color_chunk,
            };
            ESP_ERROR_CHECK(spi_device_polling_transmit(dev, &trans));
            remaining -= batch;
        }
    }
}

/*
 * The SSD1351 controller RAM is 128x128, but the 1.27" glass only shows a
 * 96-row window whose position within RAM is offset. Filling just 0..95 leaves
 * the unmapped visible rows un-painted (they show black). To guarantee the
 * whole glass is covered, the full-screen clear paints the entire 128x128 RAM.
 */
#define SSD1351_GRAM_DIM 128

void ssd1351_fill_screen(uint16_t color)
{
    const uint8_t hi = (uint8_t)(color >> 8);
    const uint8_t lo = (uint8_t)(color & 0xFF);

    size_t prefill = CHUNK_PIXELS;
    for (size_t i = 0; i < prefill; i++) {
        s_color_chunk[i * 2] = hi;
        s_color_chunk[(i * 2) + 1] = lo;
    }

    const size_t total = (size_t)SSD1351_GRAM_DIM * (size_t)SSD1351_GRAM_DIM;

    for (int d = target_first(); d <= target_last(); d++) {
        spi_device_handle_t dev = s_spi[d];
        dev_set_window(dev, 0, 0, SSD1351_GRAM_DIM - 1, SSD1351_GRAM_DIM - 1);

        size_t remaining = total;
        while (remaining > 0) {
            size_t batch = remaining > CHUNK_PIXELS ? CHUNK_PIXELS : remaining;

            gpio_set_level(OLED_PIN_DC, 1);
            spi_transaction_t trans = {
                .length = batch * 16,
                .tx_buffer = s_color_chunk,
            };
            ESP_ERROR_CHECK(spi_device_polling_transmit(dev, &trans));
            remaining -= batch;
        }
    }
}

void ssd1351_draw_pixel(int x, int y, uint16_t color)
{
    if (x < 0 || y < 0 || x >= OLED_WIDTH || y >= OLED_HEIGHT) {
        return;
    }

    const uint8_t bytes[2] = { (uint8_t)(color >> 8), (uint8_t)(color & 0xFF) };
    for (int d = target_first(); d <= target_last(); d++) {
        dev_set_window(s_spi[d], x, y, x, y);
        dev_write_data(s_spi[d], bytes, sizeof(bytes));
    }
}

void ssd1351_draw_bitmap(int x, int y, int w, int h, const uint16_t *colors)
{
    if (colors == NULL || w <= 0 || h <= 0) {
        return;
    }

    if (x < 0 || y < 0 || x + w > OLED_WIDTH || y + h > OLED_HEIGHT) {
        return;
    }

    const size_t total = (size_t)w * (size_t)h;

    for (int d = target_first(); d <= target_last(); d++) {
        spi_device_handle_t dev = s_spi[d];
        dev_set_window(dev, x, y, x + w - 1, y + h - 1);

        size_t remaining = total;
        size_t source_offset = 0;
        while (remaining > 0) {
            size_t batch = remaining > CHUNK_PIXELS ? CHUNK_PIXELS : remaining;
            for (size_t i = 0; i < batch; i++) {
                uint16_t color = colors[source_offset + i];
                s_color_chunk[i * 2] = (uint8_t)(color >> 8);
                s_color_chunk[(i * 2) + 1] = (uint8_t)(color & 0xFF);
            }

            gpio_set_level(OLED_PIN_DC, 1);
            spi_transaction_t trans = {
                .length = batch * 16,
                .tx_buffer = s_color_chunk,
            };
            ESP_ERROR_CHECK(spi_device_polling_transmit(dev, &trans));

            source_offset += batch;
            remaining -= batch;
        }
    }
}
```

## 13.4 U2D2 + USB hub (complete servo USB / Dynamixel module)

`servo_dxl.h` is the public API. `servo_dxl.c` is the full USB client, FTDI SIO path for `16d0:06a7`, hub scan, and Dynamixel v1 worker. U2D2 identification, skip lists, and attach-before-UVC live in this file.

### `main/servo_dxl.h` — complete file (77 lines)

Copied from the tree as of this document. Do not edit here; edit the source file.

```c
#pragma once

#include <stdbool.h>

#include "esp_err.h"

/** Register USB host client for U2D2 (FTDI) and start the Dynamixel worker task. */
esp_err_t nino_servo_dxl_start(void);

/** True after U2D2 is open, joint mode enabled, and servos are usable. */
bool nino_servo_dxl_is_ready(void);

/** True when U2D2 USB serial is open (may still be enabling joint mode). */
bool nino_servo_dxl_bus_open(void);

/** Queue both servos to neutral (512). Safe if not ready (no-op). */
void nino_servo_dxl_go_neutral(void);

/** Queue pan (ID 2) and tilt (ID 1) goal positions (0–1023). */
void nino_servo_dxl_set_pan_tilt(int pan_goal, int tilt_goal);

/**
 * Set joint-mode moving speed for both servos (1–1023).
 * The value is sent before the next queued goal positions.
 */
void nino_servo_dxl_set_position_speed(int speed);

/** Queue a single servo goal by Dynamixel ID (1 or 2). */
void nino_servo_dxl_set_servo_goal(uint8_t id, int goal);

/** Read present position (0–1023) for one servo. Requires bus open (torque may be off). */
esp_err_t nino_servo_dxl_get_present_position(uint8_t id, int *position);

/**
 * Enable/disable torque for one servo (ID 1 or 2).
 * Torque-off allows hand teaching while present-position reads still work.
 */
esp_err_t nino_servo_dxl_set_torque(uint8_t id, bool enable);

/** Last commanded torque state for one servo (true after joint-mode init). */
bool nino_servo_dxl_torque_is_on(uint8_t id);

/** Dynamixel IDs used by this firmware (tilt=1, pan=2). */
#define NINO_SERVO_TILT_ID 1
#define NINO_SERVO_PAN_ID 2
#define NINO_SERVO_ID_COUNT 2

/**
 * Broadcast PING on the Dynamixel bus and collect responding servo IDs.
 * Requires U2D2 open (not necessarily joint-ready). IDs are sorted ascending.
 */
esp_err_t nino_servo_dxl_scan_chain(uint8_t *ids, size_t max_ids, size_t *out_count);

/**
 * ID2 full rotation: neutral (512) if needed, then 512→0→1023→512.
 * Runs in a background task; returns ESP_ERR_INVALID_STATE if already running or bus not ready.
 */
esp_err_t nino_servo_dxl_spin_360(void);

/** True while the 360 spin task is running (pose/neutral writes are ignored then). */
bool nino_servo_dxl_spin_is_active(void);

/**
 * ID2 "track hon" sweep: 512→212→512→800→512.
 * Runs continuously in a background task until stopped; returns ESP_ERR_INVALID_STATE
 * if motion is busy or bus not ready.
 */
esp_err_t nino_servo_dxl_track_hon(void);

/** True while the track-hon task is running. */
bool nino_servo_dxl_track_hon_is_active(void);

/**
 * Request stop for running track-hon motion. The task exits, waits 2 seconds,
 * then sends ID2 to neutral (512).
 */
esp_err_t nino_servo_dxl_track_hon_stop(void);
```

### `main/servo_dxl.c` — complete file (2092 lines)

Copied from the tree as of this document. Do not edit here; edit the source file.

```c
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>

#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "esp_check.h"
#include "esp_err.h"
#include "esp_log.h"
#include "usb/usb_helpers.h"
#include "usb/usb_host.h"

#include "battery_endurance.h"
#include "servo_dxl.h"
#include "servo_motion.h"
#include "servo_recplay.h"

#define USB_CLIENT_TASK_STACK_SIZE          8192
#define USB_TASK_PRIORITY                   18

#define FTDI_VID                            0x0403
#define ROBOTIS_VID                         0x16d0
#define ROBOTIS_U2D2_PID                    0x06a7
#define FTDI_DEFAULT_INDEX                  0x0001
#define FTDI_RX_HEADER_SIZE                 2
/** FT232 bulk OUT: byte0 = 0x01 | ((payload_len & 0x3F) << 2) per ftdi_sio. */
#define FTDI_TX_HEADER_SIZE                 1
#define FTDI_TX_MAX_PAYLOAD                 63
#define FTDI_BAUD_BASE                      3000000UL

#define FTDI_SIO_RESET                      0x00
#define FTDI_SIO_MODEM_CTRL                 0x01
#define FTDI_SIO_SET_FLOW_CTRL              0x02
#define FTDI_SIO_SET_BAUD_RATE              0x03
#define FTDI_SIO_SET_DATA                   0x04
#define FTDI_SIO_SET_LATENCY_TIMER          0x09

#define FTDI_SIO_RESET_SIO                  0x0000
#define FTDI_SIO_MODEM_DTR                  0x0001
#define FTDI_SIO_MODEM_RTS                  0x0002
#define FTDI_SIO_MODEM_DTR_ENABLE           0x0100
#define FTDI_SIO_MODEM_RTS_ENABLE           0x0200
#define FTDI_SIO_SET_DATA_8N1               0x0008

#define USB_CLASS_CDC_COMM                  0x02
#ifndef USB_CLASS_CDC_DATA
#define USB_CLASS_CDC_DATA                  0x0A
#endif
#define CDC_ACM_SET_LINE_CODING             0x20
#define CDC_ACM_SET_CONTROL_LINE_STATE      0x22
#define CDC_ACM_CONTROL_LINE_DTR            0x0001
#define CDC_ACM_CONTROL_LINE_RTS            0x0002

#define DXL_HEADER_0                        0xFF
#define DXL_HEADER_1                        0xFF

#define DXL_INST_PING                       0x01
#define DXL_INST_READ                       0x02
#define DXL_INST_WRITE                      0x03

#define DXL_PRIMARY_ID                      1
#define DXL_SECONDARY_ID                    2
#define DXL_SERVO_COUNT                     2
#define DXL_DEFAULT_BAUDRATE                1000000
#define DXL_TORQUE_ENABLE_ADDR              24
#define DXL_MOVING_SPEED_ADDR               32
#define DXL_GOAL_POSITION_ADDR              30
#define DXL_PRESENT_POSITION_ADDR           36
#define DXL_CW_ANGLE_LIMIT_ADDR             6
#define DXL_CCW_ANGLE_LIMIT_ADDR            8
#define DXL_TORQUE_ON                       1
#define DXL_TORQUE_OFF                      0
#define DXL_GOAL_MIN                        0
#define DXL_GOAL_MAX                        1023
#define DXL_CENTER_POSITION                 512
#define DXL_WHEEL_SPEED_MIN                 -1023
#define DXL_WHEEL_SPEED_MAX                 1023
#define DXL_POSITION_SPEED_MIN              1
#define DXL_POSITION_SPEED_MAX              1023
#define DXL_DEFAULT_POSITION_SPEED          22
#define DXL_AX_JOINT_CCW_LIMIT              1023

#define DXL_MAX_PARAMS                      64
#define DXL_MAX_PACKET_SIZE                 96
#define DXL_RX_ACCUM_SIZE                   256
#define DXL_APP_POLL_INTERVAL_MS            100
#define DXL_FAST_POLL_INTERVAL_MS           30
#define DXL_ATTACH_RETRY_MS                   400
#define DXL_HUB_SETTLE_ATTEMPTS               80
#define DXL_PING_FAIL_RECONNECT               8
#define DXL_USB_ADDR_LIST_MAX                 16
#define DXL_POSITION_TOLERANCE                15
#define DXL_MOVE_SEGMENT_TIMEOUT_MS           60000
#define DXL_SPIN360_TASK_STACK                4096
#define DXL_SPIN360_TASK_PRIO                 4
#define DXL_TRACK_HON_TASK_STACK              4096
#define DXL_TRACK_HON_TASK_PRIO               4
#define DXL_TRACK_HON_HOLD_MS                 500
#define DXL_BROADCAST_ID                      0xFE
#define DXL_MAX_SCAN_IDS                      32
#define DXL_SCAN_COLLECT_MS                   200

typedef enum {
    DXL_SYNC_IDLE = 0,
    DXL_SYNC_READ_PENDING,
    DXL_SYNC_READ_WAIT_RSP,
    DXL_SYNC_PING_WAIT_RSP,
    DXL_SYNC_SCAN_WAIT_RSP,
} dxl_sync_state_t;

typedef struct {
    volatile dxl_sync_state_t state;
    uint8_t id;
    uint8_t addr;
    uint8_t length;
    uint16_t value;
    esp_err_t result;
} dxl_sync_request_t;

typedef enum {
    DEVICE_ACTION_NONE = 0,
    DEVICE_ACTION_OPEN = 1 << 0,
    DEVICE_ACTION_CLOSE = 1 << 1,
} device_action_t;

typedef struct {
    volatile bool done;
    esp_err_t result;
} transfer_wait_t;

typedef struct {
    uint8_t id;
    uint8_t error;
    uint16_t param_len;
    uint8_t params[DXL_MAX_PARAMS];
} dynamixel_status_packet_t;

typedef struct {
    usb_host_client_handle_t client_hdl;
    usb_device_handle_t dev_hdl;
    uint8_t dev_addr;
    uint16_t vid;
    uint16_t pid;
    bool is_ftdi;

    uint8_t interface_number;
    uint8_t interface_alt;
    uint8_t ep_in;
    uint8_t ep_out;
    uint16_t ep_mps_in;
    uint16_t ep_mps_out;
    uint8_t interface_class;
    uint8_t interface_subclass;
    uint8_t interface_protocol;
    uint8_t control_interface_number;

    bool device_ready;
    bool device_gone;

    usb_transfer_t *ctrl_xfer;
    usb_transfer_t *bulk_in_xfer;
    usb_transfer_t *bulk_out_xfer;

    uint8_t rx_accum[DXL_RX_ACCUM_SIZE];
    size_t rx_accum_len;

    volatile uint32_t actions;
} ftdi_device_t;

typedef enum {
    SERVO_MODE_POSITION = 0,
} servo_mode_t;

static const char *TAG = "nino_servo";
static const uint8_t s_servo_ids[] = {
    DXL_PRIMARY_ID,
    DXL_SECONDARY_ID,
};
static ftdi_device_t s_ftdi = {0};
static bool s_torque_enabled = false; /* joint-mode initialized / bus usable */
static bool s_servo_torque_on[DXL_SERVO_COUNT] = {false, false};
static volatile bool s_torque_update_pending[DXL_SERVO_COUNT] = {false, false};
static volatile bool s_requested_torque_on[DXL_SERVO_COUNT] = {true, true};
static volatile bool s_goal_update_pending[DXL_SERVO_COUNT] = {false};
static volatile int s_requested_goal[DXL_SERVO_COUNT] = {
    DXL_CENTER_POSITION,
    DXL_CENTER_POSITION,
};
static volatile int s_requested_position_speed = DXL_DEFAULT_POSITION_SPEED;
static int s_active_position_speed = DXL_DEFAULT_POSITION_SPEED;
static volatile bool s_position_speed_pending = false;
static int s_ping_fail_streak = 0;
static bool s_servo_started = false;
static SemaphoreHandle_t s_goal_mutex;
static dxl_sync_request_t s_sync = {0};
static SemaphoreHandle_t s_sync_mutex;
static SemaphoreHandle_t s_read_done_sem;
static TaskHandle_t s_spin360_task;
static TaskHandle_t s_track_hon_task;
static volatile bool s_track_hon_stop_requested;
static uint8_t s_scan_ids[DXL_MAX_SCAN_IDS];
static size_t s_scan_count;

static void usb_client_task(void *arg);

static void client_event_cb(const usb_host_client_event_msg_t *event_msg, void *arg);
static void ctrl_transfer_cb(usb_transfer_t *transfer);
static void bulk_out_transfer_cb(usb_transfer_t *transfer);
static void bulk_in_transfer_cb(usb_transfer_t *transfer);

static esp_err_t ftdi_open_device(ftdi_device_t *dev);
static void ftdi_close_device(ftdi_device_t *dev);
static esp_err_t ftdi_configure_device(ftdi_device_t *dev, uint32_t baudrate);
static esp_err_t cdc_acm_configure_device(ftdi_device_t *dev, uint32_t baudrate);
static esp_err_t ftdi_start_rx(ftdi_device_t *dev);
static esp_err_t ftdi_uart_write(ftdi_device_t *dev, const uint8_t *data, size_t len);

static size_t dynamixel_build_instruction_packet(uint8_t id, uint8_t instruction, const uint8_t *params, size_t params_len, uint8_t *packet, size_t packet_size);
static void dynamixel_rx_consume(ftdi_device_t *dev, const uint8_t *data, size_t len);
static bool dynamixel_try_parse_packet(ftdi_device_t *dev);
static void dynamixel_handle_status_packet(const dynamixel_status_packet_t *packet);
static esp_err_t dynamixel_send_ping(ftdi_device_t *dev, uint8_t id);
static esp_err_t dynamixel_send_read(ftdi_device_t *dev, uint8_t id, uint8_t addr, uint8_t len);
static esp_err_t dynamixel_send_write8(ftdi_device_t *dev, uint8_t id, uint8_t addr, uint8_t value);
static esp_err_t dynamixel_send_write16(ftdi_device_t *dev, uint8_t id, uint8_t addr, uint16_t value);
static esp_err_t dynamixel_send_write8_all(ftdi_device_t *dev, uint8_t addr, uint8_t value);
static esp_err_t dynamixel_send_write16_all(ftdi_device_t *dev, uint8_t addr, uint16_t value);
static esp_err_t dynamixel_set_joint_mode(ftdi_device_t *dev);
static void dynamixel_queue_goal_all(int goal);
static int clamp_goal(int goal);
static int servo_index_for_id(uint8_t id);
static esp_err_t dynamixel_read_word_blocking(uint8_t id, uint8_t addr, uint16_t *out, TickType_t timeout);
static esp_err_t dynamixel_ping_blocking(uint8_t id, TickType_t timeout);
static esp_err_t dynamixel_scan_chain_blocking(uint8_t *ids, size_t max_ids, size_t *out_count);
static void scan_add_id(uint8_t id);
static void scan_sort_ids(uint8_t *ids, size_t count);
static bool dynamixel_wait_servo_at(uint8_t id, int target, TickType_t timeout_ms);
static bool track_hon_wait_target_or_stop(uint8_t id, int target, TickType_t timeout_ms);
static void spin360_task(void *arg);
static void track_hon_task(void *arg);
static bool usb_is_obvious_non_u2d2(uint16_t vid, uint16_t pid, uint8_t dev_class);
static bool usb_is_u2d2_candidate(uint16_t vid, uint16_t pid);
static const char *usb_speed_str(usb_speed_t speed);

static uint8_t dynamixel_v1_checksum(const uint8_t *data, size_t len)
{
    uint32_t sum = 0;

    for (size_t i = 0; i < len; i++) {
        sum += data[i];
    }

    return (uint8_t)(~sum & 0xFF);
}

static const char *usb_speed_str(usb_speed_t speed)
{
    switch (speed) {
    case USB_SPEED_HIGH:
        return "HS";
    case USB_SPEED_FULL:
        return "FS";
    case USB_SPEED_LOW:
        return "LS";
    default:
        return "?";
    }
}

static bool usb_is_obvious_non_u2d2(uint16_t vid, uint16_t pid, uint8_t dev_class)
{
    (void)pid;
    if (dev_class == USB_CLASS_HUB || dev_class == USB_CLASS_VIDEO) {
        return true;
    }
    /* USB hub bridge chips and UVC camera — not the Dynamixel serial adapter. */
    if (vid == 0x046d || vid == 0x03eb || vid == 0x1a40 || vid == 0x05e3 ||
        vid == 0x2109 || vid == 0x0bda || vid == 0x1a86 || vid == 0x0424 ||
        vid == 0x174c || vid == 0x2357 || vid == 0x214b) {
        return true;
    }
    return false;
}

static bool usb_is_robotis_u2d2(uint16_t vid, uint16_t pid)
{
    return vid == ROBOTIS_VID && pid == ROBOTIS_U2D2_PID;
}

static bool usb_is_u2d2_candidate(uint16_t vid, uint16_t pid)
{
    if (vid == FTDI_VID) {
        return true;
    }
    /* USB-C and Micro-B U2D2: FT232HL with ROBOTIS EEPROM 16d0:06a7. */
    return usb_is_robotis_u2d2(vid, pid);
}

static bool usb_iface_is_cdc_acm(const ftdi_device_t *dev)
{
    return dev->interface_class == USB_CLASS_CDC_DATA &&
           dev->control_interface_number != 0xFF;
}

/** True when the chip speaks FTDI SIO (not native CDC ACM). */
static bool usb_device_uses_ftdi_sio(const ftdi_device_t *dev)
{
    if (dev->vid == FTDI_VID) {
        return true;
    }
    /* New U2D2 keeps FTDI protocol; only the VID/PID in EEPROM changed. */
    if (usb_is_robotis_u2d2(dev->vid, dev->pid) && !usb_iface_is_cdc_acm(dev)) {
        return true;
    }
    return false;
}

static int clamp_goal(int goal)
{
    if (goal < DXL_GOAL_MIN) {
        return DXL_GOAL_MIN;
    }
    if (goal > DXL_GOAL_MAX) {
        return DXL_GOAL_MAX;
    }
    return goal;
}

static void dynamixel_queue_goal_all(int goal)
{
    goal = clamp_goal(goal);
    if (s_goal_mutex != NULL) {
        xSemaphoreTake(s_goal_mutex, portMAX_DELAY);
    }
    for (size_t i = 0; i < DXL_SERVO_COUNT; i++) {
        s_requested_goal[i] = goal;
        s_goal_update_pending[i] = true;
    }
    if (s_goal_mutex != NULL) {
        xSemaphoreGive(s_goal_mutex);
    }
}

bool nino_servo_dxl_is_ready(void)
{
    return s_torque_enabled && s_ftdi.device_ready && !s_ftdi.device_gone;
}

bool nino_servo_dxl_bus_open(void)
{
    return s_ftdi.device_ready && !s_ftdi.device_gone;
}

bool nino_servo_dxl_spin_is_active(void)
{
    return s_spin360_task != NULL;
}

bool nino_servo_dxl_track_hon_is_active(void)
{
    return s_track_hon_task != NULL;
}

void nino_servo_dxl_go_neutral(void)
{
    /* Audio-playback cleanup and head-motion stop call this; while a 360 spin
     * is running it must not yank ID2 back to center mid-rotation (the spin
     * ends at neutral anyway). Same for the GPIO48 hardware test sweep. */
    if (nino_servo_dxl_spin_is_active() || nino_servo_dxl_track_hon_is_active() ||
        nino_servo_recplay_is_busy() || nino_battery_endurance_owns_actuators()) {
        return;
    }
    dynamixel_queue_goal_all(DXL_CENTER_POSITION);
}

void nino_servo_dxl_set_pan_tilt(int pan_goal, int tilt_goal)
{
    /* Head-motion poses must not override spin / hand-teach. Play mode is allowed. */
    if (nino_servo_dxl_spin_is_active() || nino_servo_dxl_track_hon_is_active()) {
        return;
    }
    if (nino_servo_recplay_mode() == NINO_SERVO_MODE_RECORD) {
        return;
    }
    if (s_goal_mutex != NULL) {
        xSemaphoreTake(s_goal_mutex, portMAX_DELAY);
    }
    /* Head wiring: ID1 = tilt (pitch), ID2 = pan (yaw). */
    s_requested_goal[0] = clamp_goal(tilt_goal);
    s_requested_goal[1] = clamp_goal(pan_goal);
    s_goal_update_pending[0] = true;
    s_goal_update_pending[1] = true;
    if (s_goal_mutex != NULL) {
        xSemaphoreGive(s_goal_mutex);
    }
}

void nino_servo_dxl_set_position_speed(int speed)
{
    if (speed < DXL_POSITION_SPEED_MIN) {
        speed = DXL_POSITION_SPEED_MIN;
    } else if (speed > DXL_POSITION_SPEED_MAX) {
        speed = DXL_POSITION_SPEED_MAX;
    }

    if (s_goal_mutex != NULL) {
        xSemaphoreTake(s_goal_mutex, portMAX_DELAY);
    }
    s_requested_position_speed = speed;
    s_position_speed_pending = true;
    if (s_goal_mutex != NULL) {
        xSemaphoreGive(s_goal_mutex);
    }
}

void nino_servo_dxl_set_servo_goal(uint8_t id, int goal)
{
    const int idx = servo_index_for_id(id);
    if (idx < 0) {
        return;
    }

    goal = clamp_goal(goal);
    if (s_goal_mutex != NULL) {
        xSemaphoreTake(s_goal_mutex, portMAX_DELAY);
    }
    s_requested_goal[idx] = goal;
    s_goal_update_pending[idx] = true;
    if (s_goal_mutex != NULL) {
        xSemaphoreGive(s_goal_mutex);
    }
}

esp_err_t nino_servo_dxl_get_present_position(uint8_t id, int *position)
{
    if (position == NULL) {
        return ESP_ERR_INVALID_ARG;
    }

    uint16_t raw = 0;
    esp_err_t err = dynamixel_read_word_blocking(id, DXL_PRESENT_POSITION_ADDR, &raw,
                                                 pdMS_TO_TICKS(2000));
    if (err != ESP_OK) {
        return err;
    }

    *position = (int)raw;
    return ESP_OK;
}

bool nino_servo_dxl_torque_is_on(uint8_t id)
{
    const int idx = servo_index_for_id(id);
    if (idx < 0) {
        return false;
    }
    return s_servo_torque_on[idx];
}

esp_err_t nino_servo_dxl_set_torque(uint8_t id, bool enable)
{
    const int idx = servo_index_for_id(id);
    if (idx < 0) {
        return ESP_ERR_INVALID_ARG;
    }
    if (!s_ftdi.device_ready || s_ftdi.device_gone || !s_torque_enabled) {
        return ESP_ERR_INVALID_STATE;
    }

    if (s_goal_mutex != NULL) {
        xSemaphoreTake(s_goal_mutex, portMAX_DELAY);
    }
    s_requested_torque_on[idx] = enable;
    s_torque_update_pending[idx] = true;
    if (s_goal_mutex != NULL) {
        xSemaphoreGive(s_goal_mutex);
    }

    /* Wait briefly for the USB worker to apply the write. */
    const TickType_t deadline = xTaskGetTickCount() + pdMS_TO_TICKS(1500);
    while (xTaskGetTickCount() < deadline) {
        if (!s_torque_update_pending[idx] && s_servo_torque_on[idx] == enable) {
            return ESP_OK;
        }
        vTaskDelay(pdMS_TO_TICKS(20));
    }
    return s_servo_torque_on[idx] == enable ? ESP_OK : ESP_ERR_TIMEOUT;
}

esp_err_t nino_servo_dxl_scan_chain(uint8_t *ids, size_t max_ids, size_t *out_count)
{
    return dynamixel_scan_chain_blocking(ids, max_ids, out_count);
}

esp_err_t nino_servo_dxl_spin_360(void)
{
    if (s_spin360_task != NULL || s_track_hon_task != NULL || nino_servo_recplay_is_busy()) {
        return ESP_ERR_INVALID_STATE;
    }
    if (!nino_servo_dxl_is_ready()) {
        return ESP_ERR_INVALID_STATE;
    }

    BaseType_t ok = xTaskCreate(spin360_task, "servo_360", DXL_SPIN360_TASK_STACK, NULL,
                                DXL_SPIN360_TASK_PRIO, &s_spin360_task);
    if (ok != pdPASS) {
        s_spin360_task = NULL;
        return ESP_ERR_NO_MEM;
    }

    return ESP_OK;
}

esp_err_t nino_servo_dxl_track_hon(void)
{
    if (s_track_hon_task != NULL || s_spin360_task != NULL || nino_servo_recplay_is_busy()) {
        return ESP_ERR_INVALID_STATE;
    }
    if (!nino_servo_dxl_is_ready()) {
        return ESP_ERR_INVALID_STATE;
    }

    /* Freeze any current LRUD/NOD motion while track-hon path is active. */
    nino_servo_motion_stop();
    s_track_hon_stop_requested = false;

    BaseType_t ok = xTaskCreate(track_hon_task, "servo_hon", DXL_TRACK_HON_TASK_STACK, NULL,
                                DXL_TRACK_HON_TASK_PRIO, &s_track_hon_task);
    if (ok != pdPASS) {
        s_track_hon_task = NULL;
        return ESP_ERR_NO_MEM;
    }

    return ESP_OK;
}

esp_err_t nino_servo_dxl_track_hon_stop(void)
{
    if (s_track_hon_task == NULL) {
        return ESP_ERR_INVALID_STATE;
    }
    s_track_hon_stop_requested = true;
    return ESP_OK;
}

static int servo_index_for_id(uint8_t id)
{
    for (size_t i = 0; i < DXL_SERVO_COUNT; i++) {
        if (s_servo_ids[i] == id) {
            return (int)i;
        }
    }
    return -1;
}

static int position_delta(int current, int target)
{
    int delta = current - target;
    if (delta < 0) {
        delta = -delta;
    }
    return delta;
}

static bool dynamixel_wait_servo_at(uint8_t id, int target, TickType_t timeout_ms)
{
    const TickType_t start = xTaskGetTickCount();

    while ((xTaskGetTickCount() - start) < timeout_ms) {
        int pos = 0;
        if (nino_servo_dxl_get_present_position(id, &pos) == ESP_OK) {
            if (position_delta(pos, target) <= DXL_POSITION_TOLERANCE) {
                return true;
            }
        }
        vTaskDelay(pdMS_TO_TICKS(80));
    }

    return false;
}

static bool track_hon_wait_target_or_stop(uint8_t id, int target, TickType_t timeout_ms)
{
    const TickType_t start = xTaskGetTickCount();

    while ((xTaskGetTickCount() - start) < timeout_ms) {
        if (s_track_hon_stop_requested) {
            return false;
        }
        int pos = 0;
        if (nino_servo_dxl_get_present_position(id, &pos) == ESP_OK) {
            if (position_delta(pos, target) <= DXL_POSITION_TOLERANCE) {
                return true;
            }
        }
        vTaskDelay(pdMS_TO_TICKS(80));
    }

    return false;
}

static void spin360_task(void *arg)
{
    (void)arg;
    const uint8_t servo_id = DXL_SECONDARY_ID;
    static const int waypoints[] = {
        DXL_CENTER_POSITION,
        DXL_GOAL_MIN,
        DXL_GOAL_MAX,
        DXL_CENTER_POSITION,
    };

    ESP_LOGI(TAG, "ID%u 360 spin start — neutral=%d, path 512→0→1023→512",
             servo_id, DXL_CENTER_POSITION);

    if (!nino_servo_dxl_is_ready()) {
        ESP_LOGW(TAG, "ID%u 360 spin aborted — servos not ready", servo_id);
        goto done;
    }

    int pos = 0;
    if (nino_servo_dxl_get_present_position(servo_id, &pos) == ESP_OK) {
        if (position_delta(pos, DXL_CENTER_POSITION) > DXL_POSITION_TOLERANCE) {
            ESP_LOGI(TAG, "ID%u not at neutral (%d) — moving to %d", servo_id, pos,
                     DXL_CENTER_POSITION);
            nino_servo_dxl_set_servo_goal(servo_id, DXL_CENTER_POSITION);
            if (!dynamixel_wait_servo_at(servo_id, DXL_CENTER_POSITION,
                                         pdMS_TO_TICKS(DXL_MOVE_SEGMENT_TIMEOUT_MS))) {
                ESP_LOGW(TAG, "ID%u failed to reach neutral before 360 spin", servo_id);
                goto done;
            }
        }
    } else {
        ESP_LOGW(TAG, "ID%u present position read failed — homing to %d", servo_id,
                 DXL_CENTER_POSITION);
        nino_servo_dxl_set_servo_goal(servo_id, DXL_CENTER_POSITION);
        vTaskDelay(pdMS_TO_TICKS(1500));
    }

    for (size_t i = 1; i < sizeof(waypoints) / sizeof(waypoints[0]); i++) {
        const int goal = waypoints[i];
        ESP_LOGI(TAG, "ID%u moving to %d", servo_id, goal);
        nino_servo_dxl_set_servo_goal(servo_id, goal);
        if (!dynamixel_wait_servo_at(servo_id, goal, pdMS_TO_TICKS(DXL_MOVE_SEGMENT_TIMEOUT_MS))) {
            ESP_LOGW(TAG, "ID%u timed out reaching %d during 360 spin", servo_id, goal);
            break;
        }
    }

    ESP_LOGI(TAG, "ID%u 360 spin finished", servo_id);

done:
    s_spin360_task = NULL;
    vTaskDelete(NULL);
}

static void track_hon_task(void *arg)
{
    (void)arg;
    const uint8_t servo_id = DXL_SECONDARY_ID;
    static const int waypoints[] = {512, 212, 512, 800, 512};

    ESP_LOGI(TAG, "ID%u track hon start — path 512->212->512->800->512", servo_id);

    if (!nino_servo_dxl_is_ready()) {
        ESP_LOGW(TAG, "ID%u track hon aborted — servos not ready", servo_id);
        goto done;
    }

    while (!s_track_hon_stop_requested) {
        for (size_t i = 0; i < sizeof(waypoints) / sizeof(waypoints[0]); i++) {
            if (s_track_hon_stop_requested) {
                break;
            }
            const int goal = waypoints[i];
            ESP_LOGI(TAG, "ID%u moving to %d", servo_id, goal);
            nino_servo_dxl_set_servo_goal(servo_id, goal);
            if (!track_hon_wait_target_or_stop(servo_id, goal,
                                               pdMS_TO_TICKS(DXL_MOVE_SEGMENT_TIMEOUT_MS))) {
                if (s_track_hon_stop_requested) {
                    break;
                }
                ESP_LOGW(TAG, "ID%u timed out reaching %d during track hon", servo_id, goal);
                goto done;
            }

            const TickType_t hold_start = xTaskGetTickCount();
            while (!s_track_hon_stop_requested &&
                   (xTaskGetTickCount() - hold_start) < pdMS_TO_TICKS(DXL_TRACK_HON_HOLD_MS)) {
                vTaskDelay(pdMS_TO_TICKS(20));
            }
        }
    }

    if (s_track_hon_stop_requested) {
        ESP_LOGI(TAG, "ID%u track hon stop requested", servo_id);
        vTaskDelay(pdMS_TO_TICKS(2000));
        nino_servo_dxl_set_servo_goal(servo_id, DXL_CENTER_POSITION);
        if (!dynamixel_wait_servo_at(servo_id, DXL_CENTER_POSITION,
                                     pdMS_TO_TICKS(DXL_MOVE_SEGMENT_TIMEOUT_MS))) {
            ESP_LOGW(TAG, "ID%u neutral return timeout after hstop", servo_id);
        }
        ESP_LOGI(TAG, "ID%u track hon stopped and returned to %d", servo_id,
                 DXL_CENTER_POSITION);
    }

done:
    s_track_hon_stop_requested = false;
    s_track_hon_task = NULL;
    vTaskDelete(NULL);
}

static void scan_add_id(uint8_t id)
{
    if (id == 0 || id == DXL_BROADCAST_ID) {
        return;
    }

    for (size_t i = 0; i < s_scan_count; i++) {
        if (s_scan_ids[i] == id) {
            return;
        }
    }

    if (s_scan_count < DXL_MAX_SCAN_IDS) {
        s_scan_ids[s_scan_count++] = id;
    }
}

static void scan_sort_ids(uint8_t *ids, size_t count)
{
    for (size_t i = 0; i + 1 < count; i++) {
        for (size_t j = i + 1; j < count; j++) {
            if (ids[j] < ids[i]) {
                const uint8_t tmp = ids[i];
                ids[i] = ids[j];
                ids[j] = tmp;
            }
        }
    }
}

static esp_err_t dynamixel_scan_chain_blocking(uint8_t *ids, size_t max_ids, size_t *out_count)
{
    if (ids == NULL || out_count == NULL || max_ids == 0) {
        return ESP_ERR_INVALID_ARG;
    }
    if (s_sync_mutex == NULL) {
        return ESP_ERR_INVALID_STATE;
    }
    if (!s_ftdi.device_ready || s_ftdi.client_hdl == NULL) {
        *out_count = 0;
        return ESP_ERR_INVALID_STATE;
    }

    *out_count = 0;

    xSemaphoreTake(s_sync_mutex, portMAX_DELAY);
    if (s_sync.state != DXL_SYNC_IDLE) {
        xSemaphoreGive(s_sync_mutex);
        return ESP_ERR_INVALID_STATE;
    }
    s_sync.state = DXL_SYNC_SCAN_WAIT_RSP;
    s_scan_count = 0;
    xSemaphoreGive(s_sync_mutex);

    esp_err_t err = dynamixel_send_ping(&s_ftdi, DXL_BROADCAST_ID);
    if (err != ESP_OK) {
        xSemaphoreTake(s_sync_mutex, portMAX_DELAY);
        s_sync.state = DXL_SYNC_IDLE;
        xSemaphoreGive(s_sync_mutex);
        return err;
    }

    const TickType_t deadline = xTaskGetTickCount() + pdMS_TO_TICKS(DXL_SCAN_COLLECT_MS);
    while (xTaskGetTickCount() < deadline) {
        if (s_ftdi.device_gone) {
            break;
        }
        (void)usb_host_client_handle_events(s_ftdi.client_hdl, pdMS_TO_TICKS(10));
    }

    xSemaphoreTake(s_sync_mutex, portMAX_DELAY);
    s_sync.state = DXL_SYNC_IDLE;
    const size_t found = s_scan_count;
    const size_t copy_count = (found < max_ids) ? found : max_ids;
    for (size_t i = 0; i < copy_count; i++) {
        ids[i] = s_scan_ids[i];
    }
    xSemaphoreGive(s_sync_mutex);

    scan_sort_ids(ids, copy_count);
    *out_count = copy_count;
    return (copy_count > 0) ? ESP_OK : ESP_ERR_NOT_FOUND;
}

static esp_err_t dynamixel_ping_blocking(uint8_t id, TickType_t timeout)
{
    if (s_sync_mutex == NULL || s_read_done_sem == NULL) {
        return ESP_ERR_INVALID_STATE;
    }
    if (!s_ftdi.device_ready || s_ftdi.client_hdl == NULL) {
        return ESP_ERR_INVALID_STATE;
    }

    (void)xSemaphoreTake(s_read_done_sem, 0);

    xSemaphoreTake(s_sync_mutex, portMAX_DELAY);
    s_sync.state = DXL_SYNC_PING_WAIT_RSP;
    s_sync.id = id;
    s_sync.result = ESP_FAIL;
    xSemaphoreGive(s_sync_mutex);

    esp_err_t err = dynamixel_send_ping(&s_ftdi, id);
    if (err != ESP_OK) {
        xSemaphoreTake(s_sync_mutex, portMAX_DELAY);
        s_sync.state = DXL_SYNC_IDLE;
        xSemaphoreGive(s_sync_mutex);
        return err;
    }

    const TickType_t start = xTaskGetTickCount();
    while ((xTaskGetTickCount() - start) < timeout) {
        if (xSemaphoreTake(s_read_done_sem, pdMS_TO_TICKS(20)) == pdTRUE) {
            xSemaphoreTake(s_sync_mutex, portMAX_DELAY);
            const esp_err_t rsp = s_sync.result;
            s_sync.state = DXL_SYNC_IDLE;
            xSemaphoreGive(s_sync_mutex);
            return rsp;
        }
        if (s_ftdi.device_gone) {
            break;
        }
        (void)usb_host_client_handle_events(s_ftdi.client_hdl, pdMS_TO_TICKS(20));
    }

    xSemaphoreTake(s_sync_mutex, portMAX_DELAY);
    s_sync.state = DXL_SYNC_IDLE;
    xSemaphoreGive(s_sync_mutex);
    return ESP_ERR_TIMEOUT;
}

static esp_err_t dynamixel_read_word_blocking(uint8_t id, uint8_t addr, uint16_t *out,
                                              TickType_t timeout)
{
    if (out == NULL || s_sync_mutex == NULL || s_read_done_sem == NULL) {
        return ESP_ERR_INVALID_STATE;
    }
    /* Present-position reads must work with torque off (hand teach / record). */
    if (!s_ftdi.device_ready || s_ftdi.device_gone) {
        return ESP_ERR_INVALID_STATE;
    }

    (void)xSemaphoreTake(s_read_done_sem, 0);

    xSemaphoreTake(s_sync_mutex, portMAX_DELAY);
    s_sync.state = DXL_SYNC_READ_PENDING;
    s_sync.id = id;
    s_sync.addr = addr;
    s_sync.length = 2;
    s_sync.result = ESP_FAIL;
    xSemaphoreGive(s_sync_mutex);

    if (xSemaphoreTake(s_read_done_sem, timeout) != pdTRUE) {
        xSemaphoreTake(s_sync_mutex, portMAX_DELAY);
        s_sync.state = DXL_SYNC_IDLE;
        xSemaphoreGive(s_sync_mutex);
        return ESP_ERR_TIMEOUT;
    }

    xSemaphoreTake(s_sync_mutex, portMAX_DELAY);
    const esp_err_t err = s_sync.result;
    if (err == ESP_OK) {
        *out = s_sync.value;
    }
    s_sync.state = DXL_SYNC_IDLE;
    xSemaphoreGive(s_sync_mutex);
    return err;
}

static uint16_t ftdi_encode_baudrate(uint32_t baudrate)
{
    static const uint16_t frac_code[8] = {0, 3, 2, 4, 1, 5, 6, 7};

    if (baudrate == 0) {
        baudrate = DXL_DEFAULT_BAUDRATE;
    }

    uint32_t divisor_x8 = (uint32_t)(((uint64_t)FTDI_BAUD_BASE * 8ULL + (baudrate / 2ULL)) / baudrate);
    if (divisor_x8 == 0) {
        divisor_x8 = 1;
    }

    return (uint16_t)((divisor_x8 >> 3) | (frac_code[divisor_x8 & 0x7] << 14));
}

static esp_err_t wait_for_transfer(ftdi_device_t *dev, transfer_wait_t *waiter, TickType_t timeout_ticks)
{
    TickType_t start = xTaskGetTickCount();

    while (!waiter->done) {
        TickType_t now = xTaskGetTickCount();
        if ((now - start) >= timeout_ticks) {
            return ESP_ERR_TIMEOUT;
        }

        esp_err_t err = usb_host_client_handle_events(dev->client_hdl, pdMS_TO_TICKS(50));
        if (err != ESP_OK && err != ESP_ERR_TIMEOUT) {
            return err;
        }
        if (dev->device_gone) {
            return ESP_ERR_INVALID_STATE;
        }
    }

    return waiter->result;
}

static esp_err_t ftdi_control_transfer(ftdi_device_t *dev,
                                       uint8_t bm_request_type,
                                       uint8_t b_request,
                                       uint16_t w_value,
                                       uint16_t w_index,
                                       const void *payload,
                                       uint16_t payload_len)
{
    if (dev->ctrl_xfer == NULL) {
        return ESP_ERR_INVALID_STATE;
    }

    usb_setup_packet_t *setup = (usb_setup_packet_t *)dev->ctrl_xfer->data_buffer;
    memset(dev->ctrl_xfer->data_buffer, 0, dev->ctrl_xfer->data_buffer_size);

    setup->bmRequestType = bm_request_type;
    setup->bRequest = b_request;
    setup->wValue = w_value;
    setup->wIndex = w_index;
    setup->wLength = payload_len;

    if (payload_len > 0 && payload != NULL) {
        memcpy(dev->ctrl_xfer->data_buffer + USB_SETUP_PACKET_SIZE, payload, payload_len);
    }

    transfer_wait_t waiter = {
        .done = false,
        .result = ESP_FAIL,
    };

    dev->ctrl_xfer->device_handle = dev->dev_hdl;
    dev->ctrl_xfer->bEndpointAddress = 0;
    dev->ctrl_xfer->num_bytes = USB_SETUP_PACKET_SIZE + payload_len;
    dev->ctrl_xfer->callback = ctrl_transfer_cb;
    dev->ctrl_xfer->context = &waiter;
    dev->ctrl_xfer->timeout_ms = 1000;

    ESP_RETURN_ON_ERROR(usb_host_transfer_submit_control(dev->client_hdl, dev->ctrl_xfer), TAG, "control submit failed");
    return wait_for_transfer(dev, &waiter, pdMS_TO_TICKS(1000));
}

static esp_err_t ftdi_configure_device(ftdi_device_t *dev, uint32_t baudrate)
{
    const uint8_t request_type = USB_BM_REQUEST_TYPE_DIR_OUT |
                                 USB_BM_REQUEST_TYPE_TYPE_VENDOR |
                                 USB_BM_REQUEST_TYPE_RECIP_DEVICE;

    ESP_RETURN_ON_ERROR(ftdi_control_transfer(dev, request_type, FTDI_SIO_RESET, FTDI_SIO_RESET_SIO, FTDI_DEFAULT_INDEX, NULL, 0), TAG, "FTDI reset failed");
    ESP_RETURN_ON_ERROR(ftdi_control_transfer(dev, request_type, FTDI_SIO_SET_LATENCY_TIMER, 1, FTDI_DEFAULT_INDEX, NULL, 0), TAG, "FTDI latency failed");
    ESP_RETURN_ON_ERROR(ftdi_control_transfer(dev, request_type, FTDI_SIO_SET_DATA, FTDI_SIO_SET_DATA_8N1, FTDI_DEFAULT_INDEX, NULL, 0), TAG, "FTDI line setup failed");
    ESP_RETURN_ON_ERROR(ftdi_control_transfer(dev, request_type, FTDI_SIO_MODEM_CTRL,
                                              FTDI_SIO_MODEM_DTR | FTDI_SIO_MODEM_RTS |
                                              FTDI_SIO_MODEM_DTR_ENABLE | FTDI_SIO_MODEM_RTS_ENABLE,
                                              FTDI_DEFAULT_INDEX, NULL, 0), TAG, "FTDI modem ctrl failed");
    ESP_RETURN_ON_ERROR(ftdi_control_transfer(dev, request_type, FTDI_SIO_SET_FLOW_CTRL, 0, FTDI_DEFAULT_INDEX, NULL, 0), TAG, "FTDI flow ctrl failed");
    ESP_RETURN_ON_ERROR(ftdi_control_transfer(dev, request_type, FTDI_SIO_SET_BAUD_RATE, ftdi_encode_baudrate(baudrate), FTDI_DEFAULT_INDEX, NULL, 0), TAG, "FTDI baudrate failed");

    ESP_LOGI(TAG, "FTDI configured at %lu baud", (unsigned long)baudrate);
    return ESP_OK;
}

static esp_err_t cdc_acm_configure_device(ftdi_device_t *dev, uint32_t baudrate)
{
    struct {
        uint32_t dw_dte_rate;
        uint8_t b_char_format;
        uint8_t b_parity_type;
        uint8_t b_data_bits;
    } line_coding = {
        .dw_dte_rate = baudrate,
        .b_char_format = 0,
        .b_parity_type = 0,
        .b_data_bits = 8,
    };

    const uint8_t request_type = USB_BM_REQUEST_TYPE_DIR_OUT |
                                 USB_BM_REQUEST_TYPE_TYPE_CLASS |
                                 USB_BM_REQUEST_TYPE_RECIP_INTERFACE;

    ESP_RETURN_ON_ERROR(ftdi_control_transfer(dev,
                                              request_type,
                                              CDC_ACM_SET_LINE_CODING,
                                              0,
                                              dev->control_interface_number,
                                              &line_coding,
                                              sizeof(line_coding)),
                        TAG,
                        "CDC SET_LINE_CODING failed");

    esp_err_t line_state_err = ftdi_control_transfer(dev,
                                                     request_type,
                                                     CDC_ACM_SET_CONTROL_LINE_STATE,
                                                     CDC_ACM_CONTROL_LINE_DTR | CDC_ACM_CONTROL_LINE_RTS,
                                                     dev->control_interface_number,
                                                     NULL,
                                                     0);
    if (line_state_err != ESP_OK) {
        ESP_LOGW(TAG,
                 "CDC SET_CONTROL_LINE_STATE was rejected (%s), continuing anyway",
                 esp_err_to_name(line_state_err));
    }

    ESP_LOGI(TAG, "CDC ACM configured at %lu baud on control interface %u", (unsigned long)baudrate, dev->control_interface_number);
    return ESP_OK;
}

static bool ftdi_find_bulk_interface(ftdi_device_t *dev, const usb_config_desc_t *config_desc)
{
    dev->control_interface_number = 0xFF;

    for (uint8_t intf_num = 0; intf_num < config_desc->bNumInterfaces; intf_num++) {
        int intf_offset = 0;
        const usb_intf_desc_t *intf_desc = usb_parse_interface_descriptor(config_desc, intf_num, 0, &intf_offset);
        if (intf_desc == NULL) {
            continue;
        }

        if (intf_desc->bInterfaceClass == USB_CLASS_CDC_COMM && dev->control_interface_number == 0xFF) {
            dev->control_interface_number = intf_desc->bInterfaceNumber;
        }

        uint8_t ep_in = 0;
        uint8_t ep_out = 0;
        uint16_t ep_mps_in = 0;
        uint16_t ep_mps_out = 0;

        for (int ep_index = 0; ep_index < intf_desc->bNumEndpoints; ep_index++) {
            int ep_offset = intf_offset;
            const usb_ep_desc_t *ep_desc = usb_parse_endpoint_descriptor_by_index(intf_desc, ep_index, config_desc->wTotalLength, &ep_offset);
            if (ep_desc == NULL) {
                continue;
            }

            if ((ep_desc->bmAttributes & USB_BM_ATTRIBUTES_XFERTYPE_MASK) != USB_BM_ATTRIBUTES_XFER_BULK) {
                continue;
            }

            if (USB_EP_DESC_GET_EP_DIR(ep_desc)) {
                ep_in = ep_desc->bEndpointAddress;
                ep_mps_in = USB_EP_DESC_GET_MPS(ep_desc);
            } else {
                ep_out = ep_desc->bEndpointAddress;
                ep_mps_out = USB_EP_DESC_GET_MPS(ep_desc);
            }
        }

        if (ep_in != 0 && ep_out != 0) {
            dev->interface_number = intf_desc->bInterfaceNumber;
            dev->interface_alt = intf_desc->bAlternateSetting;
            dev->ep_in = ep_in;
            dev->ep_out = ep_out;
            dev->ep_mps_in = ep_mps_in;
            dev->ep_mps_out = ep_mps_out;
            dev->interface_class = intf_desc->bInterfaceClass;
            dev->interface_subclass = intf_desc->bInterfaceSubClass;
            dev->interface_protocol = intf_desc->bInterfaceProtocol;
            return true;
        }
    }

    return false;
}

static esp_err_t ftdi_open_device(ftdi_device_t *dev)
{
    const usb_device_desc_t *dev_desc = NULL;
    const usb_config_desc_t *config_desc = NULL;

    ESP_RETURN_ON_ERROR(usb_host_device_open(dev->client_hdl, dev->dev_addr, &dev->dev_hdl), TAG, "device open failed");
    ESP_RETURN_ON_ERROR(usb_host_get_device_descriptor(dev->dev_hdl, &dev_desc), TAG, "descriptor read failed");

    dev->vid = dev_desc->idVendor;
    dev->pid = dev_desc->idProduct;
    dev->is_ftdi = false;

    if (usb_is_obvious_non_u2d2(dev->vid, dev->pid, dev_desc->bDeviceClass)) {
        ESP_LOGD(TAG, "addr=%u vid=%04x pid=%04x class=0x%02x — hub/camera, skip",
                 dev->dev_addr, dev->vid, dev->pid, dev_desc->bDeviceClass);
        usb_host_device_close(dev->client_hdl, dev->dev_hdl);
        dev->dev_hdl = NULL;
        dev->dev_addr = 0;
        return ESP_ERR_NOT_SUPPORTED;
    }

    if (!usb_is_u2d2_candidate(dev->vid, dev->pid)) {
        ESP_LOGD(TAG, "addr=%u vid=%04x pid=%04x — not a known U2D2 VID", dev->dev_addr,
                 dev->vid, dev->pid);
        usb_host_device_close(dev->client_hdl, dev->dev_hdl);
        dev->dev_hdl = NULL;
        dev->dev_addr = 0;
        return ESP_ERR_NOT_SUPPORTED;
    }

    ESP_RETURN_ON_ERROR(usb_host_get_active_config_descriptor(dev->dev_hdl, &config_desc), TAG, "config descriptor read failed");
    if (!ftdi_find_bulk_interface(dev, config_desc)) {
        ESP_LOGW(TAG, "Device %04x:%04x has no bulk IN/OUT pair we can use", dev->vid, dev->pid);
        usb_host_device_close(dev->client_hdl, dev->dev_hdl);
        dev->dev_hdl = NULL;
        dev->dev_addr = 0;
        return ESP_ERR_NOT_FOUND;
    }

    /* USB-C U2D2 is FT232HL with ROBOTIS 16d0:06a7 — same FTDI SIO as 0403:6014. */
    dev->is_ftdi = usb_device_uses_ftdi_sio(dev);

    usb_device_info_t info = {0};
    const bool have_info = (usb_host_device_info(dev->dev_hdl, &info) == ESP_OK);
    const bool on_hub = have_info && (info.parent.dev_hdl != NULL);
    ESP_LOGI(TAG,
             "U2D2 candidate %04x:%04x class=0x%02x proto=%s speed=%s parent=%s mps_in=%u mps_out=%u",
             dev->vid, dev->pid, dev->interface_class,
             dev->is_ftdi ? "FTDI-SIO" : "CDC",
             have_info ? usb_speed_str(info.speed) : "?",
             on_hub ? "hub" : "root",
             (unsigned)dev->ep_mps_in, (unsigned)dev->ep_mps_out);
    if (on_hub && have_info && info.speed != USB_SPEED_HIGH) {
        ESP_LOGE(TAG,
                 "U2D2 is %s behind a High-Speed hub — ESP-IDF has no Transaction Translator. "
                 "Use a USB 2.0 HS hub that keeps FT232H at High Speed, or plug U2D2 into the HOST Type-A port directly.",
                 usb_speed_str(info.speed));
    }

    bool claimed_ctrl = false;
    bool claimed_data = false;
    esp_err_t err;

    if (!dev->is_ftdi && usb_iface_is_cdc_acm(dev)) {
        err = usb_host_interface_claim(dev->client_hdl, dev->dev_hdl, dev->control_interface_number, 0);
        if (err != ESP_OK) {
            ESP_LOGE(TAG, "CDC control claim failed: %s", esp_err_to_name(err));
            goto open_fail;
        }
        claimed_ctrl = true;
    }
    err = usb_host_interface_claim(dev->client_hdl, dev->dev_hdl, dev->interface_number, dev->interface_alt);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "interface claim failed: %s (mps_in=%u mps_out=%u)",
                 esp_err_to_name(err), (unsigned)dev->ep_mps_in, (unsigned)dev->ep_mps_out);
        goto open_fail;
    }
    claimed_data = true;
    err = usb_host_transfer_alloc(USB_SETUP_PACKET_SIZE, 0, &dev->ctrl_xfer);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "control alloc failed: %s", esp_err_to_name(err));
        goto open_fail;
    }
    err = usb_host_transfer_alloc(dev->ep_mps_in, 0, &dev->bulk_in_xfer);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "bulk IN alloc failed: %s", esp_err_to_name(err));
        goto open_fail;
    }
    err = usb_host_transfer_alloc(dev->ep_mps_out + FTDI_TX_HEADER_SIZE, 0, &dev->bulk_out_xfer);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "bulk OUT alloc failed: %s", esp_err_to_name(err));
        goto open_fail;
    }

    dev->bulk_in_xfer->device_handle = dev->dev_hdl;
    dev->bulk_in_xfer->bEndpointAddress = dev->ep_in;
    dev->bulk_in_xfer->callback = bulk_in_transfer_cb;
    dev->bulk_in_xfer->context = dev;

    dev->bulk_out_xfer->device_handle = dev->dev_hdl;
    dev->bulk_out_xfer->bEndpointAddress = dev->ep_out;

    if (dev->is_ftdi) {
        err = ftdi_configure_device(dev, DXL_DEFAULT_BAUDRATE);
        if (err != ESP_OK) {
            ESP_LOGE(TAG, "FTDI configure failed: %s", esp_err_to_name(err));
            goto open_fail;
        }
    } else if (usb_iface_is_cdc_acm(dev)) {
        err = cdc_acm_configure_device(dev, DXL_DEFAULT_BAUDRATE);
        if (err != ESP_OK) {
            ESP_LOGE(TAG, "CDC configure failed: %s", esp_err_to_name(err));
            goto open_fail;
        }
    } else {
        ESP_LOGW(TAG, "U2D2 candidate %04x:%04x class=0x%02x has no usable CDC/FTDI serial path",
                 dev->vid, dev->pid, dev->interface_class);
        err = ESP_ERR_NOT_SUPPORTED;
        goto open_fail;
    }

    dev->device_ready = true;
    dev->device_gone = false;
    dev->rx_accum_len = 0;

    ESP_LOGI(TAG,
             "USB serial candidate ready: addr=%u vid=%04x pid=%04x proto=%s intf=%u class=0x%02x subclass=0x%02x protocol=0x%02x ep_in=0x%02x ep_out=0x%02x",
             dev->dev_addr,
             dev->vid,
             dev->pid,
             dev->is_ftdi ? "FTDI-SIO" : "CDC-ACM",
             dev->interface_number,
             dev->interface_class,
             dev->interface_subclass,
             dev->interface_protocol,
             dev->ep_in,
             dev->ep_out);
    return ESP_OK;

open_fail:
    if (dev->bulk_in_xfer != NULL) {
        usb_host_transfer_free(dev->bulk_in_xfer);
        dev->bulk_in_xfer = NULL;
    }
    if (dev->bulk_out_xfer != NULL) {
        usb_host_transfer_free(dev->bulk_out_xfer);
        dev->bulk_out_xfer = NULL;
    }
    if (dev->ctrl_xfer != NULL) {
        usb_host_transfer_free(dev->ctrl_xfer);
        dev->ctrl_xfer = NULL;
    }
    if (dev->dev_hdl != NULL) {
        if (claimed_ctrl) {
            usb_host_interface_release(dev->client_hdl, dev->dev_hdl, dev->control_interface_number);
        }
        if (claimed_data) {
            usb_host_interface_release(dev->client_hdl, dev->dev_hdl, dev->interface_number);
        }
        usb_host_device_close(dev->client_hdl, dev->dev_hdl);
        dev->dev_hdl = NULL;
    }
    dev->dev_addr = 0;
    return err;
}

static void ftdi_close_device(ftdi_device_t *dev)
{
    dev->device_ready = false;
    dev->device_gone = true;

    if (dev->bulk_in_xfer != NULL) {
        usb_host_transfer_free(dev->bulk_in_xfer);
        dev->bulk_in_xfer = NULL;
    }
    if (dev->bulk_out_xfer != NULL) {
        usb_host_transfer_free(dev->bulk_out_xfer);
        dev->bulk_out_xfer = NULL;
    }
    if (dev->ctrl_xfer != NULL) {
        usb_host_transfer_free(dev->ctrl_xfer);
        dev->ctrl_xfer = NULL;
    }
    if (dev->dev_hdl != NULL) {
        if (dev->control_interface_number != 0xFF) {
            usb_host_interface_release(dev->client_hdl, dev->dev_hdl, dev->control_interface_number);
        }
        usb_host_interface_release(dev->client_hdl, dev->dev_hdl, dev->interface_number);
        usb_host_device_close(dev->client_hdl, dev->dev_hdl);
    }

    dev->dev_hdl = NULL;
    dev->dev_addr = 0;
    dev->vid = 0;
    dev->pid = 0;
    dev->is_ftdi = false;
    dev->interface_number = 0;
    dev->interface_alt = 0;
    dev->ep_in = 0;
    dev->ep_out = 0;
    dev->ep_mps_in = 0;
    dev->ep_mps_out = 0;
    dev->interface_class = 0;
    dev->interface_subclass = 0;
    dev->interface_protocol = 0;
    dev->control_interface_number = 0xFF;
    dev->rx_accum_len = 0;

    ESP_LOGI(TAG, "FTDI/U2D2 closed");
}

static esp_err_t ftdi_start_rx(ftdi_device_t *dev)
{
    if (!dev->device_ready || dev->bulk_in_xfer == NULL) {
        return ESP_ERR_INVALID_STATE;
    }

    dev->bulk_in_xfer->num_bytes = usb_round_up_to_mps(dev->ep_mps_in, dev->ep_mps_in);
    dev->bulk_in_xfer->timeout_ms = 0;
    dev->bulk_in_xfer->callback = bulk_in_transfer_cb;
    dev->bulk_in_xfer->context = dev;

    ESP_RETURN_ON_ERROR(usb_host_transfer_submit(dev->bulk_in_xfer), TAG, "bulk IN submit failed");
    return ESP_OK;
}

static esp_err_t ftdi_uart_write(ftdi_device_t *dev, const uint8_t *data, size_t len)
{
    if (!dev->device_ready || dev->bulk_out_xfer == NULL) {
        return ESP_ERR_INVALID_STATE;
    }
    if (len == 0) {
        return ESP_ERR_INVALID_ARG;
    }
    if (dev->is_ftdi && len > FTDI_TX_MAX_PAYLOAD) {
        return ESP_ERR_INVALID_SIZE;
    }
    const size_t tx_len = dev->is_ftdi ? (len + FTDI_TX_HEADER_SIZE) : len;
    if (tx_len > (size_t)dev->bulk_out_xfer->data_buffer_size) {
        return ESP_ERR_INVALID_SIZE;
    }

    if (dev->is_ftdi) {
        dev->bulk_out_xfer->data_buffer[0] = (uint8_t)(0x01 | ((len & 0x3F) << 2));
        memcpy(dev->bulk_out_xfer->data_buffer + FTDI_TX_HEADER_SIZE, data, len);
    } else {
        memcpy(dev->bulk_out_xfer->data_buffer, data, len);
    }

    transfer_wait_t waiter = {
        .done = false,
        .result = ESP_FAIL,
    };

    dev->bulk_out_xfer->num_bytes = (int)tx_len;
    dev->bulk_out_xfer->actual_num_bytes = 0;
    dev->bulk_out_xfer->flags = 0;
    dev->bulk_out_xfer->timeout_ms = 1000;
    dev->bulk_out_xfer->callback = bulk_out_transfer_cb;
    dev->bulk_out_xfer->context = &waiter;

    ESP_RETURN_ON_ERROR(usb_host_transfer_submit(dev->bulk_out_xfer), TAG, "bulk OUT submit failed");
    return wait_for_transfer(dev, &waiter, pdMS_TO_TICKS(1000));
}

static size_t dynamixel_build_instruction_packet(uint8_t id,
                                                 uint8_t instruction,
                                                 const uint8_t *params,
                                                 size_t params_len,
                                                 uint8_t *packet,
                                                 size_t packet_size)
{
    const size_t total_len = 6 + params_len;
    const uint8_t dxl_len = (uint8_t)(params_len + 2);

    if (packet_size < total_len || params_len > DXL_MAX_PARAMS) {
        return 0;
    }

    packet[0] = DXL_HEADER_0;
    packet[1] = DXL_HEADER_1;
    packet[2] = id;
    packet[3] = dxl_len;
    packet[4] = instruction;
    if (params_len > 0 && params != NULL) {
        memcpy(&packet[5], params, params_len);
    }

    packet[5 + params_len] = dynamixel_v1_checksum(&packet[2], 3 + params_len);
    return total_len;
}

static esp_err_t dynamixel_send_ping(ftdi_device_t *dev, uint8_t id)
{
    uint8_t packet[DXL_MAX_PACKET_SIZE];
    size_t packet_len = dynamixel_build_instruction_packet(id, DXL_INST_PING, NULL, 0, packet, sizeof(packet));
    if (packet_len == 0) {
        return ESP_ERR_INVALID_SIZE;
    }

    ESP_LOGD(TAG, "Sending PING to Dynamixel ID %u", id);
    return ftdi_uart_write(dev, packet, packet_len);
}

static esp_err_t dynamixel_send_read(ftdi_device_t *dev, uint8_t id, uint8_t addr, uint8_t len)
{
    uint8_t params[2] = {
        addr,
        len,
    };
    uint8_t packet[DXL_MAX_PACKET_SIZE];
    size_t packet_len =
        dynamixel_build_instruction_packet(id, DXL_INST_READ, params, sizeof(params), packet, sizeof(packet));
    if (packet_len == 0) {
        return ESP_ERR_INVALID_SIZE;
    }

    ESP_LOGD(TAG, "Reading %u bytes from ID %u addr=%u", len, id, addr);
    return ftdi_uart_write(dev, packet, packet_len);
}

static esp_err_t dynamixel_send_write8(ftdi_device_t *dev, uint8_t id, uint8_t addr, uint8_t value)
{
    uint8_t params[2] = {
        addr,
        value,
    };
    uint8_t packet[DXL_MAX_PACKET_SIZE];
    size_t packet_len = dynamixel_build_instruction_packet(id, DXL_INST_WRITE, params, sizeof(params), packet, sizeof(packet));
    if (packet_len == 0) {
        return ESP_ERR_INVALID_SIZE;
    }

    ESP_LOGI(TAG, "Writing 8-bit value %u to ID %u addr=%u", value, id, addr);
    return ftdi_uart_write(dev, packet, packet_len);
}

static esp_err_t dynamixel_send_write16(ftdi_device_t *dev, uint8_t id, uint8_t addr, uint16_t value)
{
    uint8_t params[3] = {
        addr,
        (uint8_t)(value & 0xFF),
        (uint8_t)((value >> 8) & 0xFF),
    };
    uint8_t packet[DXL_MAX_PACKET_SIZE];
    size_t packet_len = dynamixel_build_instruction_packet(id, DXL_INST_WRITE, params, sizeof(params), packet, sizeof(packet));
    if (packet_len == 0) {
        return ESP_ERR_INVALID_SIZE;
    }

    ESP_LOGI(TAG, "Writing 16-bit value %u to ID %u addr=%u", value, id, addr);
    return ftdi_uart_write(dev, packet, packet_len);
}

static esp_err_t dynamixel_send_write8_all(ftdi_device_t *dev, uint8_t addr, uint8_t value)
{
    esp_err_t first_err = ESP_OK;

    for (size_t i = 0; i < DXL_SERVO_COUNT; i++) {
        esp_err_t err = dynamixel_send_write8(dev, s_servo_ids[i], addr, value);
        if (err != ESP_OK && first_err == ESP_OK) {
            first_err = err;
        }
    }

    return first_err;
}

static esp_err_t dynamixel_send_write16_all(ftdi_device_t *dev, uint8_t addr, uint16_t value)
{
    esp_err_t first_err = ESP_OK;

    for (size_t i = 0; i < DXL_SERVO_COUNT; i++) {
        esp_err_t err = dynamixel_send_write16(dev, s_servo_ids[i], addr, value);
        if (err != ESP_OK && first_err == ESP_OK) {
            first_err = err;
        }
    }

    return first_err;
}

static esp_err_t dynamixel_set_joint_mode(ftdi_device_t *dev)
{
    esp_err_t err = dynamixel_send_write8_all(dev, DXL_TORQUE_ENABLE_ADDR, DXL_TORQUE_OFF);
    if (err != ESP_OK) {
        return err;
    }

    err = dynamixel_send_write16_all(dev, DXL_CW_ANGLE_LIMIT_ADDR, 0);
    if (err != ESP_OK) {
        return err;
    }

    err = dynamixel_send_write16_all(dev, DXL_CCW_ANGLE_LIMIT_ADDR, DXL_AX_JOINT_CCW_LIMIT);
    if (err != ESP_OK) {
        return err;
    }

    err = dynamixel_send_write8_all(dev, DXL_TORQUE_ENABLE_ADDR, DXL_TORQUE_ON);
    if (err == ESP_OK) {
        s_torque_enabled = true;
        for (size_t i = 0; i < DXL_SERVO_COUNT; i++) {
            s_servo_torque_on[i] = true;
            s_requested_torque_on[i] = true;
            s_torque_update_pending[i] = false;
        }
        ESP_LOGI(TAG, "Dynamixel joint (position) mode enabled");
    }

    return err;
}

static void dynamixel_handle_status_packet(const dynamixel_status_packet_t *packet)
{
    if (packet == NULL || s_sync_mutex == NULL || s_read_done_sem == NULL) {
        return;
    }

    if (xSemaphoreTake(s_sync_mutex, pdMS_TO_TICKS(100)) != pdTRUE) {
        return;
    }

    if (s_sync.state == DXL_SYNC_SCAN_WAIT_RSP) {
        if (packet->error == 0) {
            scan_add_id(packet->id);
        }
        xSemaphoreGive(s_sync_mutex);
        return;
    }

    if ((s_sync.state == DXL_SYNC_READ_WAIT_RSP || s_sync.state == DXL_SYNC_PING_WAIT_RSP) &&
        packet->id == s_sync.id) {
        if (packet->error != 0) {
            s_sync.result = ESP_FAIL;
        } else if (s_sync.state == DXL_SYNC_PING_WAIT_RSP) {
            s_sync.result = ESP_OK;
        } else if (packet->param_len >= s_sync.length) {
            s_sync.value = packet->params[0];
            if (s_sync.length >= 2) {
                s_sync.value |= (uint16_t)packet->params[1] << 8;
            }
            s_sync.result = ESP_OK;
        } else {
            s_sync.result = ESP_ERR_INVALID_RESPONSE;
        }
        s_sync.state = DXL_SYNC_IDLE;
        xSemaphoreGive(s_sync_mutex);
        xSemaphoreGive(s_read_done_sem);
        return;
    }

    xSemaphoreGive(s_sync_mutex);
}

static bool dynamixel_try_parse_packet(ftdi_device_t *dev)
{
    uint8_t *buf = dev->rx_accum;
    size_t len = dev->rx_accum_len;

    while (len >= 2) {
        if (buf[0] == DXL_HEADER_0 && buf[1] == DXL_HEADER_1) {
            break;
        }
        memmove(buf, buf + 1, len - 1);
        len--;
    }

    dev->rx_accum_len = len;
    if (len < 6) {
        return false;
    }

    uint8_t declared_len = buf[3];
    size_t total_packet_len = (size_t)declared_len + 4;
    if (total_packet_len > len) {
        return false;
    }

    uint8_t rx_checksum = buf[total_packet_len - 1];
    uint8_t calc_checksum = dynamixel_v1_checksum(&buf[2], total_packet_len - 3);
    if (rx_checksum != calc_checksum) {
        ESP_LOGW(TAG, "Dropping packet with checksum mismatch");
        memmove(buf, buf + 1, len - 1);
        dev->rx_accum_len = len - 1;
        return true;
    }

    dynamixel_status_packet_t status = {
        .id = buf[2],
        .error = buf[4],
        .param_len = (uint16_t)(declared_len - 2),
    };

    if (status.param_len > DXL_MAX_PARAMS) {
        status.param_len = DXL_MAX_PARAMS;
    }
    if (status.param_len > 0) {
        memcpy(status.params, &buf[5], status.param_len);
    }
    dynamixel_handle_status_packet(&status);

    memmove(buf, buf + total_packet_len, len - total_packet_len);
    dev->rx_accum_len = len - total_packet_len;
    return true;
}

static void dynamixel_rx_consume(ftdi_device_t *dev, const uint8_t *data, size_t len)
{
    if (len == 0) {
        return;
    }

    if ((dev->rx_accum_len + len) > sizeof(dev->rx_accum)) {
        ESP_LOGW(TAG, "RX buffer overflow, resetting parser");
        dev->rx_accum_len = 0;
    }

    memcpy(&dev->rx_accum[dev->rx_accum_len], data, len);
    dev->rx_accum_len += len;

    while (dynamixel_try_parse_packet(dev)) {
    }
}

static void ctrl_transfer_cb(usb_transfer_t *transfer)
{
    transfer_wait_t *waiter = (transfer_wait_t *)transfer->context;
    waiter->result = (transfer->status == USB_TRANSFER_STATUS_COMPLETED) ? ESP_OK : ESP_FAIL;
    waiter->done = true;
}

static void bulk_out_transfer_cb(usb_transfer_t *transfer)
{
    transfer_wait_t *waiter = (transfer_wait_t *)transfer->context;
    waiter->result = (transfer->status == USB_TRANSFER_STATUS_COMPLETED) ? ESP_OK : ESP_FAIL;
    waiter->done = true;
}

static void bulk_in_transfer_cb(usb_transfer_t *transfer)
{
    ftdi_device_t *dev = (ftdi_device_t *)transfer->context;

    if (transfer->status == USB_TRANSFER_STATUS_COMPLETED) {
        size_t payload_offset = dev->is_ftdi ? FTDI_RX_HEADER_SIZE : 0;
        if ((size_t)transfer->actual_num_bytes > payload_offset) {
            dynamixel_rx_consume(dev,
                                 transfer->data_buffer + payload_offset,
                                 (size_t)transfer->actual_num_bytes - payload_offset);
        }
    } else if (transfer->status == USB_TRANSFER_STATUS_NO_DEVICE) {
        dev->device_gone = true;
        return;
    } else {
        ESP_LOGW(TAG, "bulk IN transfer status=%d", transfer->status);
    }

    if (dev->device_ready && !dev->device_gone) {
        transfer->num_bytes = usb_round_up_to_mps(dev->ep_mps_in, dev->ep_mps_in);
        transfer->actual_num_bytes = 0;
        esp_err_t err = usb_host_transfer_submit(transfer);
        if (err != ESP_OK) {
            ESP_LOGW(TAG, "bulk IN resubmit failed: %s", esp_err_to_name(err));
        }
    }
}

typedef struct {
    uint16_t vid;
    uint16_t pid;
    uint8_t dev_class;
    usb_speed_t speed;
    bool on_hub;
} servo_usb_peek_t;

static uint8_t s_usb_skip_addr[DXL_USB_ADDR_LIST_MAX];
static uint8_t s_usb_skip_n;

static bool servo_usb_addr_skipped(uint8_t addr)
{
    for (uint8_t i = 0; i < s_usb_skip_n; i++) {
        if (s_usb_skip_addr[i] == addr) {
            return true;
        }
    }
    return false;
}

static void servo_usb_skip_add(uint8_t addr)
{
    if (servo_usb_addr_skipped(addr) || s_usb_skip_n >= DXL_USB_ADDR_LIST_MAX) {
        return;
    }
    s_usb_skip_addr[s_usb_skip_n++] = addr;
}

static void servo_usb_skip_retain(const uint8_t *alive, int n_alive)
{
    uint8_t kept[DXL_USB_ADDR_LIST_MAX];
    uint8_t n_kept = 0;
    for (uint8_t i = 0; i < s_usb_skip_n; i++) {
        for (int j = 0; j < n_alive; j++) {
            if (alive[j] == s_usb_skip_addr[i]) {
                kept[n_kept++] = s_usb_skip_addr[i];
                break;
            }
        }
    }
    memcpy(s_usb_skip_addr, kept, n_kept);
    s_usb_skip_n = n_kept;
}

static esp_err_t servo_peek_usb_dev(ftdi_device_t *dev, uint8_t addr, servo_usb_peek_t *out)
{
    usb_device_handle_t hdl = NULL;
    esp_err_t err = usb_host_device_open(dev->client_hdl, addr, &hdl);
    if (err != ESP_OK) {
        return err;
    }
    const usb_device_desc_t *desc = NULL;
    err = usb_host_get_device_descriptor(hdl, &desc);
    if (err == ESP_OK) {
        out->vid = desc->idVendor;
        out->pid = desc->idProduct;
        out->dev_class = desc->bDeviceClass;
        usb_device_info_t info = {0};
        if (usb_host_device_info(hdl, &info) == ESP_OK) {
            out->speed = info.speed;
            out->on_hub = (info.parent.dev_hdl != NULL);
        } else {
            out->speed = USB_SPEED_FULL;
            out->on_hub = false;
        }
    }
    usb_host_device_close(dev->client_hdl, hdl);
    return err;
}

/** Scan USB tree and open U2D2 (FTDI 0403:* or ROBOTIS 16d0:06a7 FT232H). */
static void servo_try_attach_ftdi(ftdi_device_t *dev)
{
    if (dev->client_hdl == NULL || dev->device_ready) {
        return;
    }

    uint8_t addr_list[DXL_USB_ADDR_LIST_MAX];
    uint8_t try_order[DXL_USB_ADDR_LIST_MAX];
    uint16_t try_vid[DXL_USB_ADDR_LIST_MAX];
    int num_dev = 0;
    int num_try = 0;
    if (usb_host_device_addr_list_fill((int)sizeof(addr_list), addr_list, &num_dev) != ESP_OK ||
        num_dev <= 0) {
        return;
    }

    servo_usb_skip_retain(addr_list, num_dev);

    static int s_last_bus_count = -1;
    const bool log_bus = (num_dev != s_last_bus_count);
    s_last_bus_count = num_dev;
    if (log_bus) {
        ESP_LOGI(TAG, "USB bus: %d device(s)", num_dev);
    } else {
        ESP_LOGD(TAG, "USB bus scan: %d device(s)", num_dev);
    }

    for (int i = 0; i < num_dev; i++) {
        servo_usb_peek_t peek = {0};
        if (servo_usb_addr_skipped(addr_list[i])) {
            continue;
        }
        (void)usb_host_client_handle_events(dev->client_hdl, pdMS_TO_TICKS(20));
        esp_err_t peek_err = servo_peek_usb_dev(dev, addr_list[i], &peek);
        if (peek_err != ESP_OK) {
            /* Camera/hub already opened by another client. */
            if (peek_err == ESP_ERR_INVALID_STATE) {
                servo_usb_skip_add(addr_list[i]);
            }
            continue;
        }
        if (log_bus) {
            ESP_LOGI(TAG, "  addr=%u %04x:%04x class=0x%02x speed=%s parent=%s%s",
                     addr_list[i], peek.vid, peek.pid, peek.dev_class,
                     usb_speed_str(peek.speed), peek.on_hub ? "hub" : "root",
                     usb_is_u2d2_candidate(peek.vid, peek.pid) ? " [U2D2]" : "");
        }
        if (usb_is_obvious_non_u2d2(peek.vid, peek.pid, peek.dev_class) ||
            !usb_is_u2d2_candidate(peek.vid, peek.pid)) {
            servo_usb_skip_add(addr_list[i]);
            continue;
        }
        if (peek.on_hub && peek.speed != USB_SPEED_HIGH) {
            ESP_LOGW(TAG,
                     "U2D2 at addr %u is %s on a hub — ESP-IDF cannot talk FS/LS through an HS hub (no TT)",
                     addr_list[i], usb_speed_str(peek.speed));
        }
        try_order[num_try] = addr_list[i];
        try_vid[num_try] = peek.vid;
        num_try++;
    }

    /* Prefer ROBOTIS U2D2 on the hub over any other serial adapter. */
    for (int pass = 0; pass < 2 && !dev->device_ready; pass++) {
        for (int i = 0; i < num_try; i++) {
            const bool robotis = (try_vid[i] == ROBOTIS_VID);
            if (pass == 0 && !robotis) {
                continue;
            }
            if (pass == 1 && robotis) {
                continue;
            }
            if (dev->device_ready) {
                return;
            }
            (void)usb_host_client_handle_events(dev->client_hdl, pdMS_TO_TICKS(50));
            dev->dev_addr = try_order[i];
            esp_err_t err = ftdi_open_device(dev);
            if (err == ESP_OK) {
                goto attached;
            }
            dev->dev_addr = 0;
            vTaskDelay(pdMS_TO_TICKS(40));
        }
    }
    return;

attached:
    s_ping_fail_streak = 0;
    s_torque_enabled = false;
    s_position_speed_pending = true;
    dynamixel_queue_goal_all(DXL_CENTER_POSITION);
    if (ftdi_start_rx(dev) != ESP_OK) {
        ESP_LOGW(TAG, "bulk IN start failed after U2D2 open");
    }
    ESP_LOGI(TAG, "U2D2 attached addr=%u vid=%04x pid=%04x (homing to %d)", dev->dev_addr, dev->vid,
             dev->pid, DXL_CENTER_POSITION);
}

static void client_event_cb(const usb_host_client_event_msg_t *event_msg, void *arg)
{
    ftdi_device_t *dev = (ftdi_device_t *)arg;

    switch (event_msg->event) {
    case USB_HOST_CLIENT_EVENT_NEW_DEV:
        if (dev->device_ready) {
            break;
        }
        /* Do not open this address blindly — it is often the hub or camera. */
        dev->actions |= DEVICE_ACTION_OPEN;
        ESP_LOGI(TAG, "USB device at address %u — scan bus for U2D2",
                 event_msg->new_dev.address);
        break;
    case USB_HOST_CLIENT_EVENT_DEV_GONE:
        /* IDF 5.5: dev_gone carries dev_hdl (only for devices this client opened). */
        if (dev->device_ready && dev->dev_hdl != NULL &&
            event_msg->dev_gone.dev_hdl == dev->dev_hdl) {
            dev->device_gone = true;
            dev->actions |= DEVICE_ACTION_CLOSE;
            ESP_LOGW(TAG, "U2D2 removed from hub (addr %u)", dev->dev_addr);
        }
        break;
    default:
        break;
    }
}

static void usb_client_task(void *arg)
{
    (void)arg;
    usb_host_client_config_t client_config = {
        .is_synchronous = false,
        .max_num_event_msg = 16,
        .async = {
            .client_event_callback = client_event_cb,
            .callback_arg = &s_ftdi,
        },
    };

    ESP_ERROR_CHECK(usb_host_client_register(&client_config, &s_ftdi.client_hdl));
    ESP_LOGI(TAG, "Servo USB client ready — U2D2 on HS host (FTDI 0403 or ROBOTIS 16d0:06a7 FT232H)");

    TickType_t last_poll_tick = 0;
    TickType_t last_attach_scan_tick = 0;

    /* Hub downstream devices enumerate after the hub — scan until U2D2 appears. */
    for (int attempt = 0; attempt < DXL_HUB_SETTLE_ATTEMPTS && !s_ftdi.device_ready; attempt++) {
        (void)usb_host_client_handle_events(s_ftdi.client_hdl, pdMS_TO_TICKS(50));
        servo_try_attach_ftdi(&s_ftdi);
        vTaskDelay(pdMS_TO_TICKS(150));
    }
    last_attach_scan_tick = xTaskGetTickCount();
    if (!s_ftdi.device_ready) {
        ESP_LOGW(TAG, "U2D2 not found yet — keep scanning (hub+camera on J18?)");
    }

    while (1) {
        if (s_ftdi.actions & DEVICE_ACTION_OPEN) {
            s_ftdi.actions &= ~DEVICE_ACTION_OPEN;
            if (!s_ftdi.device_ready) {
                servo_try_attach_ftdi(&s_ftdi);
            }
        }

        if (s_ftdi.actions & DEVICE_ACTION_CLOSE) {
            s_ftdi.actions &= ~DEVICE_ACTION_CLOSE;
            if (s_ftdi.dev_hdl != NULL) {
                ftdi_close_device(&s_ftdi);
                s_torque_enabled = false;
                for (size_t i = 0; i < DXL_SERVO_COUNT; i++) {
                    s_servo_torque_on[i] = false;
                    s_torque_update_pending[i] = false;
                }
                s_ftdi.dev_addr = 0;
            }
            s_ping_fail_streak = 0;
            last_attach_scan_tick = 0;
        }

        if (!s_ftdi.device_ready) {
            const TickType_t now = xTaskGetTickCount();
            if ((now - last_attach_scan_tick) >= pdMS_TO_TICKS(DXL_ATTACH_RETRY_MS)) {
                servo_try_attach_ftdi(&s_ftdi);
                last_attach_scan_tick = now;
            }
        }

        if (s_ftdi.device_ready && !s_ftdi.device_gone) {
            TickType_t now = xTaskGetTickCount();
            bool has_goal_update_pending = false;
            bool has_torque_update_pending = false;
            for (size_t i = 0; i < DXL_SERVO_COUNT; i++) {
                if (s_goal_update_pending[i]) {
                    has_goal_update_pending = true;
                }
                if (s_torque_update_pending[i]) {
                    has_torque_update_pending = true;
                }
            }
            bool has_sync_read_pending = false;
            if (s_sync_mutex != NULL && xSemaphoreTake(s_sync_mutex, 0) == pdTRUE) {
                has_sync_read_pending = (s_sync.state == DXL_SYNC_READ_PENDING);
                xSemaphoreGive(s_sync_mutex);
            }
            const TickType_t poll_ms =
                (has_goal_update_pending || has_sync_read_pending || has_torque_update_pending)
                    ? pdMS_TO_TICKS(DXL_FAST_POLL_INTERVAL_MS)
                    : pdMS_TO_TICKS(DXL_APP_POLL_INTERVAL_MS);
            if ((now - last_poll_tick) >= poll_ms) {
                esp_err_t err = ESP_OK;

                if (s_sync_mutex != NULL && xSemaphoreTake(s_sync_mutex, 0) == pdTRUE) {
                    if (s_sync.state == DXL_SYNC_READ_PENDING) {
                        err = dynamixel_send_read(&s_ftdi, s_sync.id, s_sync.addr, s_sync.length);
                        if (err == ESP_OK) {
                            s_sync.state = DXL_SYNC_READ_WAIT_RSP;
                        } else {
                            s_sync.result = err;
                            s_sync.state = DXL_SYNC_IDLE;
                            xSemaphoreGive(s_read_done_sem);
                        }
                    }
                    xSemaphoreGive(s_sync_mutex);
                }

                if (!s_torque_enabled) {
                    bool sync_busy = false;
                    if (s_sync_mutex != NULL && xSemaphoreTake(s_sync_mutex, 0) == pdTRUE) {
                        sync_busy = (s_sync.state != DXL_SYNC_IDLE &&
                                     s_sync.state != DXL_SYNC_READ_PENDING);
                        xSemaphoreGive(s_sync_mutex);
                    }
                    if (!sync_busy) {
                        err = dynamixel_ping_blocking(DXL_PRIMARY_ID, pdMS_TO_TICKS(400));
                    }
                    if (!sync_busy && err != ESP_OK) {
                        s_ping_fail_streak++;
                        if (s_ping_fail_streak == 1 || (s_ping_fail_streak % 4) == 0) {
                            if (err == ESP_ERR_TIMEOUT) {
                                ESP_LOGW(TAG,
                                         "No Dynamixel reply to PING ID%u (%d) — check servo "
                                         "power, TTL wiring, IDs 1&2, 1 Mbps baud, U2D2 on J18",
                                         DXL_PRIMARY_ID, s_ping_fail_streak);
                            } else {
                                ESP_LOGW(TAG, "PING failed (%d): %s", s_ping_fail_streak,
                                         esp_err_to_name(err));
                            }
                        }
                        if (err != ESP_ERR_TIMEOUT &&
                            s_ping_fail_streak >= DXL_PING_FAIL_RECONNECT) {
                            ESP_LOGW(TAG, "U2D2 USB write failed — reopening adapter");
                            s_ping_fail_streak = 0;
                            s_ftdi.actions |= DEVICE_ACTION_CLOSE;
                        }
                    } else if (!sync_busy) {
                        s_ping_fail_streak = 0;
                        ESP_LOGI(TAG, "Dynamixel ID%u responded to PING", DXL_PRIMARY_ID);
                        err = dynamixel_set_joint_mode(&s_ftdi);
                        if (err == ESP_OK) {
                            s_position_speed_pending = true;
                            dynamixel_queue_goal_all(DXL_CENTER_POSITION);
                        }
                    }
                } else if (has_torque_update_pending) {
                    for (size_t i = 0; i < DXL_SERVO_COUNT; i++) {
                        if (!s_torque_update_pending[i]) {
                            continue;
                        }
                        const bool want_on = s_requested_torque_on[i];
                        s_torque_update_pending[i] = false;
                        esp_err_t t_err = dynamixel_send_write8(
                            &s_ftdi, s_servo_ids[i], DXL_TORQUE_ENABLE_ADDR,
                            want_on ? DXL_TORQUE_ON : DXL_TORQUE_OFF);
                        if (t_err == ESP_OK) {
                            s_servo_torque_on[i] = want_on;
                            ESP_LOGI(TAG, "Torque ID%u %s", s_servo_ids[i],
                                     want_on ? "ON" : "OFF");
                        } else {
                            ESP_LOGW(TAG, "Torque ID%u write failed: %s", s_servo_ids[i],
                                     esp_err_to_name(t_err));
                            if (err == ESP_OK) {
                                err = t_err;
                            }
                        }
                    }
                } else if (s_position_speed_pending) {
                    int speed = s_requested_position_speed;
                    s_position_speed_pending = false;
                    err = dynamixel_send_write16_all(&s_ftdi, DXL_MOVING_SPEED_ADDR, (uint16_t)speed);
                    if (err != ESP_OK) {
                        ESP_LOGW(TAG, "Position speed write failed: %s", esp_err_to_name(err));
                    } else {
                        s_active_position_speed = speed;
                        ESP_LOGI(TAG, "Joint-mode moving speed set to %d", speed);
                    }
                } else if (has_goal_update_pending) {
                    if (s_active_position_speed != s_requested_position_speed) {
                        err = dynamixel_send_write16_all(&s_ftdi, DXL_MOVING_SPEED_ADDR,
                                                         (uint16_t)s_requested_position_speed);
                        if (err == ESP_OK) {
                            s_active_position_speed = s_requested_position_speed;
                        }
                    }
                    for (size_t i = 0; i < DXL_SERVO_COUNT; i++) {
                        if (!s_goal_update_pending[i]) {
                            continue;
                        }
                        int goal = s_requested_goal[i];
                        s_goal_update_pending[i] = false;
                        if (!s_servo_torque_on[i]) {
                            continue;
                        }
                        esp_err_t goal_err =
                            dynamixel_send_write16(&s_ftdi, s_servo_ids[i], DXL_GOAL_POSITION_ADDR, (uint16_t)goal);
                        if (goal_err != ESP_OK && err == ESP_OK) {
                            err = goal_err;
                        }
                    }
                    if (err != ESP_OK) {
                        ESP_LOGW(TAG, "GOAL write failed: %s", esp_err_to_name(err));
                    }
                }

                last_poll_tick = now;
            }
        }

        esp_err_t err = usb_host_client_handle_events(s_ftdi.client_hdl, pdMS_TO_TICKS(50));
        if (err != ESP_OK && err != ESP_ERR_TIMEOUT) {
            ESP_LOGW(TAG, "usb_host_client_handle_events: %s", esp_err_to_name(err));
        }
    }
}

esp_err_t nino_servo_dxl_start(void)
{
    if (s_servo_started) {
        return ESP_OK;
    }
    if (s_goal_mutex == NULL) {
        s_goal_mutex = xSemaphoreCreateMutex();
        if (s_goal_mutex == NULL) {
            return ESP_ERR_NO_MEM;
        }
    }
    if (s_sync_mutex == NULL) {
        s_sync_mutex = xSemaphoreCreateMutex();
        if (s_sync_mutex == NULL) {
            return ESP_ERR_NO_MEM;
        }
    }
    if (s_read_done_sem == NULL) {
        s_read_done_sem = xSemaphoreCreateBinary();
        if (s_read_done_sem == NULL) {
            return ESP_ERR_NO_MEM;
        }
    }

    BaseType_t ok = xTaskCreate(usb_client_task, "servo_usb", USB_CLIENT_TASK_STACK_SIZE, NULL,
                                USB_TASK_PRIORITY, NULL);
    if (ok != pdPASS) {
        return ESP_ERR_NO_MEM;
    }

    s_servo_started = true;
    ESP_LOGI(TAG, "Dynamixel servo task started (IDs %u,%u joint mode, neutral=%d)",
             DXL_PRIMARY_ID, DXL_SECONDARY_ID, DXL_CENTER_POSITION);
    return ESP_OK;
}
```

## 13.5 GPIO20 ADC + low-battery monitor

### `main/battery_adc.h` — complete file (38 lines)

Copied from the tree as of this document. Do not edit here; edit the source file.

```c
#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "esp_err.h"

/**
 * On-chip ESP32-P4 ADC battery monitor (GPIO20 = ADC1_CHANNEL_4).
 * Display RST stays on GPIO6. Do not use GPIO54 — that pad is ESP32-C6
 * slave reset and its pull-up lifts the divider (~9.8 V pack reads ~11.5 V).
 *
 * Hardware:
 *   Battery+ -- 22k --+-- GPIO20
 *                     |
 *                  3.3k
 *                     |
 *                  3.3k
 *                     |
 *   Battery- ---------+-- ESP GND
 *
 *   battery_mv = adc_mv * (22000 + 6600) / 6600
 *
 * A 12 V pack should measure ~2.77 V from GPIO20 to GND. Never put pack
 * voltage straight on GPIO20.
 */
typedef struct {
  int16_t raw;
  int32_t adc_mv;
  int32_t battery_mv;
  uint8_t percent;
} nino_battery_sample_t;

esp_err_t nino_battery_adc_init(void);
bool nino_battery_adc_ready(void);
esp_err_t nino_battery_adc_read(nino_battery_sample_t *out);
bool nino_battery_low_alert_active(void);
void nino_battery_adc_cli_register(void);
```

### `main/battery_adc.c` — complete file (500 lines)

Copied from the tree as of this document. Do not edit here; edit the source file.

```c
#include "battery_adc.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "audio_queue.h"
#include "audio_playback.h"
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

#define LOG_TASK_STACK 3072
#define LOG_TASK_PRIO 4
#define LOG_PERIOD_DEFAULT_MS 1000
#define LOG_PERIOD_MIN_MS 200
#define LOG_PERIOD_MAX_MS 10000

#define LOW_ENTER_MV 10000
#define LOW_CLEAR_MV 10400
#define LOW_MIN_VALID_MV 8000
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

static void fill_sample(nino_battery_sample_t *out, int raw) {
  int32_t adc_mv = raw_to_pin_mv(raw);
  int32_t battery_mv = adc_to_battery_mv(adc_mv);
  out->raw = (int16_t)raw;
  out->adc_mv = adc_mv;
  out->battery_mv = battery_mv;
  out->percent = battery_percent(battery_mv);
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

static esp_err_t read_raw_avg(int *raw_out) {
  int32_t sum = 0;
  int got = 0;
  for (int i = 0; i < SAMPLE_COUNT; i++) {
    int raw = 0;
    esp_err_t err = adc_oneshot_read(s_adc, s_channel, &raw);
    if (err != ESP_OK) {
      return err;
    }
    sum += raw;
    got++;
  }
  *raw_out = sum / got;
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
    ESP_LOGI(TAG, "boot GPIO%d raw=%d pin=%ld mV vin=%ld mV %u%% %s",
             BATT_ADC_GPIO, (int)sample.raw, (long)sample.adc_mv,
             (long)sample.battery_mv, (unsigned)sample.percent,
             pack_name(sample.battery_mv));
    warn_if_open(sample.adc_mv);
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
  ESP_LOGW(TAG, "LOW BATTERY %ld mV — red LED + charge prompt", (long)battery_mv);
  say_low_battery();
}

static void exit_low_battery(int32_t battery_mv) {
  s_low_alert = false;
  nino_audio_refresh_mute();
  if (nino_audio_is_muted()) {
    (void)nino_rgb_led_show(NINO_RGB_SHOW_MUTE);
  } else {
    (void)nino_rgb_led_show(NINO_RGB_SHOW_IDLE);
  }
  ESP_LOGI(TAG, "battery recovered %ld mV", (long)battery_mv);
}

static void low_battery_task(void *arg) {
  (void)arg;
  /* ADC inits before the speaker queue; wait so the first prompt can play. */
  vTaskDelay(pdMS_TO_TICKS(8000));
  uint32_t since_say_ms = LOW_SAY_MS;
  while (true) {
    nino_battery_sample_t sample = {};
    const bool ok = (nino_battery_adc_read(&sample) == ESP_OK);
    const bool pack_present =
        ok && sample.battery_mv >= LOW_MIN_VALID_MV;
    const bool is_low = pack_present && sample.battery_mv <= LOW_ENTER_MV;
    const bool recovered = pack_present && sample.battery_mv >= LOW_CLEAR_MV;

    if (!s_low_alert && is_low) {
      enter_low_battery(sample.battery_mv);
      since_say_ms = 0;
    } else if (s_low_alert && (recovered || !pack_present)) {
      exit_low_battery(ok ? sample.battery_mv : 0);
      since_say_ms = LOW_SAY_MS;
    } else if (s_low_alert) {
      if (nino_rgb_led_current_show() != NINO_RGB_SHOW_BATTERY) {
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
  esp_err_t err = read_raw_avg(&raw);
  lock_give();
  if (err != ESP_OK) {
    return err;
  }
  fill_sample(out, raw);
  return ESP_OK;
}

static void log_sample(const nino_battery_sample_t *s) {
  ESP_LOGI(TAG, "GPIO%d raw=%d pin=%ld mV vin=%ld mV %u%% %s", BATT_ADC_GPIO,
           (int)s->raw, (long)s->adc_mv, (long)s->battery_mv,
           (unsigned)s->percent, pack_name(s->battery_mv));
}

static void print_sample(const nino_battery_sample_t *s) {
  printf("GPIO%d (22k / 2x3.3k) raw=%d  pin=", BATT_ADC_GPIO, (int)s->raw);
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
  if (s_low_alert) {
    printf("LOW BATTERY alert active (red blink + charge prompt). Clears above 10.4 V.\n");
  } else if (sample.battery_mv <= LOW_ENTER_MV &&
             sample.battery_mv >= LOW_MIN_VALID_MV) {
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
```

### `main/low_battery.wav`

Binary PCM WAV, **109876 bytes**. Embedded by CMake as:

```c
extern const uint8_t low_battery_wav_start[] asm("_binary_low_battery_wav_start");
extern const uint8_t low_battery_wav_end[] asm("_binary_low_battery_wav_end");
```

## 13.6 Mute button, RGB LED, speaker mute

### `main/push_buttons.h` — complete file (31 lines)

Copied from the tree as of this document. Do not edit here; edit the source file.

```c
#pragma once

#include <stdbool.h>

#include "esp_err.h"

/**
 * GPIO48 push button (active-low, internal pull-up, press to GND):
 *  - single press: start/stop hardware test (motors + camera + RGB + TFT)
 *  - double press: play DEMO_main.wav
 *  - triple press: erase Wi-Fi credentials, enable BLE provisioning,
 *    play NiNO-Home_Wifi.wav
 *
 * GPIO47 mute button (same wiring): single press toggles speaker mute.
 * While muted the RGB LED is solid red (not the low-battery blink).
 *
 * Do not wire buttons to GPIO7 (I2C SDA) or GPIO53 (speaker PA).
 * Call once from app_main after audio queue start.
 */
esp_err_t nino_push_buttons_start(void);

/**
 * Queue the embedded DEMO_main.wav clip, exactly as a double button press does.
 * Lets the phone app trigger the on-device demo via POST /demo {"play":true}.
 * Returns ESP_ERR_INVALID_STATE if the button subsystem has not started yet.
 * Ignored (still ESP_OK) if the demo is already playing.
 */
esp_err_t nino_push_buttons_trigger_demo(void);

/** Mute/unmute the speaker and set the solid-red mute LED. */
void nino_mute_set(bool muted);
```

### `main/push_buttons.c` — complete file (331 lines)

Copied from the tree as of this document. Do not edit here; edit the source file.

```c
#include "push_buttons.h"

#include <stdbool.h>
#include <stdint.h>

#include "audio_queue.h"
#include "audio_playback.h"
#include "battery_adc.h"
#include "battery_endurance.h"
#include "driver/gpio.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/task.h"
#include "rgb_led.h"
#include "wifi_config.h"

static const char *TAG = "push_btn";

/*
 * Wiring (ESP32-P4-Function-EV-Board J1, active-low to GND):
 *  - GPIO48 (J1 pin 33): single press = hardware test on/off;
 *    double press = DEMO_main.wav; triple press = erase Wi-Fi + BLE setup
 *  - GPIO47 (J1 pin 37): single press = speaker mute on/off (solid red LED)
 *
 * Do NOT use:
 *  - GPIO7 / GPIO8  — BSP I2C SDA/SCL (ES8311). Pressing GPIO7
 *    shorts SDA and kills the speaker codec (I2C_If Fail to … dev 30).
 *  - GPIO53         — BSP_POWER_AMP_IO (speaker amp enable).
 *  - GPIO9–13       — I2S to the codec.
 */
#define BTN_DEMO_GPIO GPIO_NUM_48
#define BTN_MUTE_GPIO GPIO_NUM_47
#define BTN_ACTIVE_LEVEL 0

#define BTN_POLL_MS 20
#define BTN_DEBOUNCE_MS 40
/* Max quiet time after the last release before a click sequence is evaluated. */
#define BTN_MULTI_GAP_MS 350
#define BTN_CLICKS_SOAK 1
#define BTN_CLICKS_DEMO 2
#define BTN_CLICKS_SETUP 3
#define BTN_TASK_STACK 3072
#define BTN_WORKER_STACK 6144
#define BTN_TASK_PRIO 5

typedef enum {
  BTN_EVT_DEMO = 1,
  BTN_EVT_SETUP = 2,
  BTN_EVT_SOAK = 3,
  BTN_EVT_MUTE = 4,
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

static void apply_user_mute(bool muted) {
  (void)nino_audio_set_muted(muted);
  if (muted) {
    nino_audio_queue_preempt_for_wake();
    if (!nino_battery_low_alert_active()) {
      (void)nino_rgb_led_show(NINO_RGB_SHOW_MUTE);
    }
  } else if (!nino_battery_low_alert_active()) {
    (void)nino_rgb_led_show(NINO_RGB_SHOW_IDLE);
  }
}

void nino_mute_set(bool muted) { apply_user_mute(muted); }

static void toggle_user_mute(void) {
  const bool next = !nino_audio_is_muted();
  apply_user_mute(next);
  ESP_LOGI(TAG, "Mute button → speaker %s", next ? "MUTED (solid red)" : "unmuted");
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
      if (nino_battery_endurance_is_active()) {
        nino_battery_endurance_stop();
      }
      esp_err_t err = wifi_config_enter_setup_mode();
      if (err != ESP_OK) {
        ESP_LOGW(TAG, "Enter setup mode failed: %s", esp_err_to_name(err));
      }
      (void)play_setup_clip();
    } else if (evt == BTN_EVT_SOAK) {
      ESP_LOGI(TAG, "Action: toggle hardware test");
      nino_battery_endurance_toggle();
    } else if (evt == BTN_EVT_MUTE) {
      toggle_user_mute();
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
        } else if (btn->gpio == BTN_MUTE_GPIO) {
          post_evt(BTN_EVT_MUTE);
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
      /* While the test is running, any click on this button is STOP — so a
       * hurried second press cannot fire demo or wipe Wi-Fi. */
      if (nino_battery_endurance_is_active()) {
        ESP_LOGI(TAG, "GPIO%d (%s) %u press during test → STOP",
                 (int)btn->gpio, btn->name, (unsigned)clicks);
        post_evt(BTN_EVT_SOAK);
      } else if (clicks >= BTN_CLICKS_SETUP) {
        ESP_LOGI(TAG, "GPIO%d (%s) triple press → setup + BLE", (int)btn->gpio,
                 btn->name);
        post_evt(BTN_EVT_SETUP);
      } else if (clicks == BTN_CLICKS_DEMO) {
        ESP_LOGI(TAG, "GPIO%d (%s) double press → demo audio", (int)btn->gpio,
                 btn->name);
        post_evt(BTN_EVT_DEMO);
      } else if (clicks == BTN_CLICKS_SOAK) {
        ESP_LOGI(TAG, "GPIO%d (%s) single press → hardware test START",
                 (int)btn->gpio, btn->name);
        post_evt(BTN_EVT_SOAK);
      } else {
        ESP_LOGI(TAG, "GPIO%d (%s) %u press ignored (need 1=hwtest, 2=demo, 3=setup)",
                 (int)btn->gpio, btn->name, (unsigned)clicks);
      }
    }
  }
}

static void push_buttons_task(void *arg) {
  (void)arg;

  btn_state_t demo = {
      .gpio = BTN_DEMO_GPIO,
      .name = "hwtest/demo/setup",
      .stable_pressed = false,
      .armed = true,
      .debounce_ms = 0,
      .click_count = 0,
      .gap_ms = 0,
  };
  btn_state_t mute = {
      .gpio = BTN_MUTE_GPIO,
      .name = "mute",
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
  if (gpio_get_level(BTN_MUTE_GPIO) == BTN_ACTIVE_LEVEL) {
    mute.stable_pressed = true;
    mute.armed = false;
  }

  ESP_LOGI(TAG,
           "Buttons ready: GPIO%d single=hwtest double=Demo triple=Wi-Fi setup; "
           "GPIO%d mute (level48=%d level47=%d, 0=pressed)",
           (int)BTN_DEMO_GPIO, (int)BTN_MUTE_GPIO, gpio_get_level(BTN_DEMO_GPIO),
           gpio_get_level(BTN_MUTE_GPIO));

  while (true) {
    btn_update(&demo);
    btn_update(&mute);
    vTaskDelay(pdMS_TO_TICKS(BTN_POLL_MS));
  }
}

esp_err_t nino_push_buttons_start(void) {
  const gpio_config_t io = {
      .pin_bit_mask = (1ULL << BTN_DEMO_GPIO) | (1ULL << BTN_MUTE_GPIO),
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
```

### `main/rgb_led.h` — complete file (53 lines)

Copied from the tree as of this document. Do not edit here; edit the source file.

```c
#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "esp_err.h"

#define RGB_LED_LEVEL_MAX 255

/** Runtime LED scenes: wake/listen, TTS done, Wi-Fi, battery, error. */
typedef enum {
  NINO_RGB_SHOW_IDLE = 0,   /* no light */
  NINO_RGB_SHOW_LISTEN,     /* solid green — user may speak */
  NINO_RGB_SHOW_DONE,       /* blink green a few times, then off */
  NINO_RGB_SHOW_BATTERY,    /* continuous red blink — pack at or below 10 V */
  NINO_RGB_SHOW_MUTE,       /* solid red — speaker muted */
  NINO_RGB_SHOW_OTA,        /* solid purple — firmware update */
  NINO_RGB_SHOW_ERROR,      /* fast red blink — capture/WS/error */
  NINO_RGB_SHOW_WIFI_WAIT,  /* white blink — connecting */
  NINO_RGB_SHOW_WIFI_OK,    /* solid cyan — Wi-Fi up (CLI preview) */
  NINO_RGB_SHOW_WIFI_FAIL,  /* solid orange — not connected */
  NINO_RGB_SHOW_SERVER_WAIT, /* pale green blink — linking to voice PC */
  NINO_RGB_SHOW_SERVER_OK,  /* cyan blink once — voice server reached */
} nino_rgb_show_t;

/** Common-anode RGB on GPIO 2 (red), 3 (green), 4 (blue). Black -> 3.3 V. */
esp_err_t nino_rgb_led_init(void);

/** Start a named scene (stops any previous blink). Safe from console or tasks. */
esp_err_t nino_rgb_led_show(nino_rgb_show_t show);

const char *nino_rgb_led_show_name(nino_rgb_show_t show);

/** Scene last started by nino_rgb_led_show (IDLE after a finite blink ends). */
nino_rgb_show_t nino_rgb_led_current_show(void);

/** Set one primary channel 0-255. Other channels unchanged. */
esp_err_t nino_rgb_led_set_channel_level(const char *color, uint8_t level);

/** Set red/green/blue mix, each 0-255. */
esp_err_t nino_rgb_led_set_rgb(uint8_t red, uint8_t green, uint8_t blue);

/** Global brightness scale 0-255 applied to all channels. */
esp_err_t nino_rgb_led_set_brightness(uint8_t level);

uint8_t nino_rgb_led_get_brightness(void);

/** Apply a named color at optional intensity 0-255 (default 255). */
esp_err_t nino_rgb_led_set_named(const char *name, uint8_t intensity);

void nino_rgb_led_all_off(void);

void nino_rgb_led_cli_register(void);
```

### `main/rgb_led.c` — complete file (714 lines)

Copied from the tree as of this document. Do not edit here; edit the source file.

```c
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
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"

#include "audio_playback.h"
#include "battery_adc.h"
#include "battery_endurance.h"

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
    {"mint", 40, 170, 0},
};

static uint8_t s_global_brightness = RGB_LED_LEVEL_MAX;
static nino_rgb_show_t s_show = NINO_RGB_SHOW_IDLE;
static SemaphoreHandle_t s_led_lock;
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
  case NINO_RGB_SHOW_DONE:
    return "done";
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
  case NINO_RGB_SHOW_WIFI_OK:
    return "wifi-ok";
  case NINO_RGB_SHOW_WIFI_FAIL:
    return "wifi-fail";
  case NINO_RGB_SHOW_SERVER_WAIT:
    return "server-wait";
  case NINO_RGB_SHOW_SERVER_OK:
    return "server-ok";
  default:
    return "unknown";
  }
}

nino_rgb_show_t nino_rgb_led_current_show(void)
{
  return s_show;
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

static void led_lock(void)
{
  if (s_led_lock != NULL) {
    (void)xSemaphoreTakeRecursive(s_led_lock, portMAX_DELAY);
  }
}

static void led_unlock(void)
{
  if (s_led_lock != NULL) {
    (void)xSemaphoreGiveRecursive(s_led_lock);
  }
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
  led_lock();
  s_blink_on = !s_blink_on;
  if (s_blink_on) {
    (void)nino_rgb_led_set_rgb(s_blink_r, s_blink_g, s_blink_b);
    led_unlock();
    return;
  }
  (void)nino_rgb_led_set_rgb(0, 0, 0);
  if (s_blink_remaining < 0) {
    led_unlock();
    return;
  }
  s_blink_remaining--;
  if (s_blink_remaining <= 0) {
    blink_timer_stop();
    s_show = NINO_RGB_SHOW_IDLE;
  }
  led_unlock();
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

esp_err_t nino_rgb_led_show(nino_rgb_show_t show)
{
  if (nino_battery_endurance_owns_actuators() && !nino_battery_endurance_is_self()) {
    return ESP_OK;
  }
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
  led_lock();
  blink_timer_stop();
  /* Drop every channel before the next scene so white/cyan/green cannot mix. */
  (void)nino_rgb_led_set_rgb(0, 0, 0);
  s_show = show;

  esp_err_t err = ESP_OK;
  switch (show) {
  case NINO_RGB_SHOW_IDLE:
    nino_rgb_led_all_off();
    break;
  case NINO_RGB_SHOW_LISTEN:
    (void)nino_rgb_led_set_named("green", RGB_LED_LEVEL_MAX);
    break;
  case NINO_RGB_SHOW_DONE:
    s_blink_r = 0;
    s_blink_g = 255;
    s_blink_b = 0;
    err = blink_timer_start(350 * 1000, 3);
    break;
  case NINO_RGB_SHOW_BATTERY:
    /* Continuous red while pack is at or below 10 V. */
    s_blink_r = 255;
    s_blink_g = 0;
    s_blink_b = 0;
    err = blink_timer_start(400 * 1000, -1);
    break;
  case NINO_RGB_SHOW_MUTE:
    /* Solid red while the speaker is muted — not the battery blink. */
    (void)nino_rgb_led_set_named("red", RGB_LED_LEVEL_MAX);
    break;
  case NINO_RGB_SHOW_OTA:
    (void)nino_rgb_led_set_named("purple", RGB_LED_LEVEL_MAX);
    break;
  case NINO_RGB_SHOW_ERROR:
    s_blink_r = 255;
    s_blink_g = 0;
    s_blink_b = 0;
    err = blink_timer_start(200 * 1000, -1);
    break;
  case NINO_RGB_SHOW_WIFI_WAIT:
    s_blink_r = 255;
    s_blink_g = 255;
    s_blink_b = 255;
    err = blink_timer_start(400 * 1000, -1);
    break;
  case NINO_RGB_SHOW_WIFI_OK:
    (void)nino_rgb_led_set_named("cyan", RGB_LED_LEVEL_MAX);
    break;
  case NINO_RGB_SHOW_WIFI_FAIL:
    (void)nino_rgb_led_set_named("orange", RGB_LED_LEVEL_MAX);
    break;
  case NINO_RGB_SHOW_SERVER_WAIT:
    /* Dim green only — no blue, so it cannot look like cyan. */
    s_blink_r = 0;
    s_blink_g = 140;
    s_blink_b = 0;
    err = blink_timer_start(400 * 1000, -1);
    break;
  case NINO_RGB_SHOW_SERVER_OK:
    s_blink_r = 0;
    s_blink_g = 255;
    s_blink_b = 255;
    err = blink_timer_start(350 * 1000, 1);
    break;
  default:
    err = ESP_ERR_INVALID_ARG;
    break;
  }

  if (err == ESP_OK) {
    ESP_LOGI(TAG, "show -> %s", nino_rgb_led_show_name(show));
  }
  led_unlock();
  return err;
}

esp_err_t nino_rgb_led_init(void) {
  if (s_led_lock == NULL) {
    s_led_lock = xSemaphoreCreateRecursiveMutex();
    if (s_led_lock == NULL) {
      return ESP_ERR_NO_MEM;
    }
  }

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
  led_lock();
  s_channels[0].level = red;
  s_channels[1].level = green;
  s_channels[2].level = blue;
  esp_err_t err = apply_all();
  led_unlock();
  return err;
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
  printf("      listen     solid green     (Ok Nino — user can speak)\n");
  printf("      done       blink green x3  (answer finished, then off)\n");
  printf("      idle       off\n");
  printf("      battery    blink red continuously (pack <= 10 V)\n");
  printf("      mute       solid red         (speaker muted)\n");
  printf("      ota        solid purple      (firmware update)\n");
  printf("      error      fast blink red    (capture/WS/error)\n");
  printf("      wifi-wait    blink white      (Wi-Fi connecting)\n");
  printf("      wifi-ok      solid cyan       (Wi-Fi connected, preview)\n");
  printf("      wifi-fail    solid orange     (Wi-Fi not connected)\n");
  printf("      server-wait  blink dim green  (linking to voice server)\n");
  printf("      server-ok    blink cyan once  (voice server reached)\n");
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
    } else if (strcmp(argv[2], "done") == 0) {
      show = NINO_RGB_SHOW_DONE;
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
    } else if (strcmp(argv[2], "wifi-ok") == 0) {
      show = NINO_RGB_SHOW_WIFI_OK;
    } else if (strcmp(argv[2], "wifi-fail") == 0) {
      show = NINO_RGB_SHOW_WIFI_FAIL;
    } else if (strcmp(argv[2], "server-wait") == 0) {
      show = NINO_RGB_SHOW_SERVER_WAIT;
    } else if (strcmp(argv[2], "server-ok") == 0) {
      show = NINO_RGB_SHOW_SERVER_OK;
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
          "rgb show listen|done|idle|battery|ota|error|wifi-wait|wifi-ok|wifi-fail|server-wait|server-ok | "
          "rgb off | rgb status",
      .hint = NULL,
      .func = &cmd_rgb,
      .argtable = NULL,
  };
  ESP_ERROR_CHECK(esp_console_cmd_register(&cmd));
}
```

### `main/audio_playback.h` — complete file (83 lines)

Copied from the tree as of this document. Do not edit here; edit the source file.

```c
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include "esp_err.h"

esp_err_t nino_audio_init(void);

esp_err_t nino_audio_play_wav(const uint8_t *wav_bytes, size_t wav_len);

/** Decoded 16-bit mono PCM ready for speaker output. */
typedef struct {
  const int16_t *samples;
  size_t num_bytes;
  uint32_t sample_rate_hz;
  int16_t *mono_heap;
} nino_decoded_wav_t;

/** Parse WAV into mono PCM. Caller frees with nino_decoded_wav_free(). */
esp_err_t nino_audio_decode_wav(const uint8_t *wav_bytes, size_t wav_len,
                                nino_decoded_wav_t *out);

/** True when @p wav_bytes is a complete PCM WAV the ESP speaker path can play. */
bool nino_audio_wav_bytes_valid(const uint8_t *wav_bytes, size_t wav_len);

void nino_decoded_wav_free(nino_decoded_wav_t *decoded);

/**
 * Play decoded PCM from @p pcm_byte_offset. Updates @p pcm_byte_offset on exit.
 * @p completed is set true when the entire clip finishes; false if @p stop_requested
 * interrupted playback mid-clip.
 */
esp_err_t nino_audio_play_decoded(const nino_decoded_wav_t *decoded, size_t *pcm_byte_offset,
                                  volatile bool *stop_requested, bool *completed);

/** Play 16-bit mono PCM; waits for the DAC pipeline to finish before closing the codec. */
esp_err_t nino_audio_play_pcm16_mono(const int16_t *samples, size_t sample_count,
                                     uint32_t sample_rate_hz);

/** Fast wake/done chime: reuse open 16 kHz codec when possible; leaves path warm. */
esp_err_t nino_audio_play_chime_pcm16_mono(const int16_t *samples, size_t sample_count,
                                           uint32_t sample_rate_hz);

/** Open speaker at 16 kHz once at boot so the first wake beep has no codec setup delay. */
esp_err_t nino_audio_warm_chime_path(uint32_t sample_rate_hz);

/**
 * Open the speaker (sample_count == 0) or write 16-bit mono PCM without closing
 * the codec. Caller must hold nino_audio_bus_lock().
 */
esp_err_t nino_audio_write_pcm16_mono_locked(const int16_t *samples, size_t sample_count,
                                             uint32_t sample_rate_hz);

/** Serialize access to the ES8311 speaker I2S path (playback). */
void nino_audio_bus_lock(void);
void nino_audio_bus_unlock(void);

/**
 * Close the speaker I2S stream. Caller must hold nino_audio_bus_lock().
 * Aux-in and the DAC share one ES8311; the ADC path must steal the bus before
 * opening, and the next speaker play must reopen so the PA/DAC come back.
 */
void nino_audio_drop_speaker_stream_locked(void);

/** Set speaker output volume percent (0-100). Persisted to NVS. */
esp_err_t nino_audio_set_volume_percent(int volume_percent);

/** Current speaker output volume percent (0-100). */
int nino_audio_get_volume_percent(void);

/** User mute (GPIO47 / `speaker mute`). Low-battery prompts still play. */
esp_err_t nino_audio_set_muted(bool muted);
bool nino_audio_is_muted(void);
/** Re-apply codec mute after battery-alert enter/exit. */
void nino_audio_refresh_mute(void);

/**
 * Load the speaker volume saved in NVS (set by the app/console) and apply it.
 * Falls back to the 80% default if nothing has been saved yet. Call once at
 * boot after nino_audio_init().
 */
esp_err_t nino_audio_load_saved_volume(void);
```

### `main/audio_playback.c` — complete file (673 lines)

Copied from the tree as of this document. Do not edit here; edit the source file.

```c
#include "audio_playback.h"

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>

#include "mic_input.h"
#include "battery_adc.h"

#include "bsp/esp32_p4_function_ev_board.h"
#include "esp_codec_dev.h"
#include "esp_codec_dev_types.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "nvs.h"

static const char *TAG = "nino_audio";

#define NINO_AUDIO_NVS_NS "nino_audio"
#define NINO_AUDIO_NVS_KEY_VOL "vol"
#define NINO_AUDIO_DEFAULT_VOLUME 80

static SemaphoreHandle_t s_mutex;
static esp_codec_dev_handle_t s_spk;
static bool s_ready;
static int s_volume_percent = NINO_AUDIO_DEFAULT_VOLUME;
static volatile bool s_user_muted;
static bool s_spk_stream_open;
static uint32_t s_spk_stream_rate_hz;
/* Set when Aux-in opens/closes the same ES8311. Next speaker open must reopen
 * the DAC even if the firmware still thinks the stream is live. */
static bool s_spk_force_reopen;

static void audio_persist_volume(int volume_percent) {
  nvs_handle_t h;
  if (nvs_open(NINO_AUDIO_NVS_NS, NVS_READWRITE, &h) != ESP_OK) {
    return;
  }
  if (nvs_set_i32(h, NINO_AUDIO_NVS_KEY_VOL, (int32_t)volume_percent) == ESP_OK) {
    nvs_commit(h);
  }
  nvs_close(h);
}

/** Let I2S/codec finish samples already queued (avoids truncated two-tone beeps). */
static void wait_pcm_pipeline_done(uint32_t sample_rate_hz, size_t pcm_bytes,
                                   TickType_t write_started) {
  if (sample_rate_hz == 0 || pcm_bytes == 0) {
    return;
  }
  const uint32_t audio_ms =
      (uint32_t)((pcm_bytes * 1000ULL) / (sample_rate_hz * 2ULL));
  const TickType_t now = xTaskGetTickCount();
  const uint32_t elapsed_ms =
      (uint32_t)((now - write_started) * portTICK_PERIOD_MS);
  const uint32_t pipeline_margin_ms = 100;
  uint32_t wait_ms = pipeline_margin_ms;
  if (elapsed_ms < audio_ms) {
    wait_ms = (audio_ms - elapsed_ms) + pipeline_margin_ms;
  }
  vTaskDelay(pdMS_TO_TICKS(wait_ms));
}

static void spk_stream_close_locked(void) {
  if (s_spk != NULL && s_spk_stream_open) {
    (void)esp_codec_dev_close(s_spk);
  }
  s_spk_stream_open = false;
  s_spk_stream_rate_hz = 0;
}

static esp_err_t spk_stream_open_locked(uint32_t sample_rate_hz, bool leave_open) {
  if (s_spk == NULL) {
    return ESP_FAIL;
  }
  if (s_spk_stream_open && s_spk_stream_rate_hz == sample_rate_hz &&
      !s_spk_force_reopen) {
    return ESP_OK;
  }
  /* ES8311 AUX ADC + speaker share one duplex I2S. Drop the ADC before any
   * rate change / reopen so capture does not keep a stale mic handle. */
  nino_mic_drop_es8311_locked();
  spk_stream_close_locked();

  esp_codec_dev_sample_info_t fs = {
      .bits_per_sample = 16,
      .channel = 1,
      .channel_mask = 0,
      .sample_rate = sample_rate_hz,
      .mclk_multiple = 0,
  };
  const int cr = esp_codec_dev_open(s_spk, &fs);
  if (cr != ESP_CODEC_DEV_OK) {
    ESP_LOGE(TAG, "esp_codec_dev_open failed: %d", cr);
    s_spk_force_reopen = true;
    return ESP_FAIL;
  }
  s_spk_stream_open = true;
  s_spk_stream_rate_hz = sample_rate_hz;
  s_spk_force_reopen = false;
  (void)esp_codec_dev_set_out_vol(s_spk, s_volume_percent);
  (void)esp_codec_dev_set_out_mute(s_spk, s_user_muted &&
                                             !nino_battery_low_alert_active());
  ESP_LOGI(TAG, "Speaker opened @ %u Hz vol=%d%% mute=%d",
           (unsigned)sample_rate_hz, s_volume_percent, (int)s_user_muted);
  (void)leave_open;
  return ESP_OK;
}

static uint32_t read_le32(const uint8_t *p) {
  return (uint32_t)p[0] | ((uint32_t)p[1] << 8) | ((uint32_t)p[2] << 16) |
         ((uint32_t)p[3] << 24);
}

static uint16_t read_le16(const uint8_t *p) {
  return (uint16_t)(p[0] | (p[1] << 8));
}

typedef struct {
  const uint8_t *pcm;
  size_t pcm_len;
  uint32_t sample_rate;
  uint16_t channels;
  uint16_t bits_per_sample;
} wav_pcm_t;

static bool parse_wav_pcm(const uint8_t *buf, size_t len, wav_pcm_t *out) {
  memset(out, 0, sizeof(*out));
  if (len < 44) {
    return false;
  }
  if (memcmp(buf, "RIFF", 4) != 0 || memcmp(buf + 8, "WAVE", 4) != 0) {
    return false;
  }

  bool have_fmt = false;
  bool have_data = false;
  uint16_t audio_format = 0;
  size_t off = 12;

  while (off + 8 <= len) {
    const uint8_t *hdr = buf + off;
    uint32_t chunk_size = read_le32(hdr + 4);
    off += 8;
    if (off + chunk_size > len) {
      return false;
    }
    const uint8_t *chunk_data = buf + off;

    if (memcmp(hdr, "fmt ", 4) == 0) {
      if (chunk_size < 16) {
        return false;
      }
      audio_format = read_le16(chunk_data);
      out->channels = read_le16(chunk_data + 2);
      out->sample_rate = read_le32(chunk_data + 4);
      out->bits_per_sample = read_le16(chunk_data + 14);
      have_fmt = true;
    } else if (memcmp(hdr, "data", 4) == 0) {
      out->pcm = chunk_data;
      out->pcm_len = chunk_size;
      have_data = true;
    }

    off += chunk_size;
    if ((chunk_size & 1U) != 0U) {
      off += 1;
    }
  }

  if (!have_fmt || !have_data) {
    return false;
  }
  /* 1 = PCM; 0xFFFE = WAVE_FORMAT_EXTENSIBLE (often still 16-bit PCM from Windows SAPI). */
  if (audio_format != 1 && audio_format != (uint16_t)0xFFFE) {
    return false;
  }
  if (out->bits_per_sample != 16) {
    return false;
  }
  if (out->channels < 1 || out->channels > 2) {
    return false;
  }
  if (out->sample_rate < 8000 || out->sample_rate > 48000) {
    return false;
  }
  return true;
}

bool nino_audio_wav_bytes_valid(const uint8_t *wav_bytes, size_t wav_len) {
  wav_pcm_t wav;
  return parse_wav_pcm(wav_bytes, wav_len, &wav);
}

esp_err_t nino_audio_init(void) {
  if (s_mutex == NULL) {
    s_mutex = xSemaphoreCreateMutex();
    if (s_mutex == NULL) {
      return ESP_ERR_NO_MEM;
    }
  }

  xSemaphoreTake(s_mutex, portMAX_DELAY);
  esp_err_t err = ESP_OK;
  if (!s_ready) {
    err = bsp_i2c_init();
    if (err != ESP_OK && err != ESP_ERR_INVALID_STATE) {
      ESP_LOGE(TAG, "bsp_i2c_init failed: %s", esp_err_to_name(err));
      xSemaphoreGive(s_mutex);
      return err;
    }

    s_spk = bsp_audio_codec_speaker_init();
    if (s_spk == NULL) {
      ESP_LOGE(TAG, "bsp_audio_codec_speaker_init failed");
      xSemaphoreGive(s_mutex);
      return ESP_FAIL;
    }

    esp_codec_dev_set_out_vol(s_spk, s_volume_percent);
    s_ready = true;
    ESP_LOGI(TAG, "Speaker ready (ES8311), volume=%d%%", s_volume_percent);
  }
  xSemaphoreGive(s_mutex);
  return ESP_OK;
}

esp_err_t nino_audio_set_volume_percent(int volume_percent) {
  if (volume_percent < 0 || volume_percent > 100) {
    return ESP_ERR_INVALID_ARG;
  }

  if (s_mutex == NULL) {
    esp_err_t err = nino_audio_init();
    if (err != ESP_OK) {
      return err;
    }
  }

  xSemaphoreTake(s_mutex, portMAX_DELAY);
  s_volume_percent = volume_percent;
  if (s_spk != NULL) {
    (void)esp_codec_dev_set_out_vol(s_spk, s_volume_percent);
  }
  xSemaphoreGive(s_mutex);

  audio_persist_volume(volume_percent);
  ESP_LOGI(TAG, "Speaker volume set to %d%%", s_volume_percent);
  return ESP_OK;
}

int nino_audio_get_volume_percent(void) { return s_volume_percent; }

static void apply_codec_mute_locked(void) {
  if (s_spk == NULL) {
    return;
  }
  const bool mute = s_user_muted && !nino_battery_low_alert_active();
  (void)esp_codec_dev_set_out_mute(s_spk, mute);
}

bool nino_audio_is_muted(void) { return s_user_muted; }

void nino_audio_refresh_mute(void) {
  if (s_mutex == NULL) {
    return;
  }
  xSemaphoreTake(s_mutex, portMAX_DELAY);
  apply_codec_mute_locked();
  xSemaphoreGive(s_mutex);
}

esp_err_t nino_audio_set_muted(bool muted) {
  if (s_mutex == NULL) {
    esp_err_t err = nino_audio_init();
    if (err != ESP_OK) {
      s_user_muted = muted;
      return err;
    }
  }

  xSemaphoreTake(s_mutex, portMAX_DELAY);
  s_user_muted = muted;
  apply_codec_mute_locked();
  xSemaphoreGive(s_mutex);
  ESP_LOGI(TAG, "Speaker %s", muted ? "MUTED" : "unmuted");
  return ESP_OK;
}

esp_err_t nino_audio_load_saved_volume(void) {
  int volume_percent = NINO_AUDIO_DEFAULT_VOLUME;
  nvs_handle_t h;
  if (nvs_open(NINO_AUDIO_NVS_NS, NVS_READONLY, &h) == ESP_OK) {
    int32_t stored = NINO_AUDIO_DEFAULT_VOLUME;
    if (nvs_get_i32(h, NINO_AUDIO_NVS_KEY_VOL, &stored) == ESP_OK &&
        stored >= 0 && stored <= 100) {
      volume_percent = (int)stored;
      ESP_LOGI(TAG, "Loaded saved speaker volume from NVS: %d%%", volume_percent);
    } else {
      ESP_LOGI(TAG, "No saved volume in NVS, using default %d%%", volume_percent);
    }
    nvs_close(h);
  }
  return nino_audio_set_volume_percent(volume_percent);
}

esp_err_t nino_audio_warm_chime_path(uint32_t sample_rate_hz) {
  if (sample_rate_hz < 8000 || sample_rate_hz > 48000) {
    return ESP_ERR_INVALID_ARG;
  }
  if (!s_ready) {
    esp_err_t e = nino_audio_init();
    if (e != ESP_OK) {
      return e;
    }
  }
  xSemaphoreTake(s_mutex, portMAX_DELAY);
  esp_err_t e = spk_stream_open_locked(sample_rate_hz, true);
  if (e == ESP_OK) {
    /* Prime I2S DMA so the first wake beep does not pay open latency on cold start. */
    int16_t silence[320] = {0};
    (void)esp_codec_dev_write(s_spk, silence, sizeof(silence));
    vTaskDelay(pdMS_TO_TICKS(25));
    ESP_LOGI(TAG, "Chime path warm @ %u Hz", (unsigned)sample_rate_hz);
  }
  xSemaphoreGive(s_mutex);
  return e;
}

esp_err_t nino_audio_play_chime_pcm16_mono(const int16_t *samples, size_t sample_count,
                                           uint32_t sample_rate_hz) {
  if (samples == NULL || sample_count == 0 || sample_rate_hz < 8000 ||
      sample_rate_hz > 48000) {
    return ESP_ERR_INVALID_ARG;
  }

  if (!s_ready) {
    esp_err_t e = nino_audio_init();
    if (e != ESP_OK) {
      return e;
    }
  }

  const size_t play_len = sample_count * sizeof(int16_t);
  xSemaphoreTake(s_mutex, portMAX_DELAY);

  esp_err_t err = spk_stream_open_locked(sample_rate_hz, true);
  if (err != ESP_OK) {
    xSemaphoreGive(s_mutex);
    return err;
  }

  const TickType_t write_started = xTaskGetTickCount();
  const uint8_t *play_ptr = (const uint8_t *)samples;
  size_t offset = 0;
  int cr = ESP_CODEC_DEV_OK;
  while (offset < play_len) {
    int block = (int)(play_len - offset);
    if (block > 8192) {
      block = 8192;
    }
    cr = esp_codec_dev_write(s_spk, (void *)(play_ptr + offset), block);
    if (cr != ESP_CODEC_DEV_OK) {
      ESP_LOGE(TAG, "esp_codec_dev_write failed: %d", cr);
      break;
    }
    offset += (size_t)block;
  }

  if (cr == ESP_CODEC_DEV_OK) {
    if (sample_rate_hz == 0 || play_len == 0) {
      /* skip */
    } else {
      const uint32_t audio_ms =
          (uint32_t)((play_len * 1000ULL) / (sample_rate_hz * 2ULL));
      const TickType_t now = xTaskGetTickCount();
      const uint32_t elapsed_ms =
          (uint32_t)((now - write_started) * portTICK_PERIOD_MS);
      const uint32_t pipeline_margin_ms = 40;
      uint32_t wait_ms = pipeline_margin_ms;
      if (elapsed_ms < audio_ms) {
        wait_ms = (audio_ms - elapsed_ms) + pipeline_margin_ms;
      }
      vTaskDelay(pdMS_TO_TICKS(wait_ms));
    }
  }

  nino_mic_drop_es8311_locked();
  xSemaphoreGive(s_mutex);
  return (cr == ESP_CODEC_DEV_OK) ? ESP_OK : ESP_FAIL;
}

esp_err_t nino_audio_play_pcm16_mono(const int16_t *samples, size_t sample_count,
                                     uint32_t sample_rate_hz) {
  if (samples == NULL || sample_count == 0 || sample_rate_hz < 8000 ||
      sample_rate_hz > 48000) {
    return ESP_ERR_INVALID_ARG;
  }

  if (!s_ready) {
    esp_err_t e = nino_audio_init();
    if (e != ESP_OK) {
      return e;
    }
  }

  const size_t play_len = sample_count * sizeof(int16_t);
  xSemaphoreTake(s_mutex, portMAX_DELAY);

  esp_err_t err = spk_stream_open_locked(sample_rate_hz, false);
  if (err != ESP_OK) {
    xSemaphoreGive(s_mutex);
    return err;
  }

  const TickType_t write_started = xTaskGetTickCount();
  const uint8_t *play_ptr = (const uint8_t *)samples;
  size_t offset = 0;
  int cr = ESP_CODEC_DEV_OK;
  while (offset < play_len) {
    int block = (int)(play_len - offset);
    if (block > 8192) {
      block = 8192;
    }
    cr = esp_codec_dev_write(s_spk, (void *)(play_ptr + offset), block);
    if (cr != ESP_CODEC_DEV_OK) {
      ESP_LOGE(TAG, "esp_codec_dev_write failed: %d", cr);
      break;
    }
    offset += (size_t)block;
  }

  if (cr == ESP_CODEC_DEV_OK) {
    wait_pcm_pipeline_done(sample_rate_hz, play_len, write_started);
  }

  spk_stream_close_locked();
  nino_mic_drop_es8311_locked();
  xSemaphoreGive(s_mutex);
  return (cr == ESP_CODEC_DEV_OK) ? ESP_OK : ESP_FAIL;
}

esp_err_t nino_audio_decode_wav(const uint8_t *wav_bytes, size_t wav_len,
                                nino_decoded_wav_t *out) {
  if (out == NULL) {
    return ESP_ERR_INVALID_ARG;
  }
  memset(out, 0, sizeof(*out));

  wav_pcm_t wav;
  if (!parse_wav_pcm(wav_bytes, wav_len, &wav)) {
    ESP_LOGE(TAG, "Invalid WAV (need PCM 16-bit mono or stereo, 8–48 kHz; fmt 1 or 0xFFFE)");
    return ESP_ERR_INVALID_ARG;
  }

  const int16_t *in_samples = (const int16_t *)wav.pcm;
  size_t in_bytes = wav.pcm_len;
  int16_t *pcm_owned = NULL;
  const int16_t *samples = in_samples;
  size_t num_bytes = wav.pcm_len;

  if (wav.channels == 2) {
    size_t frames = in_bytes / (sizeof(int16_t) * 2);
    size_t mono_bytes = frames * sizeof(int16_t);
    pcm_owned = (int16_t *)heap_caps_malloc(
        mono_bytes, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (pcm_owned == NULL) {
      pcm_owned = (int16_t *)malloc(mono_bytes);
    }
    if (pcm_owned == NULL) {
      return ESP_ERR_NO_MEM;
    }
    for (size_t i = 0; i < frames; i++) {
      int32_t L = in_samples[i * 2];
      int32_t R = in_samples[i * 2 + 1];
      pcm_owned[i] = (int16_t)((L + R) / 2);
    }
    samples = pcm_owned;
    num_bytes = mono_bytes;
  } else {
    pcm_owned = (int16_t *)heap_caps_malloc(num_bytes, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (pcm_owned == NULL) {
      pcm_owned = (int16_t *)malloc(num_bytes);
    }
    if (pcm_owned == NULL) {
      return ESP_ERR_NO_MEM;
    }
    memcpy(pcm_owned, in_samples, num_bytes);
    samples = pcm_owned;
  }

  out->samples = samples;
  out->num_bytes = num_bytes;
  out->sample_rate_hz = wav.sample_rate;
  out->mono_heap = pcm_owned;
  return ESP_OK;
}

void nino_decoded_wav_free(nino_decoded_wav_t *decoded) {
  if (decoded == NULL) {
    return;
  }
  free(decoded->mono_heap);
  memset(decoded, 0, sizeof(*decoded));
}

esp_err_t nino_audio_play_decoded(const nino_decoded_wav_t *decoded, size_t *pcm_byte_offset,
                                  volatile bool *stop_requested, bool *completed) {
  if (decoded == NULL || pcm_byte_offset == NULL || completed == NULL) {
    return ESP_ERR_INVALID_ARG;
  }
  *completed = false;

  if (decoded->samples == NULL || decoded->num_bytes == 0) {
    *completed = true;
    return ESP_OK;
  }

  if (!s_ready) {
    esp_err_t e = nino_audio_init();
    if (e != ESP_OK) {
      return e;
    }
  }

  size_t offset = *pcm_byte_offset;
  if (offset >= decoded->num_bytes) {
    *completed = true;
    return ESP_OK;
  }

  const uint8_t *play_ptr = (const uint8_t *)decoded->samples;

  xSemaphoreTake(s_mutex, portMAX_DELAY);

  esp_err_t open_err = spk_stream_open_locked(decoded->sample_rate_hz, false);
  if (open_err != ESP_OK) {
    xSemaphoreGive(s_mutex);
    return open_err;
  }
  ESP_LOGI(TAG, "Playing %u bytes @ %u Hz vol=%d%%",
           (unsigned)(decoded->num_bytes - offset),
           (unsigned)decoded->sample_rate_hz, s_volume_percent);

  const size_t session_start = offset;
  const TickType_t write_started = xTaskGetTickCount();
  bool stopped = false;
  int cr = ESP_CODEC_DEV_OK;
  while (offset < decoded->num_bytes) {
    if (stop_requested != NULL && *stop_requested) {
      stopped = true;
      break;
    }

    int block = (int)(decoded->num_bytes - offset);
    if (block > 4096) {
      block = 4096;
    }
    cr = esp_codec_dev_write(s_spk, (void *)(play_ptr + offset), block);
    if (cr != ESP_CODEC_DEV_OK) {
      ESP_LOGE(TAG, "esp_codec_dev_write failed: %d", cr);
      break;
    }
    offset += (size_t)block;
  }

  *pcm_byte_offset = offset;

  if (!stopped && cr == ESP_CODEC_DEV_OK && offset >= decoded->num_bytes) {
    const size_t written = offset - session_start;
    wait_pcm_pipeline_done(decoded->sample_rate_hz, written, write_started);
    *completed = true;
    ESP_LOGI(TAG, "Speaker finished %u bytes @ %u Hz", (unsigned)written,
             (unsigned)decoded->sample_rate_hz);
  }

  spk_stream_close_locked();
  nino_mic_drop_es8311_locked();
  xSemaphoreGive(s_mutex);

  if (cr != ESP_CODEC_DEV_OK) {
    return ESP_FAIL;
  }
  return ESP_OK;
}

#define NINO_AUDIO_WRITE_CHUNK 4096

esp_err_t nino_audio_write_pcm16_mono_locked(const int16_t *samples, size_t sample_count,
                                             uint32_t sample_rate_hz) {
  if (!s_ready) {
    return ESP_ERR_INVALID_STATE;
  }
  if (s_spk == NULL) {
    return ESP_ERR_INVALID_STATE;
  }
  if (sample_rate_hz < 8000 || sample_rate_hz > 48000) {
    return ESP_ERR_INVALID_ARG;
  }
  if (sample_count > 0 && samples == NULL) {
    return ESP_ERR_INVALID_ARG;
  }

  esp_err_t err = spk_stream_open_locked(sample_rate_hz, true);
  if (err != ESP_OK) {
    return err;
  }
  if (sample_count == 0) {
    return ESP_OK;
  }

  const size_t play_len = sample_count * sizeof(int16_t);
  const uint8_t *play_ptr = (const uint8_t *)samples;
  size_t offset = 0;
  while (offset < play_len) {
    int block = (int)(play_len - offset);
    if (block > NINO_AUDIO_WRITE_CHUNK) {
      block = NINO_AUDIO_WRITE_CHUNK;
    }
    const int cr = esp_codec_dev_write(s_spk, (void *)(play_ptr + offset), block);
    if (cr != ESP_CODEC_DEV_OK) {
      ESP_LOGE(TAG, "esp_codec_dev_write failed: %d", cr);
      return ESP_FAIL;
    }
    offset += (size_t)block;
  }
  return ESP_OK;
}

esp_err_t nino_audio_play_wav(const uint8_t *wav_bytes, size_t wav_len) {
  nino_decoded_wav_t decoded = {};
  esp_err_t err = nino_audio_decode_wav(wav_bytes, wav_len, &decoded);
  if (err != ESP_OK) {
    return err;
  }

  size_t offset = 0;
  bool completed = false;
  err = nino_audio_play_decoded(&decoded, &offset, NULL, &completed);
  nino_decoded_wav_free(&decoded);
  if (err != ESP_OK) {
    return err;
  }
  return completed ? ESP_OK : ESP_FAIL;
}

void nino_audio_bus_lock(void) {
  if (s_mutex == NULL) {
    (void)nino_audio_init();
  }
  if (s_mutex != NULL) {
    xSemaphoreTake(s_mutex, portMAX_DELAY);
  }
}

void nino_audio_bus_unlock(void) {
  if (s_mutex != NULL) {
    xSemaphoreGive(s_mutex);
  }
}

void nino_audio_drop_speaker_stream_locked(void) {
  if (s_spk_stream_open) {
    ESP_LOGI(TAG, "Closing speaker I2S so AUX ADC can take the duplex");
  }
  spk_stream_close_locked();
  s_spk_force_reopen = true;
}
```

## 13.7 Hardware soak test (GPIO48 / hub + camera + U2D2 load)

### `main/battery_endurance.h` — complete file (37 lines)

Copied from the tree as of this document. Do not edit here; edit the source file.

```c
#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "esp_err.h"

/**
 * Hardware load test (GPIO48 single press, or CLI `hwtest`).
 *
 * Starts / stops together:
 *   - both Dynamixels sweeping clockwise / anti-clockwise continuously
 *   - USB camera left streaming (UVC already running when the cam is on J18)
 *   - RGB LED cycling colours
 *   - TFT/OLED expressions changing every 5 s
 *
 * Same button press again stops motors, LED, and eyes.
 */
esp_err_t nino_battery_endurance_init(void);

void nino_battery_endurance_toggle(void);
esp_err_t nino_battery_endurance_start(void);
void nino_battery_endurance_stop(void);
bool nino_battery_endurance_is_active(void);

/** True while the test loop is running (before stop). Other tasks must not
 *  park motors, steal the RGB LED, or change eye expressions. */
bool nino_battery_endurance_owns_actuators(void);

/** True when the hardware-test task itself is calling into LED/eyes. */
bool nino_battery_endurance_is_self(void);

void nino_battery_endurance_cli_register(void);

/** UVC stream status — implemented in main.c */
bool nino_uvc_camera_connected(void);
uint32_t nino_uvc_frame_sequence(void);
```

### `main/battery_endurance.c` — complete file (404 lines)

Copied from the tree as of this document. Do not edit here; edit the source file.

```c
#include "battery_endurance.h"

#include <stdio.h>
#include <string.h>

#include "esp_console.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"

#include "face_tracker.h"
#include "nino_eye.h"
#include "rgb_led.h"
#include "servo_dxl.h"
#include "servo_motion.h"
#include "servo_recplay.h"

static const char *TAG = "hw_test";

#define LOOP_MS 50
#define RGB_STEP_MS 400
#define EYE_STEP_MS 5000
#define LOG_STEP_MS 5000
#define MILESTONE_MS 60000

#define TASK_STACK 4096
#define TASK_PRIO 4

typedef struct {
  const char *name;
  uint8_t r;
  uint8_t g;
  uint8_t b;
} soak_color_t;

typedef struct {
  nino_eye_state_t state;
  const char *name;
} soak_expr_t;

static const soak_color_t s_colors[] = {
    {"red", 255, 0, 0},       {"orange", 255, 102, 0}, {"yellow", 255, 255, 0},
    {"green", 0, 255, 0},     {"cyan", 0, 255, 255},   {"blue", 0, 0, 255},
    {"purple", 153, 0, 255},  {"magenta", 255, 0, 255}, {"pink", 255, 51, 102},
    {"white", 255, 255, 255}, {"lime", 128, 255, 0},    {"warm", 255, 153, 51},
};

static const soak_expr_t s_exprs[] = {
    {NINO_EYE_HAPPY, "happy"},         {NINO_EYE_SURPRISED, "surprised"},
    {NINO_EYE_CURIOUS_QUIZ, "curious"}, {NINO_EYE_SMILE, "smile"},
    {NINO_EYE_THINKING, "thinking"},    {NINO_EYE_SPARKLE, "sparkle"},
    {NINO_EYE_LISTENING, "listening"},  {NINO_EYE_BIGSMILE, "bigsmile"},
    {NINO_EYE_SAD, "sad"},              {NINO_EYE_ROBOT, "robot"},
    {NINO_EYE_MAD, "mad"},              {NINO_EYE_TIRED, "tired"},
};

#define COLOR_COUNT ((int)(sizeof(s_colors) / sizeof(s_colors[0])))
#define EXPR_COUNT ((int)(sizeof(s_exprs) / sizeof(s_exprs[0])))

static SemaphoreHandle_t s_lock;
static TaskHandle_t s_task;
static SemaphoreHandle_t s_done;
static volatile bool s_run;
static volatile bool s_active;

static bool s_face_track_was_on;
static int64_t s_start_us;
static bool s_warned_no_cam;
static bool s_warned_no_servo;
static int s_expr_log_left;

static void format_elapsed(int64_t elapsed_us, char *buf, size_t buflen) {
  if (elapsed_us < 0) {
    elapsed_us = 0;
  }
  const int64_t total_s = elapsed_us / 1000000LL;
  const int hours = (int)(total_s / 3600);
  const int mins = (int)((total_s % 3600) / 60);
  const int secs = (int)(total_s % 60);
  snprintf(buf, buflen, "%02d:%02d:%02d", hours, mins, secs);
}

static void apply_rgb(int index) {
  const soak_color_t *c = &s_colors[index % COLOR_COUNT];
  (void)nino_rgb_led_set_rgb(c->r, c->g, c->b);
}

static void apply_eye(int index) {
  const soak_expr_t *e = &s_exprs[index % EXPR_COUNT];
  nino_eye_set_state(e->state);
}

static const char *servo_status(void) {
  if (nino_servo_dxl_is_ready()) {
    return "READY";
  }
  if (nino_servo_dxl_bus_open()) {
    return "U2D2 OPEN (waiting PING/torque)";
  }
  return "U2D2 DOWN";
}

static const char *cam_state(uint32_t *seq_out) {
  const uint32_t seq = nino_uvc_frame_sequence();
  if (seq_out != NULL) {
    *seq_out = seq;
  }
  if (nino_uvc_camera_connected()) {
    return seq > 0U ? "STREAMING" : "CONNECTED (no frame yet)";
  }
  return "NOT CONNECTED";
}

static void log_heartbeat(int64_t now_us, int color_i, int expr_i,
                          uint32_t last_cam_seq) {
  char elapsed[16];
  format_elapsed(now_us - s_start_us, elapsed, sizeof(elapsed));

  uint32_t cam_seq = 0;
  const char *cam = cam_state(&cam_seq);
  const uint32_t fps_est = (cam_seq >= last_cam_seq)
                               ? (uint32_t)((cam_seq - last_cam_seq) * 1000U / LOG_STEP_MS)
                               : 0;

  ESP_LOGI(TAG, "t=%s  cam=%s #%lu ~%u fps  motors=%s  eye=%s  rgb=%s",
           elapsed, cam, (unsigned long)cam_seq, (unsigned)fps_est, servo_status(),
           s_exprs[expr_i % EXPR_COUNT].name, s_colors[color_i % COLOR_COUNT].name);

  if (!nino_uvc_camera_connected() && !s_warned_no_cam) {
    s_warned_no_cam = true;
    ESP_LOGW(TAG, "USB camera not streaming — plug UVC cam into the J18 hub");
  } else if (nino_uvc_camera_connected()) {
    s_warned_no_cam = false;
  }

  if (!nino_servo_dxl_is_ready() && !s_warned_no_servo) {
    s_warned_no_servo = true;
    ESP_LOGW(TAG, "Servos not ready — check J18 hub + U2D2 + Dynamixel power");
  } else if (nino_servo_dxl_is_ready()) {
    s_warned_no_servo = false;
  }
}

static void log_start_banner(void) {
  uint32_t cam_seq = 0;
  const char *cam = cam_state(&cam_seq);

  ESP_LOGI(TAG, "========== HARDWARE TEST START ==========");
  ESP_LOGI(TAG, "cam=%s #%lu   servos=%s", cam, (unsigned long)cam_seq,
           servo_status());
  ESP_LOGI(TAG, "motors: continuous pan+tilt CW/CCW until the same button is pressed");
  ESP_LOGI(TAG, "camera stream  |  RGB cycle %d ms  |  TFT expr every %d s",
           RGB_STEP_MS, EYE_STEP_MS / 1000);
  ESP_LOGI(TAG, "Press GPIO48 once more (or type 'hwtest') to STOP");
}

static void log_stop_banner(void) {
  char elapsed[16];
  format_elapsed(esp_timer_get_time() - s_start_us, elapsed, sizeof(elapsed));
  ESP_LOGI(TAG, "========== HARDWARE TEST STOP ==========");
  ESP_LOGI(TAG, "ran %s  motors parked at center  LED off  eyes idle", elapsed);
}

static void restore_idle(void) {
  nino_servo_motion_stop();
  nino_servo_dxl_go_neutral();
  (void)nino_rgb_led_show(NINO_RGB_SHOW_IDLE);
  nino_eye_idle();
  if (s_face_track_was_on) {
    nino_face_tracker_set_enabled(true);
  }
}

static void soak_task(void *arg) {
  (void)arg;

  int color_i = 0;
  int expr_i = 0;
  uint32_t rgb_ms = 0;
  uint32_t eye_ms = 0;
  uint32_t log_ms = 0;
  uint32_t milestone_ms = 0;
  uint32_t last_cam_seq = nino_uvc_frame_sequence();

  (void)nino_rgb_led_show(NINO_RGB_SHOW_IDLE);
  apply_rgb(0);
  apply_eye(0);
  nino_servo_motion_start(NINO_SERVO_MOTION_SWEEP);

  while (s_run) {
    vTaskDelay(pdMS_TO_TICKS(LOOP_MS));
    rgb_ms += LOOP_MS;
    eye_ms += LOOP_MS;
    log_ms += LOOP_MS;
    milestone_ms += LOOP_MS;

    if (!nino_servo_motion_is_active()) {
      nino_servo_motion_start(NINO_SERVO_MOTION_SWEEP);
    }

    if (rgb_ms >= RGB_STEP_MS) {
      rgb_ms = 0;
      color_i = (color_i + 1) % COLOR_COUNT;
      apply_rgb(color_i);
    }

    if (eye_ms >= EYE_STEP_MS) {
      eye_ms = 0;
      expr_i = (expr_i + 1) % EXPR_COUNT;
      apply_eye(expr_i);
      if (s_expr_log_left > 0) {
        s_expr_log_left--;
        ESP_LOGI(TAG, "TFT expression -> %s", s_exprs[expr_i].name);
      }
    }

    if (log_ms >= LOG_STEP_MS) {
      log_ms = 0;
      log_heartbeat(esp_timer_get_time(), color_i, expr_i, last_cam_seq);
      last_cam_seq = nino_uvc_frame_sequence();
    }

    if (milestone_ms >= MILESTONE_MS) {
      milestone_ms = 0;
      char elapsed[16];
      format_elapsed(esp_timer_get_time() - s_start_us, elapsed, sizeof(elapsed));
      ESP_LOGI(TAG, "--- milestone %s still running ---", elapsed);
    }
  }

  restore_idle();
  log_stop_banner();
  s_active = false;
  s_task = NULL;
  if (s_done != NULL) {
    xSemaphoreGive(s_done);
  }
  vTaskDelete(NULL);
}

static bool lock_take(void) {
  if (s_lock == NULL) {
    return false;
  }
  return xSemaphoreTake(s_lock, pdMS_TO_TICKS(500)) == pdTRUE;
}

static void lock_give(void) {
  if (s_lock != NULL) {
    xSemaphoreGive(s_lock);
  }
}

esp_err_t nino_battery_endurance_init(void) {
  if (s_lock == NULL) {
    s_lock = xSemaphoreCreateMutex();
    if (s_lock == NULL) {
      return ESP_ERR_NO_MEM;
    }
  }
  if (s_done == NULL) {
    s_done = xSemaphoreCreateBinary();
    if (s_done == NULL) {
      return ESP_ERR_NO_MEM;
    }
  }
  ESP_LOGI(TAG,
           "Ready — GPIO48 single press starts/stops hardware test "
           "(motors + camera + RGB + TFT). CLI: hwtest");
  return ESP_OK;
}

bool nino_battery_endurance_is_active(void) {
  return s_active;
}

bool nino_battery_endurance_owns_actuators(void) {
  return s_run;
}

bool nino_battery_endurance_is_self(void) {
  return s_run && s_task != NULL && xTaskGetCurrentTaskHandle() == s_task;
}

esp_err_t nino_battery_endurance_start(void) {
  if (!lock_take()) {
    return ESP_ERR_INVALID_STATE;
  }

  if (s_active) {
    lock_give();
    ESP_LOGI(TAG, "Test already running — press GPIO48 once to stop");
    return ESP_OK;
  }

  if (nino_servo_recplay_is_busy()) {
    lock_give();
    ESP_LOGW(TAG, "Cannot start: servo record/play is busy");
    return ESP_ERR_INVALID_STATE;
  }

  if (nino_servo_dxl_spin_is_active() || nino_servo_dxl_track_hon_is_active()) {
    ESP_LOGI(TAG, "Stopping spin/track-hon so the test can own both motors");
    (void)nino_servo_dxl_track_hon_stop();
  }
  nino_servo_motion_stop();

  s_face_track_was_on = nino_face_tracker_is_enabled();
  if (s_face_track_was_on) {
    nino_face_tracker_set_enabled(false);
  }

  s_start_us = esp_timer_get_time();
  s_warned_no_cam = false;
  s_warned_no_servo = false;
  s_expr_log_left = 6;
  s_run = true;
  s_active = true;
  if (s_done != NULL) {
    (void)xSemaphoreTake(s_done, 0);
  }

  log_start_banner();

  BaseType_t ok = xTaskCreate(soak_task, "hw_test", TASK_STACK, NULL, TASK_PRIO,
                              &s_task);
  if (ok != pdPASS) {
    s_run = false;
    s_active = false;
    s_task = NULL;
    if (s_face_track_was_on) {
      nino_face_tracker_set_enabled(true);
    }
    lock_give();
    ESP_LOGE(TAG, "Failed to create hardware test task");
    return ESP_ERR_NO_MEM;
  }

  lock_give();
  return ESP_OK;
}

void nino_battery_endurance_stop(void) {
  if (!lock_take()) {
    return;
  }
  if (!s_active && s_task == NULL) {
    lock_give();
    ESP_LOGI(TAG, "Hardware test is not running");
    return;
  }

  ESP_LOGI(TAG, "Stop requested — winding down motors / LED / TFT");
  s_run = false;
  TaskHandle_t task = s_task;
  lock_give();

  if (task != NULL && s_done != NULL) {
    if (xSemaphoreTake(s_done, pdMS_TO_TICKS(2000)) != pdTRUE) {
      ESP_LOGW(TAG, "Hardware test task stop timeout");
    }
  }
}

void nino_battery_endurance_toggle(void) {
  if (s_active) {
    nino_battery_endurance_stop();
  } else {
    (void)nino_battery_endurance_start();
  }
}

static int cmd_hwtest(int argc, char **argv) {
  if (argc >= 2) {
    if (strcmp(argv[1], "on") == 0 || strcmp(argv[1], "start") == 0) {
      return nino_battery_endurance_start() == ESP_OK ? 0 : 1;
    }
    if (strcmp(argv[1], "off") == 0 || strcmp(argv[1], "stop") == 0) {
      nino_battery_endurance_stop();
      return 0;
    }
    if (strcmp(argv[1], "status") == 0) {
      printf("hardware test: %s\n", s_active ? "RUNNING" : "idle");
      return 0;
    }
    printf("Usage: hwtest [on|off|status]\n");
    return 1;
  }
  nino_battery_endurance_toggle();
  return 0;
}

void nino_battery_endurance_cli_register(void) {
  const esp_console_cmd_t cmd = {
      .command = "hwtest",
      .help = "hwtest | on | off | status  — GPIO48 motors+cam+RGB+TFT load test",
      .hint = NULL,
      .func = &cmd_hwtest,
      .argtable = NULL,
  };
  ESP_ERROR_CHECK(esp_console_cmd_register(&cmd));
}
```

## 13.8 `main/main.c` — complete file (4334 lines)

Where this session’s work lands inside `main.c` (the fenced block below is the **entire file**):

| Lines | What |
|------:|------|
| 44–59 | Battery / mute / servo includes |
| 890–908 | RGB: do not overwrite mute or low-battery scenes |
| 1408–1426 | Console: `adc`, `hwtest` |
| 1555–1561 | UVC helpers for hub soak test |
| 3649–3707 | `speaker mute` CLI |
| 3931–3945 | USB host lib task (hub + camera + U2D2) |
| 4246–4334 | Boot: ADC, buttons, USB host, **U2D2 before UVC** |

Copied from the tree as of this document. Do not edit here; edit the source file.

### `main/main.c` — complete file (4334 lines)

```c
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/semphr.h"
#include "freertos/task.h"

#include "esp_check.h"
#include "esp_console.h"
#include "esp_err.h"
#include "esp_event.h"
#include "esp_heap_caps.h"
#include "esp_http_client.h"
#include "esp_http_server.h"
#include "esp_log.h"
#include "esp_mac.h"
#include "esp_netif.h"
#include "esp_timer.h"
#include "esp_wifi.h"
#include "lwip/inet.h"
#include "lwip/ip4_addr.h"
#include "lwip/sockets.h"
#include "nvs.h"
#include "nvs_flash.h"
#include "sdkconfig.h"
#include "usb/usb_host.h"
#include "usb/uvc_host.h"

#if __has_include("mdns.h")
#include "mdns.h"
#define NINO_HAS_MDNS 1
#elif __has_include("mdns/include/mdns.h")
#include "mdns/include/mdns.h"
#define NINO_HAS_MDNS 1
#else
#define NINO_HAS_MDNS 0
#endif

#include "audio_playback.h"
#include "audio_capture.h"
#include "audio_queue.h"
#include "battery_adc.h"
#include "battery_endurance.h"
#include "camera_orientation.h"
#include "face_detect.hpp"
#include "face_tracker.h"
#include "mic_input.h"
#include "nino_eye.h"
#include "nino_display.h"
#include "rgb_led.h"
#include "servo_dxl.h"
#include "servo_motion.h"
#include "servo_recplay.h"
#include "push_buttons.h"
#include "voice_assist.h"
#include "music_stream.h"
#include "wifi_config.h"
#include "wifi_prov_ble.h"
#if CONFIG_ESP_HOSTED_ENABLED
#include "esp_hosted.h"
#endif
#define MAX_STA_CONN 4

#define NVS_NAMESPACE "wifi_cfg"
#define NVS_KEY_VOICE_WS "voice_ws"
#define NVS_KEY_MODE "mode"
#define NVS_KEY_STA_SSID "sta_ssid"
#define NVS_KEY_STA_PASS "sta_pass"
#define NVS_KEY_DEVICE_NAME "dev_name"
#define NVS_KEY_DEVICE_ID "device_id"
#define DEVICE_ID_MAX 32

#define MULTICAST_ADDR "239.255.255.250"
#define BROADCAST_ADDR "255.255.255.255"
#define DISCOVERY_PORT 1900
#define MESSAGE_PORT 8888
#define DISCOVERY_MSG "discover"
#define DISCOVERY_BUF 256
#define MESSAGE_BUF 256
#define DEFAULT_VOICE_SERVER_PORT 8000
#define MDNS_HOSTNAME_MAX DEVICE_ID_MAX

#define STA_RECONNECT_DELAY_MS 5000
static char s_sta_ssid[WIFI_CONFIG_STA_SSID_MAX] = "";
static char s_sta_pass[WIFI_CONFIG_STA_PASS_MAX] = "";

static wifi_mode_t s_wifi_mode = WIFI_MODE_AP;
static bool s_sta_connected = false;
static bool s_server_link_ok = false;
static volatile bool s_server_probe_active = false;
static bool s_wifi_connected_chime_pending = false;
static bool s_audio_queue_ready = false;
static volatile bool s_wifi_connected_chime_task_running = false;
static bool s_boot_unprovisioned = false;
static volatile bool s_provisioned_welcome_scheduled = false;
static bool s_boot_greeting_done = false;
static bool s_mdns_started = false;

#define USB_LIB_TASK_STACK_SIZE 4096
#define USB_LIB_TASK_PRIORITY 20
#define UVC_DRIVER_TASK_STACK_SIZE 4096
#define UVC_DRIVER_TASK_PRIORITY 21
#define UVC_STREAM_TASK_STACK_SIZE 8192
#define UVC_STREAM_TASK_PRIORITY 19

#define UVC_TARGET_WIDTH 320
#define UVC_TARGET_HEIGHT 240
#define UVC_TARGET_FPS 15.0f
#define UVC_FRAME_QUEUE_LEN 3
#define UVC_FRAME_BUFFERS 3
#define UVC_URB_COUNT 8
#define UVC_URB_SIZE (12 * 1024)
#define UVC_FRAME_SIZE_BYTES (92 * 1024)
#define UVC_FRAME_TIMEOUT_LOG_INTERVAL_MS 15000
#define UVC_OPEN_TIMEOUT_MS 5000
#define FACE_TRACK_TASK_STACK_SIZE (12 * 1024)
#define FACE_TRACK_TASK_PRIORITY 5
#define FACE_TRACK_NOTIFY_WAIT_MS 40
/* Run detection below camera FPS so tracking does not steal too much CPU. */
#define FACE_TRACK_INFERENCE_INTERVAL_MS 200
/* Reuse last face briefly when the stream hiccups to avoid servo twitching. */
#define FACE_TRACK_REUSE_LAST_FACE_MS 8000

#define HTTP_STREAM_BOUNDARY "frame"
#define HTTP_SERVER_PORT 80
#define HTTP_STREAM_POLL_MS 25
#define HTTP_STREAM_ROTATE_DEG NINO_CAMERA_ROTATION_DEG
#define MAX_PLAY_WAV_BYTES (384 * 1024)
#define STR_HELPER(x) #x
#define STR(x) STR_HELPER(x)
#define DEVICE_NAME_DEFAULT WIFI_PROV_BLE_DEVICE_NAME_DEFAULT
#define MDNS_SERVICE_TYPE "_nino"
#define MDNS_SERVICE_PROTO "_tcp"
#ifndef PROJECT_VER
#define PROJECT_VER "unknown"
#endif

#if CONFIG_FREERTOS_NUMBER_OF_CORES > 1
#define APP_CORE_NET 0
#define APP_CORE_USB 1
#else
#define APP_CORE_NET tskNO_AFFINITY
#define APP_CORE_USB tskNO_AFFINITY
#endif

typedef struct {
  uint8_t dev_addr;
  uint8_t stream_index;
  uvc_host_stream_format_t format;
} selected_stream_t;

typedef struct {
  uint8_t *data;
  size_t capacity;
  size_t len;
  uint32_t sequence;
  uint16_t width;
  uint16_t height;
  uvc_host_stream_format_t format;
  bool ready;
} latest_frame_t;

static const char *TAG = "usb_camera";

static TaskHandle_t s_stream_task_handle;
static TaskHandle_t s_face_track_task_handle;
static QueueHandle_t s_frame_queue;
static volatile bool s_device_connected;
static volatile bool s_stream_task_created;
static selected_stream_t s_selected_stream;
static uvc_host_frame_info_t *s_frame_info_list;
static size_t s_frame_info_count;
static SemaphoreHandle_t s_frame_mutex;
static latest_frame_t s_latest_frame;

static httpd_handle_t s_http_server;
static esp_console_repl_t *s_repl;
static char s_voice_ws_url[200];
static int64_t s_last_uvc_timeout_log_us;
static char s_device_name[WIFI_PROV_BLE_DEVICE_NAME_MAX + 1] =
    DEVICE_NAME_DEFAULT;
static char s_device_id[DEVICE_ID_MAX + 1] = "";

extern const uint8_t wifi_wav_start[] asm("_binary_WIFI_wav_start");
extern const uint8_t wifi_wav_end[] asm("_binary_WIFI_wav_end");

extern const uint8_t hello_home_wav_start[] asm("_binary_Hello_home_wav_start");
extern const uint8_t hello_home_wav_end[] asm("_binary_Hello_home_wav_end");

extern const uint8_t wifi_unable_wav_start[] asm("_binary_Wifi_Unable_wav_start");
extern const uint8_t wifi_unable_wav_end[] asm("_binary_Wifi_Unable_wav_end");

extern const uint8_t go_app_wav_start[] asm("_binary_NiNO_Home_Wifi_wav_start");
extern const uint8_t go_app_wav_end[] asm("_binary_NiNO_Home_Wifi_wav_end");

extern const uint8_t schedule_dinnner_wav_start[] asm("_binary_schedule_dinnner_wav_start");
extern const uint8_t schedule_dinnner_wav_end[] asm("_binary_schedule_dinnner_wav_end");

extern const uint8_t bday_surprise_wav_start[] asm("_binary_Bday_Surprise_wav_start");
extern const uint8_t bday_surprise_wav_end[] asm("_binary_Bday_Surprise_wav_end");

/* Set once Wifi_Unable.wav has been played for the current connect attempt so
 * the prompt is not repeated on every reconnect retry. Reset on success and on
 * fresh credentials from GATT provisioning. */
static volatile bool s_wifi_unable_chimed = false;

static bool play_wifi_connected_clip(void) {
  const size_t wav_len = (size_t)(wifi_wav_end - wifi_wav_start);
  if (wav_len < 44) {
    ESP_LOGW(TAG, "Embedded WIFI.wav missing or too small");
    return false;
  }

  esp_err_t err = nino_audio_queue_wav_copy(wifi_wav_start, wav_len, false,
                                            NINO_AUDIO_SERVO_PRIORITY_NONE, false);
  if (err != ESP_OK) {
    ESP_LOGW(TAG, "Failed to queue WIFI.wav: %s", esp_err_to_name(err));
    return false;
  }

  ESP_LOGI(TAG, "Queued WIFI.wav (%u bytes) after STA connect",
           (unsigned)wav_len);
  return true;
}

/* The IP event can happen before the audio task is ready. Keep the connection
 * chime pending and retry its queue operation instead of losing it. */
static void wifi_connected_chime_task(void *arg) {
  (void)arg;
  for (int attempt = 0; attempt < 120 && s_sta_connected &&
                        s_wifi_connected_chime_pending;
       ++attempt) {
    if (s_audio_queue_ready && play_wifi_connected_clip()) {
      s_wifi_connected_chime_pending = false;
      break;
    }
    vTaskDelay(pdMS_TO_TICKS(250));
  }
  if (s_wifi_connected_chime_pending && s_sta_connected) {
    ESP_LOGW(TAG, "WIFI.wav was not queued after STA connection");
  }
  s_wifi_connected_chime_task_running = false;
  vTaskDelete(NULL);
}

static void schedule_wifi_connected_chime(void) {
  if (!s_sta_connected || !s_wifi_connected_chime_pending ||
      s_wifi_connected_chime_task_running) {
    return;
  }
  s_wifi_connected_chime_task_running = true;
  if (xTaskCreate(wifi_connected_chime_task, "wifi_chime", 3072, NULL, 5,
                  NULL) != pdPASS) {
    s_wifi_connected_chime_task_running = false;
    ESP_LOGW(TAG, "WIFI.wav task not started");
  }
}

static bool play_hello_home_clip(void) {
  const size_t wav_len = (size_t)(hello_home_wav_end - hello_home_wav_start);
  if (wav_len < 44) {
    ESP_LOGW(TAG, "Embedded Hello-home.wav missing or too small");
    return false;
  }

  esp_err_t err = nino_audio_queue_wav_copy(hello_home_wav_start, wav_len, false,
                                            NINO_AUDIO_SERVO_NONE, false);
  if (err != ESP_OK) {
    ESP_LOGW(TAG, "Failed to queue Hello-home.wav: %s", esp_err_to_name(err));
    return false;
  }

  ESP_LOGI(TAG, "Queued Hello-home.wav (%u bytes) after boot",
           (unsigned)wav_len);
  return true;
}

/* A boot that began without credentials finishes its NiNO-Home_Wifi greeting before
 * BLE provisioning completes. Add the normal welcome after the Wi-Fi chime. */
static void provisioned_welcome_task(void *arg) {
  (void)arg;
  for (int waited_ms = 0;
       waited_ms < 30000 && s_sta_connected && s_wifi_connected_chime_pending;
       waited_ms += 100) {
    vTaskDelay(pdMS_TO_TICKS(100));
  }
  if (s_sta_connected && !s_wifi_connected_chime_pending) {
    (void)play_hello_home_clip();
  }
  vTaskDelete(NULL);
}

static void schedule_provisioned_welcome(void) {
  if (!s_boot_unprovisioned || s_provisioned_welcome_scheduled) {
    return;
  }
  s_provisioned_welcome_scheduled = true;
  if (xTaskCreate(provisioned_welcome_task, "prov_welcome", 3072, NULL, 4,
                  NULL) != pdPASS) {
    s_provisioned_welcome_scheduled = false;
    ESP_LOGW(TAG, "Hello-home task not started after provisioning");
  }
}

static bool play_wifi_unable_clip(void) {
  const size_t wav_len = (size_t)(wifi_unable_wav_end - wifi_unable_wav_start);
  if (wav_len < 44) {
    ESP_LOGW(TAG, "Embedded Wifi_Unable.wav missing or too small");
    return false;
  }

  esp_err_t err = nino_audio_queue_wav_copy(wifi_unable_wav_start, wav_len, false,
                                            NINO_AUDIO_SERVO_PRIORITY_NONE, false);
  if (err != ESP_OK) {
    ESP_LOGW(TAG, "Failed to queue Wifi_Unable.wav: %s", esp_err_to_name(err));
    return false;
  }

  ESP_LOGI(TAG, "Queued Wifi_Unable.wav (%u bytes): STA connect failed",
           (unsigned)wav_len);
  return true;
}

static bool play_go_app_clip(void) {
  const size_t wav_len = (size_t)(go_app_wav_end - go_app_wav_start);
  if (wav_len < 44) {
    ESP_LOGW(TAG, "Embedded NiNO-Home_Wifi.wav missing or too small");
    return false;
  }

  esp_err_t err = nino_audio_queue_wav_copy(go_app_wav_start, wav_len, false,
                                            NINO_AUDIO_SERVO_PRIORITY_NONE, false);
  if (err != ESP_OK) {
    ESP_LOGW(TAG, "Failed to queue NiNO-Home_Wifi.wav: %s", esp_err_to_name(err));
    return false;
  }

  ESP_LOGI(TAG, "Queued NiNO-Home_Wifi.wav (%u bytes): no saved Wi-Fi network",
           (unsigned)wav_len);
  return true;
}

/* Disconnect reasons that mean "could not join the network" (wrong password,
 * auth/handshake failure, or SSID not found) rather than a transient drop. */
static bool wifi_disconnect_is_connect_failure(uint8_t reason) {
  switch (reason) {
    case WIFI_REASON_AUTH_EXPIRE:
    case WIFI_REASON_AUTH_FAIL:
    case WIFI_REASON_4WAY_HANDSHAKE_TIMEOUT:
    case WIFI_REASON_HANDSHAKE_TIMEOUT:
    case WIFI_REASON_NO_AP_FOUND:
    case WIFI_REASON_ASSOC_FAIL:
    case WIFI_REASON_CONNECTION_FAIL:
      return true;
    default:
      return false;
  }
}

/* Boot greeting:
 *  - No saved Wi-Fi network in NVS -> prompt with NiNO-Home_Wifi.wav.
 *  - Provisioned and connected -> greet with Hello-home after the WIFI.wav clip.
 *    Falls back to greeting anyway if Wi-Fi never connects within the timeout,
 *    unless we already played the "unable to connect" prompt. */
static void finish_boot_greeting_and_enable_wake(void) {
  nino_audio_queue_wait_idle(45000);
  if (nino_voice_preload_wake_chime() == ESP_OK) {
    ESP_LOGI(TAG, "Speaker path ready after boot greeting");
  } else {
    ESP_LOGW(TAG, "Speaker warm after boot greeting failed");
  }
  s_boot_greeting_done = true;
  if (s_voice_ws_url[0] == '\0') {
    ESP_LOGW(TAG,
             "Voice PC URL not set — serial: voice connect <YOUR_PC_LAN_IP> 8000 "
             "then start [seconds]");
  } else {
    ESP_LOGI(TAG, "Voice assistant URL: %s — Aux-in listen arms after greeting", s_voice_ws_url);
  }
  nino_voice_assist_start_listen_loop();
  ESP_LOGI(TAG, "Aux-in listen on — Sirena energy starts VAD capture (not a fixed 5 s)");
}

static void hello_home_task(void *arg) {
  (void)arg;
  if (s_sta_ssid[0] == '\0') {
    /* NiNO-Home_Wifi.wav is queued immediately after audio setup during boot. */
    finish_boot_greeting_and_enable_wake();
    vTaskDelete(NULL);
    return;
  }

  const int timeout_ms = 60000;
  int waited_ms = 0;
  while (waited_ms < timeout_ms && !s_wifi_unable_chimed &&
         !(s_sta_connected && !s_wifi_connected_chime_pending)) {
    vTaskDelay(pdMS_TO_TICKS(100));
    waited_ms += 100;
  }
  if (s_sta_connected) {
    play_hello_home_clip();
  }
  finish_boot_greeting_and_enable_wake();
  vTaskDelete(NULL);
}

static bool is_valid_device_id(const char *id);

/** DNS-safe mDNS hostname: prefer stable device_id (unique per robot on LAN). */
static void mdns_hostname_for_device(char *dst, size_t dst_size) {
  if (dst == NULL || dst_size == 0) {
    return;
  }
  const char *src =
      (s_device_id[0] != '\0' && is_valid_device_id(s_device_id)) ? s_device_id
                                                                  : "nino";
  size_t out = 0;
  for (size_t i = 0; src[i] != '\0' && out + 1 < dst_size; ++i) {
    char c = src[i];
    if (c >= 'A' && c <= 'Z') {
      c = (char)(c - 'A' + 'a');
    }
    if ((c >= 'a' && c <= 'z') || (c >= '0' && c <= '9') || c == '-') {
      dst[out++] = c;
    } else if (c == '_') {
      dst[out++] = '-';
    }
  }
  if (out == 0) {
    strncpy(dst, "nino", dst_size - 1);
    dst[dst_size - 1] = '\0';
    return;
  }
  dst[out] = '\0';
}

#if NINO_HAS_MDNS
static void mdns_stop_service(void) {
  if (!s_mdns_started) {
    return;
  }
  mdns_free();
  s_mdns_started = false;
  ESP_LOGI(TAG, "mDNS stopped");
}

static void mdns_start_service(void) {
  if (s_mdns_started) {
    return;
  }

  esp_err_t err = mdns_init();
  if (err != ESP_OK) {
    ESP_LOGW(TAG, "mDNS init failed: %s", esp_err_to_name(err));
    return;
  }

  char hostname[MDNS_HOSTNAME_MAX + 1];
  mdns_hostname_for_device(hostname, sizeof(hostname));

  err = mdns_hostname_set(hostname);
  if (err != ESP_OK) {
    ESP_LOGW(TAG, "mDNS hostname set failed: %s", esp_err_to_name(err));
    mdns_free();
    return;
  }

  /* Friendly display name for browsers; hostname stays unique via device_id. */
  err = mdns_instance_name_set(s_device_name);
  if (err != ESP_OK) {
    ESP_LOGW(TAG, "mDNS instance set failed: %s", esp_err_to_name(err));
    mdns_free();
    return;
  }

  err = mdns_service_add(s_device_name, MDNS_SERVICE_TYPE, MDNS_SERVICE_PROTO,
                         HTTP_SERVER_PORT, NULL, 0);
  if (err != ESP_OK) {
    ESP_LOGW(TAG, "mDNS service add failed: %s", esp_err_to_name(err));
    mdns_free();
    return;
  }

  mdns_txt_item_t txt[] = {
      {"device", "nino"},
      {"device_id", s_device_id},
      {"ble_name", s_device_name},
      {"transport", "http"},
      {"path", "/status"},
  };
  err = mdns_service_txt_set(MDNS_SERVICE_TYPE, MDNS_SERVICE_PROTO, txt,
                             sizeof(txt) / sizeof(txt[0]));
  if (err != ESP_OK) {
    ESP_LOGW(TAG, "mDNS TXT set failed: %s", esp_err_to_name(err));
    mdns_free();
    return;
  }

  s_mdns_started = true;
  ESP_LOGI(TAG,
           "mDNS ready: %s.local (device_id=%s name=%s) service %s.%s port %d",
           hostname, s_device_id, s_device_name, MDNS_SERVICE_TYPE,
           MDNS_SERVICE_PROTO, HTTP_SERVER_PORT);
}
#else
static void mdns_stop_service(void) {}

static void mdns_start_service(void) {
  static bool s_logged_missing_mdns;
  if (!s_logged_missing_mdns) {
    s_logged_missing_mdns = true;
    ESP_LOGW(TAG, "mDNS headers not available in current build environment");
  }
}
#endif

static void copy_cstr_field(uint8_t *dst, size_t dst_size, const char *src) {
  if (dst == NULL || dst_size == 0 || src == NULL) {
    return;
  }

  size_t len = strnlen(src, dst_size - 1);
  memcpy(dst, src, len);
  dst[len] = '\0';
}

static void copy_device_name(char *dst, size_t dst_size, const char *src) {
  if (dst == NULL || dst_size == 0) {
    return;
  }
  if (src == NULL || src[0] == '\0') {
    src = DEVICE_NAME_DEFAULT;
  }
  size_t len = strnlen(src, dst_size - 1);
  memcpy(dst, src, len);
  dst[len] = '\0';
}

static bool is_valid_device_name(const char *name) {
  if (name == NULL || name[0] == '\0') {
    return false;
  }
  size_t len = strnlen(name, WIFI_PROV_BLE_DEVICE_NAME_MAX + 1);
  if (len == 0 || len > WIFI_PROV_BLE_DEVICE_NAME_MAX) {
    return false;
  }
  for (size_t i = 0; i < len; ++i) {
    char c = name[i];
    if ((unsigned char)c < 32U || c == '"' || c == '\\') {
      return false;
    }
  }
  return true;
}

static bool is_valid_device_id(const char *id) {
  if (id == NULL || id[0] == '\0') {
    return false;
  }
  size_t len = strnlen(id, DEVICE_ID_MAX + 1);
  if (len == 0 || len > DEVICE_ID_MAX) {
    return false;
  }
  for (size_t i = 0; i < len; ++i) {
    char c = id[i];
    if (!((c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') ||
          (c >= '0' && c <= '9') || c == '-' || c == '_')) {
      return false;
    }
  }
  return true;
}

/* Early multi-robot builds stored this placeholder on every board. It is a
 * syntactically valid ID, but cannot safely identify a robot on a shared LAN. */
static bool is_legacy_placeholder_device_id(const char *id) {
  return id != NULL && strcmp(id, "nino-000000") == 0;
}

static void make_default_device_id_from_mac(char *dst, size_t dst_size) {
  uint8_t mac[6] = {0};
  if (esp_read_mac(mac, ESP_MAC_WIFI_STA) != ESP_OK) {
    (void)esp_wifi_get_mac(WIFI_IF_STA, mac);
  }
  snprintf(dst, dst_size, "nino-%02x%02x%02x", mac[3], mac[4], mac[5]);
}

static bool make_device_id_from_name(char *dst, size_t dst_size) {
  if (dst == NULL || dst_size < 2) {
    return false;
  }

  char name[WIFI_PROV_BLE_DEVICE_NAME_MAX + 1] = "";
  nvs_handle_t h;
  if (nvs_open(NVS_NAMESPACE, NVS_READONLY, &h) == ESP_OK) {
    size_t len = sizeof(name);
    if (nvs_get_str(h, NVS_KEY_DEVICE_NAME, name, &len) != ESP_OK ||
        !is_valid_device_name(name)) {
      name[0] = '\0';
    }
    nvs_close(h);
  }
  if (name[0] == '\0') {
    copy_device_name(name, sizeof(name), s_device_name);
  }

  size_t out = 0;
  bool previous_dash = false;
  for (size_t i = 0; name[i] != '\0' && out < dst_size - 1; ++i) {
    char c = name[i];
    if ((c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') ||
        (c >= '0' && c <= '9')) {
      dst[out++] = (c >= 'A' && c <= 'Z') ? (char)(c - 'A' + 'a') : c;
      previous_dash = false;
    } else if (out > 0 && !previous_dash) {
      dst[out++] = '-';
      previous_dash = true;
    }
  }
  while (out > 0 && dst[out - 1] == '-') {
    --out;
  }
  dst[out] = '\0';
  return out > 0 && is_valid_device_id(dst);
}

static void copy_device_id(char *dst, size_t dst_size, const char *src) {
  if (dst == NULL || dst_size == 0) {
    return;
  }
  if (src == NULL || !is_valid_device_id(src)) {
    make_default_device_id_from_mac(dst, dst_size);
    return;
  }
  size_t len = strnlen(src, dst_size - 1);
  memcpy(dst, src, len);
  dst[len] = '\0';
}

static esp_err_t save_device_id_to_nvs(void) {
  nvs_handle_t h;
  esp_err_t err = nvs_open(NVS_NAMESPACE, NVS_READWRITE, &h);
  if (err != ESP_OK) {
    return err;
  }
  err = nvs_set_str(h, NVS_KEY_DEVICE_ID, s_device_id);
  if (err == ESP_OK) {
    err = nvs_commit(h);
  }
  nvs_close(h);
  return err;
}

static void load_device_id_from_nvs(void) {
  nvs_handle_t h;
  char stored[DEVICE_ID_MAX + 1] = "";
  bool migrate_placeholder = false;
  if (nvs_open(NVS_NAMESPACE, NVS_READONLY, &h) == ESP_OK) {
    size_t len = sizeof(stored);
    if (nvs_get_str(h, NVS_KEY_DEVICE_ID, stored, &len) != ESP_OK ||
        !is_valid_device_id(stored)) {
      stored[0] = '\0';
    } else if (is_legacy_placeholder_device_id(stored)) {
      stored[0] = '\0';
      migrate_placeholder = true;
    }
    nvs_close(h);
  }
  if (migrate_placeholder &&
      make_device_id_from_name(s_device_id, sizeof(s_device_id))) {
    ESP_LOGI(TAG, "Migrated legacy device_id from device name");
  } else {
    copy_device_id(s_device_id, sizeof(s_device_id), stored);
  }
  if (stored[0] == '\0') {
    (void)save_device_id_to_nvs();
    if (!migrate_placeholder) {
      ESP_LOGI(TAG, "Generated device_id from Wi-Fi MAC");
    }
  }
  ESP_LOGI(TAG, "device_id=%s", s_device_id);
}

/** Ensure s_voice_ws_url carries ?device_id=<id> (strip any prior query). */
static void ensure_voice_ws_url_has_device_id(void) {
  if (s_voice_ws_url[0] == '\0' || s_device_id[0] == '\0') {
    return;
  }
  char base[200];
  strncpy(base, s_voice_ws_url, sizeof(base) - 1);
  base[sizeof(base) - 1] = '\0';
  char *q = strchr(base, '?');
  if (q != NULL) {
    *q = '\0';
  }
  char *hash = strchr(base, '#');
  if (hash != NULL) {
    *hash = '\0';
  }
  if (base[0] == '\0') {
    return;
  }
  int n = snprintf(s_voice_ws_url, sizeof(s_voice_ws_url), "%s?device_id=%s",
                   base, s_device_id);
  if (n <= 0 || (size_t)n >= sizeof(s_voice_ws_url)) {
    ESP_LOGW(TAG, "voice WS URL with device_id too long");
  }
}

static bool json_escape_string(char *dst, size_t dst_size, const char *src) {
  if (dst == NULL || dst_size == 0 || src == NULL) {
    return false;
  }
  size_t out = 0;
  for (size_t i = 0; src[i] != '\0'; ++i) {
    const unsigned char c = (unsigned char)src[i];
    const char *escaped = NULL;
    switch (c) {
    case '"':
      escaped = "\\\"";
      break;
    case '\\':
      escaped = "\\\\";
      break;
    case '\b':
      escaped = "\\b";
      break;
    case '\f':
      escaped = "\\f";
      break;
    case '\n':
      escaped = "\\n";
      break;
    case '\r':
      escaped = "\\r";
      break;
    case '\t':
      escaped = "\\t";
      break;
    default:
      break;
    }
    if (escaped != NULL) {
      size_t escaped_len = strlen(escaped);
      if (out + escaped_len >= dst_size) {
        return false;
      }
      memcpy(dst + out, escaped, escaped_len);
      out += escaped_len;
    } else if (c < 0x20) {
      if (out + 6 >= dst_size) {
        return false;
      }
      int written = snprintf(dst + out, dst_size - out, "\\u%04x", c);
      if (written != 6) {
        return false;
      }
      out += 6;
    } else {
      if (out + 1 >= dst_size) {
        return false;
      }
      dst[out++] = (char)c;
    }
  }
  dst[out] = '\0';
  return true;
}

static bool voice_http_url_from_ws(char *dst, size_t dst_size, const char *path) {
  if (dst == NULL || dst_size == 0 || path == NULL || s_voice_ws_url[0] == '\0') {
    return false;
  }
  const char *separator = strstr(s_voice_ws_url, "://");
  if (separator == NULL ||
      (strncmp(s_voice_ws_url, "ws://", 5) != 0 &&
       strncmp(s_voice_ws_url, "wss://", 6) != 0)) {
    return false;
  }
  const char *authority = separator + 3;
  size_t authority_len = strcspn(authority, "/?#");
  if (authority_len == 0 || authority_len > 180) {
    return false;
  }
  const char *http_scheme =
      strncmp(s_voice_ws_url, "wss://", 6) == 0 ? "https" : "http";
  int written = snprintf(dst, dst_size, "%s://%.*s%s", http_scheme,
                         (int)authority_len, authority, path);
  return written > 0 && (size_t)written < dst_size;
}

static bool wifi_report_url_from_voice_ws(char *dst, size_t dst_size) {
  if (dst == NULL || dst_size == 0 || s_device_id[0] == '\0') {
    return false;
  }
  char path[96];
  int n = snprintf(path, sizeof(path), "/api/devices/%s/network", s_device_id);
  if (n <= 0 || (size_t)n >= sizeof(path)) {
    return false;
  }
  return voice_http_url_from_ws(dst, dst_size, path);
}

static void report_wifi_network_task(void *arg) {
  (void)arg;
  /* The server may need a moment to rediscover the board after DHCP changes. */
  vTaskDelay(pdMS_TO_TICKS(2000));

  wifi_ap_record_t ap = {};
  if (esp_wifi_sta_get_ap_info(&ap) != ESP_OK) {
    ESP_LOGW(TAG, "Wi-Fi network report skipped: AP information unavailable");
    vTaskDelete(NULL);
    return;
  }

  char report_url[256] = "";
  if (!wifi_report_url_from_voice_ws(report_url, sizeof(report_url))) {
    ESP_LOGD(TAG, "Wi-Fi network report skipped: voice server URL unavailable");
    vTaskDelete(NULL);
    return;
  }

  char raw_ssid[WIFI_CONFIG_STA_SSID_MAX + 1] = "";
  size_t raw_ssid_len =
      strnlen((const char *)ap.ssid, WIFI_CONFIG_STA_SSID_MAX);
  memcpy(raw_ssid, ap.ssid, raw_ssid_len);
  char ssid[WIFI_CONFIG_STA_SSID_MAX * 2 + 1] = "";
  if (!json_escape_string(ssid, sizeof(ssid), raw_ssid)) {
    ESP_LOGW(TAG, "Wi-Fi network report skipped: SSID could not be encoded");
    vTaskDelete(NULL);
    return;
  }
  char bssid[18] = "";
  snprintf(bssid, sizeof(bssid), MACSTR, MAC2STR(ap.bssid));
  char payload[192] = "";
  int payload_len = snprintf(
      payload, sizeof(payload),
      "{\"ssid\":\"%s\",\"bssid\":\"%s\",\"rssi\":%d,\"channel\":%u}",
      ssid, bssid, ap.rssi, (unsigned)ap.primary);
  if (payload_len <= 0 || (size_t)payload_len >= sizeof(payload)) {
    ESP_LOGW(TAG, "Wi-Fi network report skipped: payload too large");
    vTaskDelete(NULL);
    return;
  }

  for (int attempt = 1; attempt <= 3; ++attempt) {
    esp_http_client_config_t config = {
        .url = report_url,
        .method = HTTP_METHOD_POST,
        .timeout_ms = 5000,
    };
    esp_http_client_handle_t client = esp_http_client_init(&config);
    if (client == NULL) {
      ESP_LOGW(TAG, "Wi-Fi network report: HTTP client allocation failed");
      break;
    }
    esp_http_client_set_header(client, "Content-Type", "application/json");
    esp_http_client_set_post_field(client, payload, payload_len);
    esp_err_t err = esp_http_client_perform(client);
    int status = esp_http_client_get_status_code(client);
    esp_http_client_cleanup(client);
    if (err == ESP_OK && status >= 200 && status < 300) {
      ESP_LOGI(TAG, "Reported Wi-Fi network %s (%s) to server", ap.ssid, bssid);
      break;
    }
    ESP_LOGW(TAG, "Wi-Fi network report attempt %d failed: %s (HTTP %d)", attempt,
             esp_err_to_name(err), status);
    if (attempt < 3) {
      vTaskDelay(pdMS_TO_TICKS(3000));
    }
  }
  vTaskDelete(NULL);
}

static void schedule_wifi_network_report(void) {
  if (!s_sta_connected || s_voice_ws_url[0] == '\0') {
    return;
  }
  if (xTaskCreatePinnedToCore(report_wifi_network_task, "wifi_report", 4096, NULL,
                              5, NULL, APP_CORE_NET) != pdPASS) {
    ESP_LOGW(TAG, "Could not schedule Wi-Fi network report");
  }
}

static void show_server_wait_light(void) {
  nino_rgb_show_t cur = nino_rgb_led_current_show();
  if (cur == NINO_RGB_SHOW_LISTEN || cur == NINO_RGB_SHOW_ERROR ||
      cur == NINO_RGB_SHOW_BATTERY || cur == NINO_RGB_SHOW_MUTE ||
      cur == NINO_RGB_SHOW_OTA ||
      cur == NINO_RGB_SHOW_DONE) {
    return;
  }
  (void)nino_rgb_led_show(NINO_RGB_SHOW_SERVER_WAIT);
}

static void show_server_ok_light(void) {
  nino_rgb_show_t cur = nino_rgb_led_current_show();
  if (cur != NINO_RGB_SHOW_SERVER_WAIT && cur != NINO_RGB_SHOW_IDLE &&
      cur != NINO_RGB_SHOW_WIFI_OK) {
    return;
  }
  (void)nino_rgb_led_show(NINO_RGB_SHOW_SERVER_OK);
}

static void voice_server_probe_task(void *arg) {
  (void)arg;
  /* Keep pale green visible before cyan so the two phases do not smear. */
  vTaskDelay(pdMS_TO_TICKS(900));
  while (s_sta_connected && !s_server_link_ok) {
    if (s_voice_ws_url[0] == '\0') {
      vTaskDelay(pdMS_TO_TICKS(500));
      continue;
    }

    char url[256] = "";
    if (!voice_http_url_from_ws(url, sizeof(url), "/api/status")) {
      vTaskDelay(pdMS_TO_TICKS(1000));
      continue;
    }

    esp_http_client_config_t config = {
        .url = url,
        .method = HTTP_METHOD_GET,
        .timeout_ms = 3000,
    };
    esp_http_client_handle_t client = esp_http_client_init(&config);
    if (client == NULL) {
      ESP_LOGW(TAG, "Voice server probe: HTTP client allocation failed");
      vTaskDelay(pdMS_TO_TICKS(2000));
      continue;
    }
    esp_err_t err = esp_http_client_perform(client);
    int status = esp_http_client_get_status_code(client);
    esp_http_client_cleanup(client);
    if (err == ESP_OK && status >= 200 && status < 300) {
      s_server_link_ok = true;
      ESP_LOGI(TAG, "Voice server reachable: %s", url);
      show_server_ok_light();
      break;
    }
    ESP_LOGW(TAG, "Voice server probe failed: %s (HTTP %d)",
             esp_err_to_name(err), status);
    vTaskDelay(pdMS_TO_TICKS(2000));
  }
  s_server_probe_active = false;
  vTaskDelete(NULL);
}

static void schedule_voice_server_probe(void) {
  if (!s_sta_connected || s_server_link_ok || s_server_probe_active) {
    return;
  }
  s_server_probe_active = true;
  if (xTaskCreatePinnedToCore(voice_server_probe_task, "srv_probe", 4096, NULL, 5,
                              NULL, APP_CORE_NET) != pdPASS) {
    s_server_probe_active = false;
    ESP_LOGW(TAG, "Could not schedule voice server probe");
  }
}

static void begin_voice_server_link(void) {
  if (!s_sta_connected) {
    return;
  }
  s_server_link_ok = false;
  nino_rgb_show_t cur = nino_rgb_led_current_show();
  if (cur == NINO_RGB_SHOW_WIFI_WAIT || cur == NINO_RGB_SHOW_WIFI_OK ||
      cur == NINO_RGB_SHOW_WIFI_FAIL) {
    (void)nino_rgb_led_show(NINO_RGB_SHOW_IDLE);
  }
  show_server_wait_light();
  schedule_voice_server_probe();
}

static void sta_reconnect_task(void *arg) {
  (void)arg;
  vTaskDelay(pdMS_TO_TICKS(STA_RECONNECT_DELAY_MS));
  if (strlen(s_sta_ssid) > 0 &&
      (s_wifi_mode == WIFI_MODE_STA || s_wifi_mode == WIFI_MODE_APSTA)) {
    ESP_LOGI(TAG, "STA: retrying connect to %s", s_sta_ssid);
    esp_wifi_connect();
  }
  vTaskDelete(NULL);
}

static esp_err_t wifi_switch_mode(wifi_mode_t mode);

void wifi_config_get_ap_ip(char *buf, size_t buf_size) {
  esp_netif_t *ap_netif = esp_netif_get_handle_from_ifkey("WIFI_AP_DEF");
  if (ap_netif == NULL) {
    snprintf(buf, buf_size, "0.0.0.0");
    return;
  }
  esp_netif_ip_info_t ip_info = {};
  if (esp_netif_get_ip_info(ap_netif, &ip_info) != ESP_OK) {
    snprintf(buf, buf_size, "0.0.0.0");
    return;
  }
  ip4_addr_t addr;
  addr.addr = ip_info.ip.addr;
  snprintf(buf, buf_size, "%s", ip4addr_ntoa(&addr));
}

void wifi_config_get_sta_ip(char *buf, size_t buf_size) {
  esp_netif_t *sta_netif = esp_netif_get_handle_from_ifkey("WIFI_STA_DEF");
  if (sta_netif == NULL) {
    snprintf(buf, buf_size, "0.0.0.0");
    return;
  }
  esp_netif_ip_info_t ip_info = {};
  if (esp_netif_get_ip_info(sta_netif, &ip_info) != ESP_OK) {
    snprintf(buf, buf_size, "0.0.0.0");
    return;
  }
  ip4_addr_t addr;
  addr.addr = ip_info.ip.addr;
  snprintf(buf, buf_size, "%s", ip4addr_ntoa(&addr));
}

static void get_primary_ip_str(char *buf, size_t buf_size) {
  if (s_wifi_mode == WIFI_MODE_AP || s_wifi_mode == WIFI_MODE_APSTA) {
    wifi_config_get_ap_ip(buf, buf_size);
    if (strcmp(buf, "0.0.0.0") != 0)
      return;
  }
  if (s_wifi_mode == WIFI_MODE_STA || s_wifi_mode == WIFI_MODE_APSTA) {
    wifi_config_get_sta_ip(buf, buf_size);
  }
}

static const char *INDEX_HTML =
    "<!DOCTYPE html>"
    "<html><head><meta charset=\"utf-8\">"
    "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
    "<title>NiNO camera (ESP32-P4)</title>"
    "<style>"
    "body{font-family:system-ui,sans-serif;margin:0;background:#101820;color:#"
    "f5f7fa;}"
    "main{max-width:900px;margin:0 auto;padding:24px;}"
    "h1{margin:0 0 8px;font-size:2rem;}"
    "p{color:#b8c2cc;line-height:1.5;}"
    ".card{background:#17232e;border-radius:16px;padding:16px;box-shadow:0 "
    "16px 40px rgba(0,0,0,.25);}"
    "img{display:block;width:100%;max-width:640px;height:auto;border-radius:12px;"
    "background:#000;}"
    ".rotated{transform:rotate(" STR(HTTP_STREAM_ROTATE_DEG) "deg);"
    "transform-origin:center center;}"
    "code{background:#0d141b;padding:2px 6px;border-radius:6px;}"
    "</style></head>"
    "<body><main><h1>NiNO camera host</h1>"
    "<p>This board captures the USB camera and exposes it to your <strong>NiNO "
    "Camera Face Server</strong> on the PC. Use <code>http://localhost:8000</code> "
    "for live video, face recognition, and speech.</p>"
    "<p>Opening this page does <strong>not</strong> start a second MJPEG viewer, so "
    "it will not fight the PC app.</p>"
    "<p>One-frame check (loads once when you open or refresh this page):</p>"
    "<div class=\"card\"><img class=\"rotated\" src=\"/snapshot.jpg\" alt=\"last camera frame\"></div>"
    "<p>Machine endpoints: <code>/snapshot.jpg</code>, <code>/stream</code> (raw MJPEG), "
    "<code>/view</code> (rotated browser view), "
    "<code>/stream.mjpeg</code> (raw MJPEG alias), "
    "<code>/play_wav</code> (POST WAV).</p>"
    "</main></body></html>";

static const char *STREAM_VIEW_HTML =
    "<!DOCTYPE html>"
    "<html><head><meta charset=\"utf-8\">"
    "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
    "<title>NiNO live stream</title>"
    "<style>"
    "body{margin:0;background:#000;display:flex;align-items:center;justify-content:center;"
    "min-height:100vh;}"
    "img{max-width:100vw;max-height:100vh;"
    "transform:rotate(" STR(HTTP_STREAM_ROTATE_DEG) "deg);"
    "transform-origin:center center;}"
    "</style></head><body>"
    "<img src=\"/stream.mjpeg\" alt=\"camera stream\">"
    "</body></html>";

static int cmd_cpu_dump(int argc, char **argv) {
  (void)argc;
  (void)argv;

  UBaseType_t task_count = uxTaskGetNumberOfTasks();
  TaskStatus_t *task_list = calloc(task_count, sizeof(TaskStatus_t));
  uint32_t total_runtime = 0;

  if (task_list == NULL) {
    printf("cpu_dump: out of memory\n");
    return 1;
  }

  task_count = uxTaskGetSystemState(task_list, task_count, &total_runtime);
  if (task_count == 0) {
    free(task_list);
    printf("cpu_dump: no task data\n");
    return 1;
  }

  printf("Task                 Core Prio State Stack Runtime      CPU\n");
  printf("-------------------------------------------------------------\n");

  for (UBaseType_t i = 0; i < task_count; ++i) {
    const TaskStatus_t *task = &task_list[i];
    const char state = (task->eCurrentState == eRunning)     ? 'R'
                       : (task->eCurrentState == eReady)     ? 'Y'
                       : (task->eCurrentState == eBlocked)   ? 'B'
                       : (task->eCurrentState == eSuspended) ? 'S'
                       : (task->eCurrentState == eDeleted)   ? 'D'
                                                             : '?';
    unsigned long runtime = (unsigned long)task->ulRunTimeCounter;
    unsigned long pct =
        (total_runtime > 0U) ? (runtime * 100UL) / total_runtime : 0UL;

    printf("%-20s %4d %4u %5c %5u %10lu %4lu%%\n", task->pcTaskName,
           (int)task->xCoreID, (unsigned)task->uxCurrentPriority, state,
           (unsigned)task->usStackHighWaterMark, runtime, pct);
  }

  printf("Total runtime ticks: %lu\n", (unsigned long)total_runtime);
  free(task_list);
  return 0;
}

static int cmd_wifi_mode(int argc, char **argv) {
  if (argc < 2) {
    printf("Usage: wifi mode <ap|sta|both>\n");
    return 0;
  }
  wifi_mode_t mode;
  if (strcmp(argv[1], "ap") == 0) {
    mode = WIFI_MODE_AP;
  } else if (strcmp(argv[1], "sta") == 0) {
    mode = WIFI_MODE_STA;
  } else if (strcmp(argv[1], "both") == 0) {
    mode = WIFI_MODE_APSTA;
  } else {
    printf("Invalid mode. Use: ap, sta, or both\n");
    return 0;
  }
  esp_err_t err = wifi_switch_mode(mode);
  printf("%s\n", (err == ESP_OK) ? "OK" : "Failed");
  return 0;
}

static void wifi_save_to_nvs(wifi_mode_t mode) {
  nvs_handle_t h;
  if (nvs_open(NVS_NAMESPACE, NVS_READWRITE, &h) != ESP_OK)
    return;
  uint8_t m = (uint8_t)mode;
  nvs_set_u8(h, NVS_KEY_MODE, m);
  nvs_set_str(h, NVS_KEY_STA_SSID, s_sta_ssid);
  nvs_set_str(h, NVS_KEY_STA_PASS, s_sta_pass);
  nvs_set_str(h, NVS_KEY_DEVICE_NAME, s_device_name);
  nvs_commit(h);
  nvs_close(h);
  ESP_LOGI(TAG, "Saved Wi-Fi credentials to NVS (mode=%d, ssid=%s, pass_len=%u)",
           (int)mode, s_sta_ssid, (unsigned)strlen(s_sta_pass));
}

esp_err_t wifi_config_sta_connect(wifi_mode_t mode_to_save) {
  if (s_sta_ssid[0] == '\0') {
    return ESP_ERR_INVALID_ARG;
  }

  wifi_mode_t cur = WIFI_MODE_AP;
  if (esp_wifi_get_mode(&cur) != ESP_OK) {
    return ESP_FAIL;
  }

  esp_err_t err;
  if (cur != WIFI_MODE_STA && cur != WIFI_MODE_APSTA) {
    err = wifi_switch_mode(WIFI_MODE_APSTA);
  } else {
    wifi_config_t cfg = {};
    copy_cstr_field(cfg.sta.ssid, sizeof(cfg.sta.ssid), s_sta_ssid);
    copy_cstr_field(cfg.sta.password, sizeof(cfg.sta.password), s_sta_pass);
    err = esp_wifi_set_config(WIFI_IF_STA, &cfg);
    if (err == ESP_OK) {
      err = esp_wifi_connect();
    }
    s_wifi_mode = cur;
  }
  if (err != ESP_OK) {
    return err;
  }
  wifi_save_to_nvs(mode_to_save);
  return ESP_OK;
}

esp_err_t wifi_config_set_sta_credentials(const char *ssid, const char *pass) {
  if (ssid == NULL || ssid[0] == '\0') {
    return ESP_ERR_INVALID_ARG;
  }
  strncpy(s_sta_ssid, ssid, WIFI_CONFIG_STA_SSID_MAX - 1);
  s_sta_ssid[WIFI_CONFIG_STA_SSID_MAX - 1] = '\0';
  if (pass != NULL) {
    strncpy(s_sta_pass, pass, WIFI_CONFIG_STA_PASS_MAX - 1);
    s_sta_pass[WIFI_CONFIG_STA_PASS_MAX - 1] = '\0';
  } else {
    s_sta_pass[0] = '\0';
  }
  /* New credentials: allow the "unable to connect" prompt to play again if
   * this attempt also fails. */
  s_wifi_unable_chimed = false;
  /* Each new connection attempt should announce success only after it receives
   * an IP address. */
  s_wifi_connected_chime_pending = true;
  return ESP_OK;
}

bool wifi_config_sta_connected(void) { return s_sta_connected; }

bool wifi_config_is_provisioned(void) { return s_sta_ssid[0] != '\0'; }

esp_err_t wifi_config_enter_setup_mode(void) {
  ESP_LOGI(TAG, "Entering setup mode — erasing Wi-Fi credentials");

  memset(s_sta_ssid, 0, sizeof(s_sta_ssid));
  memset(s_sta_pass, 0, sizeof(s_sta_pass));
  s_sta_connected = false;
  s_server_link_ok = false;
  s_wifi_unable_chimed = false;
  s_wifi_connected_chime_pending = false;
  s_boot_unprovisioned = true;

  nvs_handle_t h;
  if (nvs_open(NVS_NAMESPACE, NVS_READWRITE, &h) == ESP_OK) {
    (void)nvs_erase_key(h, NVS_KEY_STA_SSID);
    (void)nvs_erase_key(h, NVS_KEY_STA_PASS);
    (void)nvs_set_str(h, NVS_KEY_STA_SSID, "");
    (void)nvs_set_str(h, NVS_KEY_STA_PASS, "");
    (void)nvs_set_u8(h, NVS_KEY_MODE, (uint8_t)WIFI_MODE_AP);
    (void)nvs_commit(h);
    nvs_close(h);
    ESP_LOGI(TAG, "NVS Wi-Fi STA credentials erased (namespace %s)",
             NVS_NAMESPACE);
  } else {
    ESP_LOGW(TAG, "Could not open NVS to erase Wi-Fi credentials");
  }

  (void)esp_wifi_disconnect();
  wifi_config_t empty_sta = {0};
  (void)esp_wifi_set_config(WIFI_IF_STA, &empty_sta);
  wifi_prov_ble_on_sta_ip_changed(false);

  esp_err_t err = wifi_switch_mode(WIFI_MODE_AP);
  if (err != ESP_OK) {
    ESP_LOGW(TAG, "Setup mode: Wi-Fi AP switch failed: %s",
             esp_err_to_name(err));
  }

  err = wifi_prov_ble_enable_provisioning();
  if (err != ESP_OK && err != ESP_ERR_NOT_SUPPORTED &&
      err != ESP_ERR_INVALID_STATE) {
    ESP_LOGW(TAG, "Setup mode: BLE provisioning enable failed: %s",
             esp_err_to_name(err));
    return err;
  }

  ESP_LOGI(TAG, "Setup mode active — BLE advertising for provisioning");
  return ESP_OK;
}

static int cmd_wifi_connect(int argc, char **argv) {
  if (argc < 2) {
    printf("Usage: wifi connect <ssid> [password]\n");
    return 0;
  }
  if (wifi_config_set_sta_credentials(argv[1], (argc >= 3) ? argv[2] : "") !=
      ESP_OK) {
    printf("Invalid SSID\n");
    return 0;
  }

  wifi_mode_t cur;
  if (esp_wifi_get_mode(&cur) != ESP_OK) {
    printf("Failed to get WiFi mode\n");
    return 0;
  }
  wifi_mode_t save = (cur == WIFI_MODE_STA || cur == WIFI_MODE_APSTA) ? cur
                                                                    : WIFI_MODE_APSTA;
  esp_err_t err = wifi_config_sta_connect(save);
  printf("%s\n", (err == ESP_OK) ? "Connecting..." : "Failed");
  if (err == ESP_OK) {
    printf("Connecting to %s...\n", s_sta_ssid);
  }
  return 0;
}

static int cmd_wifi_disconnect(int argc, char **argv) {
  (void)argc;
  (void)argv;
  esp_wifi_disconnect();
  printf("Disconnected\n");
  return 0;
}

static int cmd_wifi_status(int argc, char **argv) {
  (void)argc;
  (void)argv;
  wifi_mode_t mode;
  esp_wifi_get_mode(&mode);
  const char *mode_str = (mode == WIFI_MODE_AP)    ? "AP"
                         : (mode == WIFI_MODE_STA) ? "STA"
                                                   : "AP+STA";
  printf("Mode: %s\n", mode_str);

  char ap_ip[16], sta_ip[16];
  wifi_config_get_ap_ip(ap_ip, sizeof(ap_ip));
  wifi_config_get_sta_ip(sta_ip, sizeof(sta_ip));

  if (strcmp(ap_ip, "0.0.0.0") != 0) {
    printf("AP IP: %s\n", ap_ip);
  }
  if (mode != WIFI_MODE_AP) {
    printf("STA: %s\n", s_sta_connected ? "connected" : "disconnected");
    if (strcmp(sta_ip, "0.0.0.0") != 0) {
      printf("STA IP: %s\n", sta_ip);
    }
    if (strlen(s_sta_ssid) > 0) {
      printf("STA SSID: %s\n", s_sta_ssid);
    }
  }
  return 0;
}

static int cmd_wifi(int argc, char **argv) {
  if (argc >= 2 && strcmp(argv[1], "mode") == 0) {
    return cmd_wifi_mode(argc - 1, argv + 1);
  }
  if (argc >= 2 && strcmp(argv[1], "connect") == 0) {
    return cmd_wifi_connect(argc - 1, argv + 1);
  }
  if (argc >= 2 && strcmp(argv[1], "disconnect") == 0) {
    return cmd_wifi_disconnect(0, NULL);
  }
  if (argc >= 2 && strcmp(argv[1], "status") == 0) {
    return cmd_wifi_status(0, NULL);
  }
  printf("Usage: wifi mode <ap|sta|both> | wifi connect <ssid> [pass] | wifi "
         "disconnect | wifi status\n");
  return 0;
}

static void wifi_cli_register(void) {
  const esp_console_cmd_t wifi_cmd = {
      .command = "wifi",
      .help = "wifi mode <ap|sta|both> | wifi connect <ssid> [pass] | wifi "
              "disconnect | wifi status",
      .hint = NULL,
      .func = &cmd_wifi,
      .argtable = NULL,
  };
  ESP_ERROR_CHECK(esp_console_cmd_register(&wifi_cmd));
}

static void voice_cli_register(void);
static void start_cli_register(void);
static void device_cli_register(void);
static void servo_cli_register(void);
static void track_cli_register(void);
static void speaker_cli_register(void);
static void hstop_cli_register(void);
static void dinner_cli_register(void);
static void bday_cli_register(void);

static int cmd_eye(int argc, char **argv) {
  if (argc >= 2) {
    /* Join argv[1..] so "eye jai bhalaiah" / "eye big smile" work. */
    char line[48];
    size_t len = 0;
    for (int i = 1; i < argc && len < sizeof(line) - 1; i++) {
      if (i > 1 && len < sizeof(line) - 1) {
        line[len++] = ' ';
      }
      for (const char *p = argv[i]; *p && len < sizeof(line) - 1; p++) {
        line[len++] = *p;
      }
    }
    line[len] = '\0';
    if (nino_eye_apply_command(line)) {
      printf("eye -> state %d\n", (int)nino_eye_get_state());
      return 0;
    }
  }
  printf("Usage: eye <name>  e.g. idle happy mad med fire smile sparkle pencil\n"
         "              radio tv bulb robot bigsmile  (current: %d)\n",
         (int)nino_eye_get_state());
  return 0;
}

static void eye_cli_register(void) {
  const esp_console_cmd_t eye_cmd = {
      .command = "eye",
      .help = "Set NINO eye state by name (idle/happy/.../fire/smile/sparkle/.../bigsmile)",
      .hint = NULL,
      .func = &cmd_eye,
      .argtable = NULL,
  };
  ESP_ERROR_CHECK(esp_console_cmd_register(&eye_cmd));
}

static void console_init(void) {
  esp_console_repl_config_t repl_config = ESP_CONSOLE_REPL_CONFIG_DEFAULT();
  repl_config.prompt = "usb_cam> ";

  esp_console_register_help_command();
  wifi_cli_register();
  start_cli_register();
  voice_cli_register();
  device_cli_register();
  servo_cli_register();
  track_cli_register();
  speaker_cli_register();
  eye_cli_register();
  nino_rgb_led_cli_register();
  nino_battery_adc_cli_register();
  nino_battery_endurance_cli_register();
  hstop_cli_register();
  dinner_cli_register();
  bday_cli_register();

  const esp_console_cmd_t cpu_dump_cmd = {
      .command = "cpu_dump",
      .help = "Show current FreeRTOS runtime CPU stats",
      .hint = NULL,
      .func = &cmd_cpu_dump,
      .argtable = NULL,
  };
  ESP_ERROR_CHECK(esp_console_cmd_register(&cpu_dump_cmd));

#if defined(CONFIG_ESP_CONSOLE_UART_DEFAULT) ||                                \
    defined(CONFIG_ESP_CONSOLE_UART_CUSTOM)
  esp_console_dev_uart_config_t uart_config =
      ESP_CONSOLE_DEV_UART_CONFIG_DEFAULT();
  ESP_ERROR_CHECK(
      esp_console_new_repl_uart(&uart_config, &repl_config, &s_repl));
#elif defined(CONFIG_ESP_CONSOLE_USB_SERIAL_JTAG)
  esp_console_dev_usb_serial_jtag_config_t usbjtag_config =
      ESP_CONSOLE_DEV_USB_SERIAL_JTAG_CONFIG_DEFAULT();
  ESP_ERROR_CHECK(esp_console_new_repl_usb_serial_jtag(&usbjtag_config,
                                                       &repl_config, &s_repl));
#else
  esp_console_dev_uart_config_t uart_config =
      ESP_CONSOLE_DEV_UART_CONFIG_DEFAULT();
  ESP_ERROR_CHECK(
      esp_console_new_repl_uart(&uart_config, &repl_config, &s_repl));
#endif

  ESP_ERROR_CHECK(esp_console_start_repl(s_repl));
}

static const char *format_to_str(enum uvc_host_stream_format format) {
  switch (format) {
  case UVC_VS_FORMAT_MJPEG:
    return "MJPEG";
  case UVC_VS_FORMAT_YUY2:
    return "YUY2";
  case UVC_VS_FORMAT_H264:
    return "H264";
  case UVC_VS_FORMAT_H265:
    return "H265";
  case UVC_VS_FORMAT_DEFAULT:
  default:
    return "DEFAULT";
  }
}

static float frame_interval_to_fps(uint32_t frame_interval) {
  return (frame_interval != 0U) ? (10000000.0f / (float)frame_interval) : 0.0f;
}

static void free_frame_info_list(void) {
  free(s_frame_info_list);
  s_frame_info_list = NULL;
  s_frame_info_count = 0;
}

static esp_err_t latest_frame_reserve(size_t required) {
  if (required <= s_latest_frame.capacity) {
    return ESP_OK;
  }

  uint8_t *new_buf = heap_caps_realloc(s_latest_frame.data, required,
                                       MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
  if (new_buf == NULL) {
    new_buf = realloc(s_latest_frame.data, required);
  }
  if (new_buf == NULL) {
    return ESP_ERR_NO_MEM;
  }

  s_latest_frame.data = new_buf;
  s_latest_frame.capacity = required;
  return ESP_OK;
}

static void latest_frame_store(const uvc_host_frame_t *frame) {
  if (frame == NULL || frame->data == NULL || frame->data_len == 0) {
    return;
  }

  if (xSemaphoreTake(s_frame_mutex, portMAX_DELAY) != pdTRUE) {
    return;
  }

  if (latest_frame_reserve(frame->data_len) != ESP_OK) {
    xSemaphoreGive(s_frame_mutex);
    ESP_LOGE(TAG, "Failed to grow latest frame buffer to %u bytes",
             (unsigned)frame->data_len);
    return;
  }

  memcpy(s_latest_frame.data, frame->data, frame->data_len);
  s_latest_frame.len = frame->data_len;
  s_latest_frame.width = frame->vs_format.h_res;
  s_latest_frame.height = frame->vs_format.v_res;
  s_latest_frame.format = frame->vs_format;
  s_latest_frame.sequence++;
  s_latest_frame.ready = true;

  xSemaphoreGive(s_frame_mutex);

  if (s_face_track_task_handle != NULL) {
    xTaskNotifyGive(s_face_track_task_handle);
  }
}

static bool latest_frame_copy(uint8_t *dst, size_t dst_capacity,
                              size_t *out_len, uint32_t *out_sequence) {
  bool ok = false;

  if (xSemaphoreTake(s_frame_mutex, portMAX_DELAY) != pdTRUE) {
    return false;
  }

  if (s_latest_frame.ready &&
      s_latest_frame.format.format == UVC_VS_FORMAT_MJPEG &&
      s_latest_frame.len <= dst_capacity) {
    memcpy(dst, s_latest_frame.data, s_latest_frame.len);
    *out_len = s_latest_frame.len;
    *out_sequence = s_latest_frame.sequence;
    ok = true;
  }

  xSemaphoreGive(s_frame_mutex);
  return ok;
}

bool nino_uvc_camera_connected(void) {
  return s_device_connected;
}

uint32_t nino_uvc_frame_sequence(void) {
  return s_latest_frame.sequence;
}

static void face_track_task(void *arg) {
  (void)arg;

  uint8_t *jpeg_buf = heap_caps_malloc(UVC_FRAME_SIZE_BYTES,
                                       MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
  if (jpeg_buf == NULL) {
    ESP_LOGE(TAG, "Face tracking buffer allocation failed");
    s_face_track_task_handle = NULL;
    vTaskDelete(NULL);
    return;
  }

  esp_err_t detector_err = nino_face_detect_init();
  if (detector_err != ESP_OK) {
    ESP_LOGE(TAG, "Face detector init failed: %s",
             esp_err_to_name(detector_err));
    nino_face_tracker_set_detector_ready(false);
  } else {
    nino_face_tracker_set_detector_ready(true);
  }

  nino_face_detect_result_t last_face = {};
  bool have_last_face = false;
  uint32_t last_processed_sequence = 0;
  int64_t last_inference_us = 0;
  int64_t last_face_seen_us = 0;

  while (true) {
    (void)ulTaskNotifyTake(pdTRUE, pdMS_TO_TICKS(FACE_TRACK_NOTIFY_WAIT_MS));

    const int64_t now_us = esp_timer_get_time();

    if (!nino_face_tracker_is_enabled() || !nino_face_detect_is_ready()) {
      continue;
    }

    if (last_inference_us != 0 &&
        (now_us - last_inference_us) <
            (int64_t)FACE_TRACK_INFERENCE_INTERVAL_MS * 1000LL) {
      continue;
    }

    size_t frame_len = 0;
    uint32_t frame_sequence = 0;
    bool have_frame = latest_frame_copy(jpeg_buf, UVC_FRAME_SIZE_BYTES, &frame_len,
                                        &frame_sequence);

    if (have_frame && frame_sequence != last_processed_sequence) {
      nino_face_detect_result_t face = {};
      if (nino_face_detect_process(jpeg_buf, frame_len, &face) == ESP_OK) {
        last_processed_sequence = frame_sequence;
        last_inference_us = now_us;
        if (face.found) {
          last_face = face;
          have_last_face = true;
          last_face_seen_us = now_us;
        }
        nino_face_tracker_update(face.found, face.cx, face.cy, face.frame_w,
                                 face.frame_h, frame_sequence);
      }
      continue;
    }

    if (have_last_face &&
        (now_us - last_face_seen_us) <= (int64_t)FACE_TRACK_REUSE_LAST_FACE_MS * 1000LL) {
      nino_face_tracker_update(last_face.found, last_face.cx, last_face.cy,
                               last_face.frame_w, last_face.frame_h,
                               last_processed_sequence);
      last_inference_us = now_us;
    }
  }
}

static void wifi_event_handler(void *arg, esp_event_base_t event_base,
                               int32_t event_id, void *event_data) {
  if (event_base == WIFI_EVENT && event_id == WIFI_EVENT_AP_STACONNECTED) {
    wifi_event_ap_staconnected_t *event =
        (wifi_event_ap_staconnected_t *)event_data;
    ESP_LOGI(TAG, "AP: Device Connected AID: %d", event->aid);
  }

  if (event_base == WIFI_EVENT && event_id == WIFI_EVENT_AP_STADISCONNECTED) {
    wifi_event_ap_stadisconnected_t *event =
        (wifi_event_ap_stadisconnected_t *)event_data;
    ESP_LOGI(TAG, "AP: Device Disconnected AID: %d", event->aid);
  }

  if (event_base == WIFI_EVENT && event_id == WIFI_EVENT_STA_CONNECTED) {
    ESP_LOGI(TAG, "STA: Connected to AP");
  }

  if (event_base == WIFI_EVENT && event_id == WIFI_EVENT_STA_DISCONNECTED) {
    s_sta_connected = false;
    s_server_link_ok = false;
    s_wifi_connected_chime_pending = true;
    wifi_prov_ble_on_sta_ip_changed(false);
    mdns_stop_service();
    (void)nino_rgb_led_show(NINO_RGB_SHOW_WIFI_FAIL);
    wifi_event_sta_disconnected_t *ev =
        (wifi_event_sta_disconnected_t *)event_data;
    ESP_LOGW(TAG, "STA: Disconnected (reason %d)", ev->reason);
    if (strlen(s_sta_ssid) > 0 &&
        wifi_disconnect_is_connect_failure(ev->reason) &&
        !s_wifi_unable_chimed) {
      if (play_wifi_unable_clip()) {
        s_wifi_unable_chimed = true;
      }
    }
    if (strlen(s_sta_ssid) > 0 &&
        (s_wifi_mode == WIFI_MODE_STA || s_wifi_mode == WIFI_MODE_APSTA)) {
      xTaskCreatePinnedToCore(sta_reconnect_task, "sta_reconn", 2048, NULL, 5,
                              NULL, APP_CORE_NET);
    }
  }

  if (event_base == IP_EVENT && event_id == IP_EVENT_STA_GOT_IP) {
    ip_event_got_ip_t *event = (ip_event_got_ip_t *)event_data;
    s_sta_connected = true;
    s_wifi_unable_chimed = false;
    begin_voice_server_link();
    ESP_LOGI(TAG, "STA: Got IP " IPSTR, IP2STR(&event->ip_info.ip));
    mdns_start_service();
    wifi_prov_ble_on_sta_ip_changed(true);
    schedule_wifi_network_report();
    schedule_wifi_connected_chime();
    schedule_provisioned_welcome();
  }
}

static wifi_mode_t wifi_load_from_nvs(void) {
  nvs_handle_t h;
  if (nvs_open(NVS_NAMESPACE, NVS_READONLY, &h) != ESP_OK) {
    copy_device_name(s_device_name, sizeof(s_device_name), DEVICE_NAME_DEFAULT);
    wifi_prov_ble_set_device_name(s_device_name);
    return WIFI_MODE_AP;
  }
  uint8_t m = WIFI_MODE_AP;
  esp_err_t err = nvs_get_u8(h, NVS_KEY_MODE, &m);
  if (err == ESP_OK && m >= WIFI_MODE_STA && m <= WIFI_MODE_APSTA) {
    s_wifi_mode = (wifi_mode_t)m;
  }
  size_t len = WIFI_CONFIG_STA_SSID_MAX;
  if (nvs_get_str(h, NVS_KEY_STA_SSID, s_sta_ssid, &len) != ESP_OK) {
    s_sta_ssid[0] = '\0';
  }
  len = WIFI_CONFIG_STA_PASS_MAX;
  if (nvs_get_str(h, NVS_KEY_STA_PASS, s_sta_pass, &len) != ESP_OK) {
    s_sta_pass[0] = '\0';
  }
  len = sizeof(s_device_name);
  if (nvs_get_str(h, NVS_KEY_DEVICE_NAME, s_device_name, &len) != ESP_OK ||
      !is_valid_device_name(s_device_name)) {
    copy_device_name(s_device_name, sizeof(s_device_name), DEVICE_NAME_DEFAULT);
  }
  nvs_close(h);
  wifi_prov_ble_set_device_name(s_device_name);
  if (s_sta_ssid[0] == '\0' && s_wifi_mode != WIFI_MODE_AP) {
    s_wifi_mode = WIFI_MODE_AP;
  }
  return s_wifi_mode;
}

static esp_err_t wifi_switch_mode(wifi_mode_t mode) {
  esp_err_t err = esp_wifi_stop();
  if (err != ESP_OK && err != ESP_ERR_WIFI_NOT_STARTED)
    return err;

  s_wifi_mode = mode;
  err = esp_wifi_set_mode(mode);
  if (err != ESP_OK)
    return err;

  wifi_config_t wifi_config = {0};

  if (mode == WIFI_MODE_AP || mode == WIFI_MODE_APSTA) {
    copy_cstr_field(wifi_config.ap.ssid, sizeof(wifi_config.ap.ssid),
                    WIFI_CONFIG_AP_SSID);
    copy_cstr_field(wifi_config.ap.password, sizeof(wifi_config.ap.password),
                    WIFI_CONFIG_AP_PASS);
    wifi_config.ap.ssid_len = strlen(WIFI_CONFIG_AP_SSID);
    wifi_config.ap.max_connection = MAX_STA_CONN;
    wifi_config.ap.authmode =
        (strlen(WIFI_CONFIG_AP_PASS) == 0) ? WIFI_AUTH_OPEN : WIFI_AUTH_WPA2_PSK;
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_AP, &wifi_config));
  }

  if (mode == WIFI_MODE_STA || mode == WIFI_MODE_APSTA) {
    memset(&wifi_config, 0, sizeof(wifi_config));
    copy_cstr_field(wifi_config.sta.ssid, sizeof(wifi_config.sta.ssid),
                    s_sta_ssid);
    copy_cstr_field(wifi_config.sta.password, sizeof(wifi_config.sta.password),
                    s_sta_pass);
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &wifi_config));
  }

  err = esp_wifi_start();
  if (err != ESP_OK)
    return err;

  if ((mode == WIFI_MODE_STA || mode == WIFI_MODE_APSTA) &&
      strlen(s_sta_ssid) > 0) {
    (void)nino_rgb_led_show(NINO_RGB_SHOW_WIFI_WAIT);
    esp_wifi_connect();
  }

  wifi_save_to_nvs(mode);
  const char *mode_str = (mode == WIFI_MODE_AP)    ? "AP"
                         : (mode == WIFI_MODE_STA) ? "STA"
                                                   : "AP+STA";
  ESP_LOGI(TAG, "WiFi mode switched to %s", mode_str);
  return ESP_OK;
}

static esp_err_t wifi_init_all(void) {
  esp_err_t err = esp_netif_init();
  if (err != ESP_OK) {
    return err;
  }
  err = esp_event_loop_create_default();
  if (err != ESP_OK && err != ESP_ERR_INVALID_STATE) {
    return err;
  }
  esp_netif_create_default_wifi_ap();
  esp_netif_create_default_wifi_sta();

  wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
  err = esp_wifi_init(&cfg);
  if (err != ESP_OK) {
    ESP_LOGE(TAG, "esp_wifi_init failed: %s (check ESP-Hosted SDIO / C6 link)",
             esp_err_to_name(err));
    return err;
  }

  s_wifi_connected_chime_pending = true;

  ESP_ERROR_CHECK(esp_event_handler_instance_register(
      WIFI_EVENT, ESP_EVENT_ANY_ID, &wifi_event_handler, NULL, NULL));
  ESP_ERROR_CHECK(esp_event_handler_instance_register(
      IP_EVENT, IP_EVENT_STA_GOT_IP, &wifi_event_handler, NULL, NULL));

  wifi_mode_t saved_mode = wifi_load_from_nvs();
  return wifi_switch_mode(saved_mode);
}

/* Start BLE after wifi_init_all() has brought Hosted SDIO up. */
static void wifi_provisioning_task(void *arg) {
  (void)arg;
  /* Brief settle so SDIO TX path is stable before NimBLE/HCI traffic. */
  vTaskDelay(pdMS_TO_TICKS(200));
  esp_err_t err = wifi_prov_ble_start_if_needed();
  if (err != ESP_OK && err != ESP_ERR_NOT_SUPPORTED &&
      err != ESP_ERR_INVALID_STATE) {
    ESP_LOGW(TAG, "BLE Wi-Fi provisioning not started: %s",
             esp_err_to_name(err));
  }
  vTaskDelete(NULL);
}

static bool is_discovery_request(const char *msg, size_t len) {
  if (len < strlen(DISCOVERY_MSG))
    return false;
  if (len >= DISCOVERY_BUF)
    return false;
  return (strncmp(msg, DISCOVERY_MSG, strlen(DISCOVERY_MSG)) == 0);
}

static void apply_voice_ws_url_from_server(const char *uri);

/** Pair voice WS to the PC that just discovered us (no manual voice connect). */
static void auto_pair_voice_from_discovery_peer(const struct sockaddr_in *peer) {
  if (peer == NULL || peer->sin_addr.s_addr == 0 ||
      peer->sin_addr.s_addr == htonl(INADDR_NONE)) {
    return;
  }

  char ip[16];
  if (inet_ntoa_r(peer->sin_addr, ip, sizeof(ip)) == NULL) {
    return;
  }

  char uri[160];
  int n = snprintf(uri, sizeof(uri), "ws://%s:%d/voice-query", ip,
                   DEFAULT_VOICE_SERVER_PORT);
  if (n <= 0 || (size_t)n >= sizeof(uri)) {
    return;
  }

  /* Skip NVS rewrite when already paired to this host:port. */
  if (strncmp(s_voice_ws_url, uri, (size_t)n) == 0 &&
      (s_voice_ws_url[n] == '\0' || s_voice_ws_url[n] == '?')) {
    return;
  }

  ESP_LOGI(TAG, "Discovery: auto-pairing voice to %s", uri);
  apply_voice_ws_url_from_server(uri);
}

static void multicast_discovery_task(void *arg) {
  (void)arg;
  vTaskDelay(pdMS_TO_TICKS(2000));

  int sock = -1;
  char primary_ip[16] = "0.0.0.0";
  struct timeval tv = {.tv_sec = 1, .tv_usec = 0};

  while (1) {
    get_primary_ip_str(primary_ip, sizeof(primary_ip));
    if (strcmp(primary_ip, "0.0.0.0") == 0) {
      vTaskDelay(pdMS_TO_TICKS(500));
      continue;
    }

    if (sock >= 0)
      close(sock);
    sock = socket(PF_INET, SOCK_DGRAM, IPPROTO_IP);
    if (sock < 0) {
      vTaskDelay(pdMS_TO_TICKS(2000));
      continue;
    }

    int opt = 1;
    setsockopt(sock, SOL_SOCKET, SO_REUSEADDR, &opt, sizeof(opt));
    setsockopt(sock, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));

    struct sockaddr_in saddr = {};
    saddr.sin_family = AF_INET;
    saddr.sin_port = htons(DISCOVERY_PORT);
    saddr.sin_addr.s_addr = htonl(INADDR_ANY);

    if (bind(sock, (struct sockaddr *)&saddr, sizeof(saddr)) < 0) {
      close(sock);
      sock = -1;
      vTaskDelay(pdMS_TO_TICKS(2000));
      continue;
    }

    struct ip_mreq imreq = {};
    inet_aton(MULTICAST_ADDR, &imreq.imr_multiaddr);
    inet_aton(primary_ip, &imreq.imr_interface);

    if (setsockopt(sock, IPPROTO_IP, IP_ADD_MEMBERSHIP, &imreq,
                   sizeof(imreq)) == 0) {
      ESP_LOGI(TAG, "Discovery: listening on %s:%d (if %s)", MULTICAST_ADDR,
               DISCOVERY_PORT, primary_ip);
    } else {
      int broadcast_enable = 1;
      if (setsockopt(sock, SOL_SOCKET, SO_BROADCAST, &broadcast_enable,
                     sizeof(broadcast_enable)) < 0) {
        close(sock);
        sock = -1;
        vTaskDelay(pdMS_TO_TICKS(2000));
        continue;
      }
      ESP_LOGW(TAG, "Discovery: multicast fail, using broadcast on %s:%d",
               BROADCAST_ADDR, DISCOVERY_PORT);
    }

    char recvbuf[DISCOVERY_BUF];
    struct sockaddr_in raddr;
    socklen_t raddr_len = sizeof(raddr);

    while (1) {
      int len = recvfrom(sock, recvbuf, sizeof(recvbuf) - 1, 0,
                         (struct sockaddr *)&raddr, &raddr_len);
      if (len > 0 && len < (int)sizeof(recvbuf)) {
        recvbuf[len] = '\0';
        if (is_discovery_request(recvbuf, (size_t)len)) {
          char ap_ip[16], sta_ip[16];
          wifi_config_get_ap_ip(ap_ip, sizeof(ap_ip));
          wifi_config_get_sta_ip(sta_ip, sizeof(sta_ip));

          uint8_t mac[6];
          esp_wifi_get_mac(WIFI_IF_AP, mac);
          char mac_str[18];
          snprintf(mac_str, sizeof(mac_str), "%02X:%02X:%02X:%02X:%02X:%02X",
                   mac[0], mac[1], mac[2], mac[3], mac[4], mac[5]);

          const char *device_name = s_device_name;
          char response[DISCOVERY_BUF];
          int rlen;

          /* Include device_id so PC discovery can upsert without guessing. */
          if (strcmp(ap_ip, "0.0.0.0") != 0 && strcmp(sta_ip, "0.0.0.0") != 0) {
            rlen = snprintf(response, sizeof(response),
                            "hi\nmac=%s\nname=%s\ndevice_id=%s\n%s:%d\n%s:%d",
                            mac_str, device_name, s_device_id, ap_ip,
                            MESSAGE_PORT, sta_ip, MESSAGE_PORT);
          } else if (strcmp(ap_ip, "0.0.0.0") != 0) {
            rlen = snprintf(response, sizeof(response),
                            "hi\nmac=%s\nname=%s\ndevice_id=%s\n%s:%d", mac_str,
                            device_name, s_device_id, ap_ip, MESSAGE_PORT);
          } else {
            rlen = snprintf(response, sizeof(response),
                            "hi\nmac=%s\nname=%s\ndevice_id=%s\n%s:%d", mac_str,
                            device_name, s_device_id, sta_ip, MESSAGE_PORT);
          }
          if (rlen > 0 && rlen < (int)sizeof(response)) {
            sendto(sock, response, (size_t)rlen, 0, (struct sockaddr *)&raddr,
                   raddr_len);
            ESP_LOGI(TAG, "Discovery: responded to %s",
                     inet_ntoa(raddr.sin_addr));
            auto_pair_voice_from_discovery_peer(&raddr);
          }
        }
      }
      raddr_len = sizeof(raddr);

      char new_primary[16];
      get_primary_ip_str(new_primary, sizeof(new_primary));
      if (strcmp(new_primary, primary_ip) != 0)
        break;
    }
  }
}

static void tcp_message_server_task(void *arg) {
  (void)arg;
  vTaskDelay(pdMS_TO_TICKS(1000));

  int listen_sock = socket(AF_INET, SOCK_STREAM, 0);
  if (listen_sock < 0) {
    return;
  }

  int opt = 1;
  setsockopt(listen_sock, SOL_SOCKET, SO_REUSEADDR, &opt, sizeof(opt));

  struct sockaddr_in saddr = {};
  saddr.sin_family = AF_INET;
  saddr.sin_port = htons(MESSAGE_PORT);
  saddr.sin_addr.s_addr = htonl(INADDR_ANY);

  if (bind(listen_sock, (struct sockaddr *)&saddr, sizeof(saddr)) < 0) {
    close(listen_sock);
    return;
  }
  if (listen(listen_sock, 5) < 0) {
    close(listen_sock);
    return;
  }

  ESP_LOGI(TAG, "TCP message server: listening on port %d", MESSAGE_PORT);

  while (1) {
    struct sockaddr_in client_addr;
    socklen_t client_len = sizeof(client_addr);
    int client_sock =
        accept(listen_sock, (struct sockaddr *)&client_addr, &client_len);
    if (client_sock < 0) {
      continue;
    }
    ESP_LOGI(TAG, "TCP server: client connected");

    char buf[MESSAGE_BUF];
    int n;
    while ((n = recv(client_sock, buf, sizeof(buf) - 1, 0)) > 0) {
      if (n < (int)sizeof(buf)) {
        buf[n] = '\0';
        ESP_LOGI(TAG, "Message received: %s", buf);
      }
    }
    close(client_sock);
  }
}

static esp_err_t index_handler(httpd_req_t *req) {
  httpd_resp_set_type(req, "text/html; charset=utf-8");
  httpd_resp_set_hdr(req, "Cache-Control", "no-store");
  return httpd_resp_send(req, INDEX_HTML, HTTPD_RESP_USE_STRLEN);
}

static esp_err_t snapshot_handler(httpd_req_t *req) {
  uint8_t *buffer = heap_caps_malloc(UVC_FRAME_SIZE_BYTES,
                                     MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
  size_t frame_len = 0;
  uint32_t frame_seq = 0;
  esp_err_t err;

  if (buffer == NULL) {
    return httpd_resp_send_500(req);
  }

  if (!latest_frame_copy(buffer, UVC_FRAME_SIZE_BYTES, &frame_len,
                         &frame_seq)) {
    free(buffer);
    httpd_resp_set_status(req, "503 Service Unavailable");
    return httpd_resp_sendstr(req, "No MJPEG frame available");
  }

  (void)frame_seq;
  httpd_resp_set_type(req, "image/jpeg");
  httpd_resp_set_hdr(req, "Cache-Control", "no-store");
  err = httpd_resp_send(req, (const char *)buffer, frame_len);
  free(buffer);
  return err;
}

static esp_err_t stream_handler(httpd_req_t *req) {
  static const char *stream_type =
      "multipart/x-mixed-replace;boundary=" HTTP_STREAM_BOUNDARY;
  uint8_t *buffer = heap_caps_malloc(UVC_FRAME_SIZE_BYTES,
                                     MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
  uint32_t last_sequence = 0;
  esp_err_t err = ESP_OK;

  if (buffer == NULL) {
    return httpd_resp_send_500(req);
  }

  httpd_resp_set_type(req, stream_type);
  httpd_resp_set_hdr(req, "Cache-Control", "no-store");
  httpd_resp_set_hdr(req, "Pragma", "no-cache");
  httpd_resp_set_hdr(req, "Access-Control-Allow-Origin", "*");

  while (true) {
    size_t frame_len = 0;
    uint32_t frame_sequence = 0;

    if (!latest_frame_copy(buffer, UVC_FRAME_SIZE_BYTES, &frame_len,
                           &frame_sequence) ||
        frame_sequence == last_sequence) {
      vTaskDelay(pdMS_TO_TICKS(HTTP_STREAM_POLL_MS));
      continue;
    }

    last_sequence = frame_sequence;

    char part_header[96];
    int header_len = snprintf(part_header, sizeof(part_header),
                              "--" HTTP_STREAM_BOUNDARY "\r\n"
                              "Content-Type: image/jpeg\r\n"
                              "Content-Length: %u\r\n\r\n",
                              (unsigned)frame_len);
    if (header_len <= 0 || header_len >= (int)sizeof(part_header)) {
      err = ESP_FAIL;
      break;
    }

    err = httpd_resp_send_chunk(req, part_header, header_len);
    if (err != ESP_OK) {
      break;
    }
    err = httpd_resp_send_chunk(req, (const char *)buffer, frame_len);
    if (err != ESP_OK) {
      break;
    }
    err = httpd_resp_send_chunk(req, "\r\n", 2);
    if (err != ESP_OK) {
      break;
    }
  }

  free(buffer);
  return err;
}

static esp_err_t stream_route_handler(httpd_req_t *req) {
  httpd_resp_set_type(req, "text/html; charset=utf-8");
  httpd_resp_set_hdr(req, "Cache-Control", "no-store");
  return httpd_resp_send(req, STREAM_VIEW_HTML, HTTPD_RESP_USE_STRLEN);
}

static esp_err_t play_wav_handler(httpd_req_t *req) {
  if (req->method != HTTP_POST) {
    httpd_resp_set_status(req, "405 Method Not Allowed");
    httpd_resp_set_type(req, "application/json");
    return httpd_resp_send(req, "{\"ok\":false,\"error\":\"POST only\"}",
                            HTTPD_RESP_USE_STRLEN);
  }

  size_t total = req->content_len;
  if (total == 0 || total > MAX_PLAY_WAV_BYTES) {
    httpd_resp_set_status(req, "413 Payload Too Large");
    httpd_resp_set_type(req, "application/json");
    return httpd_resp_send(
        req, "{\"ok\":false,\"error\":\"invalid Content-Length\"}",
        HTTPD_RESP_USE_STRLEN);
  }

  uint8_t *buf =
      heap_caps_malloc(total, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
  if (buf == NULL) {
    buf = malloc(total);
  }
  if (buf == NULL) {
    return httpd_resp_send_500(req);
  }

  int received = 0;
  while (received < (int)total) {
    int r = httpd_req_recv(req, (char *)buf + received,
                           (int)total - received);
    if (r <= 0) {
      free(buf);
      httpd_resp_set_status(req, "400 Bad Request");
      httpd_resp_set_type(req, "application/json");
      return httpd_resp_send(req, "{\"ok\":false,\"error\":\"recv\"}",
                             HTTPD_RESP_USE_STRLEN);
    }
    received += r;
  }

  bool prompt_ack = false;
  char ack_hdr[4] = {0};
  if (httpd_req_get_hdr_value_str(req, "X-Nino-Prompt-Ack", ack_hdr,
                                  sizeof(ack_hdr)) == ESP_OK) {
    prompt_ack = (ack_hdr[0] == '1');
  }

  bool prompt_ack_chime = true;
  char chime_hdr[4] = {0};
  if (httpd_req_get_hdr_value_str(req, "X-Nino-Prompt-Ack-Chime", chime_hdr,
                                  sizeof(chime_hdr)) == ESP_OK) {
    prompt_ack_chime = (chime_hdr[0] != '0');
  }

  char ws_hdr[160] = {0};
  if (httpd_req_get_hdr_value_str(req, "X-Nino-Voice-Ws-Url", ws_hdr,
                                  sizeof(ws_hdr)) == ESP_OK) {
    apply_voice_ws_url_from_server(ws_hdr);
  }

  if (prompt_ack) {
    nino_voice_assist_set_next_prompt_ack_chime(prompt_ack_chime);
  }

  /* Optional emotion tag from server (e.g. happy/sad/surprised). */
  nino_eye_state_t eye_state = NINO_EYE_STATE_COUNT;
  char eye_hdr[24] = {0};
  if (httpd_req_get_hdr_value_str(req, "X-Nino-Eye-Expression", eye_hdr,
                                  sizeof(eye_hdr)) == ESP_OK) {
    eye_state = nino_eye_state_from_name(eye_hdr);
    if (eye_state < NINO_EYE_STATE_COUNT) {
      ESP_LOGI(TAG, "HTTP /play_wav eye_expression=%s -> state %d", eye_hdr,
               (int)eye_state);
      nino_eye_set_state(eye_state);
    }
  }

  if (nino_audio_queue_wav(buf, total, false, NINO_AUDIO_SERVO_FULL, prompt_ack,
                           eye_state) != ESP_OK) {
    httpd_resp_set_status(req, "503 Service Unavailable");
    httpd_resp_set_type(req, "application/json");
    return httpd_resp_send(req, "{\"ok\":false,\"error\":\"audio queue down\"}",
                           HTTPD_RESP_USE_STRLEN);
  }

  httpd_resp_set_type(req, "application/json");
  httpd_resp_set_hdr(req, "Access-Control-Allow-Origin", "*");
  return httpd_resp_send(req, "{\"ok\":true,\"queued\":true}",
                         HTTPD_RESP_USE_STRLEN);
}

static bool parse_json_string_field(const char *body, const char *key, char *out,
                                    size_t out_len);

static esp_err_t music_json_error(httpd_req_t *req, const char *status,
                                  const char *error) {
  httpd_resp_set_status(req, status);
  httpd_resp_set_type(req, "application/json");
  httpd_resp_set_hdr(req, "Access-Control-Allow-Origin", "*");
  char body[128];
  int n = snprintf(body, sizeof(body), "{\"ok\":false,\"error\":\"%s\"}", error);
  if (n <= 0 || n >= (int)sizeof(body)) {
    return httpd_resp_send(req, "{\"ok\":false,\"error\":\"failed\"}",
                           HTTPD_RESP_USE_STRLEN);
  }
  return httpd_resp_send(req, body, n);
}

static esp_err_t music_play_handler(httpd_req_t *req) {
  if (req->method != HTTP_POST) {
    return music_json_error(req, "405 Method Not Allowed", "POST only");
  }
  if (req->content_len <= 0 || req->content_len > 640) {
    return music_json_error(req, "400 Bad Request", "bad_body");
  }

  char body[641] = {0};
  int received = 0;
  while (received < req->content_len) {
    int r = httpd_req_recv(req, body + received, req->content_len - received);
    if (r <= 0) {
      return music_json_error(req, "400 Bad Request", "recv");
    }
    received += r;
  }
  body[received] = '\0';

  char url[512] = {0};
  if (!parse_json_string_field(body, "url", url, sizeof(url))) {
    return music_json_error(req, "400 Bad Request", "missing url");
  }

  esp_err_t err = nino_music_start(url);
  if (err != ESP_OK) {
    ESP_LOGW(TAG, "HTTP /music/play failed: %s", esp_err_to_name(err));
    if (err == ESP_ERR_INVALID_ARG) {
      return music_json_error(req, "400 Bad Request", "bad url");
    }
    if (err == ESP_ERR_NO_MEM) {
      return music_json_error(req, "503 Service Unavailable", "no_mem");
    }
    return music_json_error(req, "503 Service Unavailable", "start_failed");
  }

  httpd_resp_set_type(req, "application/json");
  httpd_resp_set_hdr(req, "Access-Control-Allow-Origin", "*");
  return httpd_resp_send(req, "{\"ok\":true,\"queued\":true}",
                         HTTPD_RESP_USE_STRLEN);
}

static esp_err_t music_stop_handler(httpd_req_t *req) {
  if (req->method != HTTP_POST) {
    return music_json_error(req, "405 Method Not Allowed", "POST only");
  }
  int remaining = req->content_len;
  while (remaining > 0) {
    char discard[64];
    int chunk = remaining > (int)sizeof(discard) ? (int)sizeof(discard) : remaining;
    int r = httpd_req_recv(req, discard, chunk);
    if (r <= 0) {
      break;
    }
    remaining -= r;
  }
  nino_music_stop();
  httpd_resp_set_type(req, "application/json");
  httpd_resp_set_hdr(req, "Access-Control-Allow-Origin", "*");
  return httpd_resp_send(req, "{\"ok\":true}", HTTPD_RESP_USE_STRLEN);
}

static esp_err_t music_status_handler(httpd_req_t *req) {
  if (req->method != HTTP_GET) {
    return music_json_error(req, "405 Method Not Allowed", "GET only");
  }
  httpd_resp_set_type(req, "application/json");
  httpd_resp_set_hdr(req, "Access-Control-Allow-Origin", "*");
  char body[64];
  int n = snprintf(body, sizeof(body), "{\"ok\":true,\"playing\":%s}",
                   nino_music_is_playing() ? "true" : "false");
  if (n <= 0 || n >= (int)sizeof(body)) {
    return httpd_resp_send_500(req);
  }
  return httpd_resp_send(req, body, n);
}

static bool parse_volume_percent_value(const char *text, int *out) {
  if (text == NULL || out == NULL || *text == '\0') {
    return false;
  }
  char *end = NULL;
  long value = strtol(text, &end, 10);
  if (end == text || *end != '\0' || value < 0 || value > 100) {
    return false;
  }
  *out = (int)value;
  return true;
}

/* Minimal extractor for a numeric JSON field, e.g. {"volume": 42}. Avoids
 * pulling in a full JSON parser for this single small request body. */
static bool parse_json_int_field(const char *body, const char *key, int *out) {
  if (body == NULL || key == NULL || out == NULL) {
    return false;
  }
  char pattern[24];
  int pn = snprintf(pattern, sizeof(pattern), "\"%s\"", key);
  if (pn <= 0 || pn >= (int)sizeof(pattern)) {
    return false;
  }
  const char *p = strstr(body, pattern);
  if (p == NULL) {
    return false;
  }
  p += pn;
  while (*p == ' ' || *p == '\t' || *p == ':' || *p == '"') {
    p++;
  }
  char *end = NULL;
  long value = strtol(p, &end, 10);
  if (end == p || value < 0 || value > 100) {
    return false;
  }
  *out = (int)value;
  return true;
}

static bool parse_json_bool_field(const char *body, const char *key, bool *out) {
  if (body == NULL || key == NULL || out == NULL) {
    return false;
  }
  char pattern[24];
  int pn = snprintf(pattern, sizeof(pattern), "\"%s\"", key);
  if (pn <= 0 || pn >= (int)sizeof(pattern)) {
    return false;
  }
  const char *p = strstr(body, pattern);
  if (p == NULL) {
    return false;
  }
  p = strchr(p + pn, ':');
  if (p == NULL) {
    return false;
  }
  p++;
  while (*p == ' ' || *p == '\t' || *p == '\r' || *p == '\n') {
    p++;
  }
  if (strncmp(p, "true", 4) == 0) {
    *out = true;
    return true;
  }
  if (strncmp(p, "false", 5) == 0) {
    *out = false;
    return true;
  }
  return false;
}

static bool parse_json_string_field(const char *body, const char *key, char *out,
                                    size_t out_len) {
  if (body == NULL || key == NULL || out == NULL || out_len == 0) {
    return false;
  }
  out[0] = '\0';
  char pattern[24];
  int pn = snprintf(pattern, sizeof(pattern), "\"%s\"", key);
  if (pn <= 0 || pn >= (int)sizeof(pattern)) {
    return false;
  }
  const char *p = strstr(body, pattern);
  if (p == NULL) {
    return false;
  }
  p = strchr(p + pn, ':');
  if (p == NULL) {
    return false;
  }
  p++;
  while (*p == ' ' || *p == '\t' || *p == '\r' || *p == '\n') {
    p++;
  }
  if (*p != '"') {
    return false;
  }
  p++;
  size_t n = 0;
  while (p[n] != '\0' && p[n] != '"') {
    n++;
  }
  if (p[n] != '"') {
    return false;
  }
  if (n >= out_len) {
    n = out_len - 1;
  }
  memcpy(out, p, n);
  out[n] = '\0';
  return n > 0;
}

static esp_err_t eye_expression_handler(httpd_req_t *req) {
  if (req->method != HTTP_POST) {
    httpd_resp_set_status(req, "405 Method Not Allowed");
    httpd_resp_set_type(req, "application/json");
    return httpd_resp_send(req, "{\"ok\":false,\"error\":\"POST only\"}",
                           HTTPD_RESP_USE_STRLEN);
  }

  if (req->content_len <= 0 || req->content_len > 128) {
    httpd_resp_set_status(req, "400 Bad Request");
    httpd_resp_set_type(req, "application/json");
    return httpd_resp_send(req, "{\"ok\":false,\"error\":\"bad_body\"}",
                           HTTPD_RESP_USE_STRLEN);
  }

  char body[129] = {0};
  int read_n = 0;
  while (read_n < req->content_len) {
    int r = httpd_req_recv(req, body + read_n, req->content_len - read_n);
    if (r <= 0) {
      httpd_resp_set_status(req, "400 Bad Request");
      httpd_resp_set_type(req, "application/json");
      return httpd_resp_send(req, "{\"ok\":false,\"error\":\"recv\"}",
                             HTTPD_RESP_USE_STRLEN);
    }
    read_n += r;
  }
  body[read_n] = '\0';

  char expression[24] = {0};
  const char *k = strstr(body, "\"expression\"");
  if (k == NULL) {
    k = strstr(body, "\"eye_expression\"");
  }
  if (k != NULL) {
    const char *colon = strchr(k, ':');
    if (colon != NULL) {
      const char *q1 = strchr(colon, '"');
      if (q1 != NULL) {
        q1++;
        const char *q2 = strchr(q1, '"');
        if (q2 != NULL && q2 > q1) {
          size_t n = (size_t)(q2 - q1);
          if (n >= sizeof(expression)) {
            n = sizeof(expression) - 1;
          }
          memcpy(expression, q1, n);
          expression[n] = '\0';
        }
      }
    }
  }

  /* Unknown/empty expression intentionally falls back to idle. */
  nino_eye_state_t target_state = nino_eye_state_from_name(expression);
  if (target_state >= NINO_EYE_STATE_COUNT) {
    target_state = NINO_EYE_IDLE;
  }
  nino_eye_state_t current_state = nino_eye_get_state();
  if (current_state != target_state) {
    nino_eye_set_state(target_state);
    ESP_LOGI(TAG, "HTTP eye expression -> %s", expression[0] ? expression : "idle");
  }

  httpd_resp_set_type(req, "application/json");
  httpd_resp_set_hdr(req, "Access-Control-Allow-Origin", "*");
  return httpd_resp_send(req, "{\"ok\":true}", HTTPD_RESP_USE_STRLEN);
}

static esp_err_t speaker_volume_handler(httpd_req_t *req) {
  httpd_resp_set_type(req, "application/json");
  httpd_resp_set_hdr(req, "Access-Control-Allow-Origin", "*");

  if (req->method == HTTP_GET) {
    char body[96];
    int vol = nino_audio_get_volume_percent();
    int n = snprintf(body, sizeof(body),
                     "{\"ok\":true,\"volume\":%d,\"volume_percent\":%d}", vol,
                     vol);
    if (n <= 0 || n >= (int)sizeof(body)) {
      return httpd_resp_send_500(req);
    }
    return httpd_resp_send(req, body, n);
  }

  if (req->method != HTTP_POST) {
    httpd_resp_set_status(req, "405 Method Not Allowed");
    return httpd_resp_send(req, "{\"ok\":false,\"error\":\"GET or POST only\"}",
                           HTTPD_RESP_USE_STRLEN);
  }

  int volume_percent = -1;
  bool ok = false;

  /* Preferred: JSON body {"volume": N} (also accept {"volume_percent": N}). */
  if (req->content_len > 0) {
    char body[128] = {0};
    int to_read = req->content_len < (int)sizeof(body) - 1
                      ? req->content_len
                      : (int)sizeof(body) - 1;
    int received = 0;
    while (received < to_read) {
      int r = httpd_req_recv(req, body + received, to_read - received);
      if (r <= 0) {
        break;
      }
      received += r;
    }
    body[received] = '\0';
    /* Drain any remainder beyond our buffer so the socket stays in sync. */
    int remaining = req->content_len - received;
    while (remaining > 0) {
      char discard[64];
      int chunk = remaining > (int)sizeof(discard) ? (int)sizeof(discard) : remaining;
      int r = httpd_req_recv(req, discard, chunk);
      if (r <= 0) {
        break;
      }
      remaining -= r;
    }
    ok = parse_json_int_field(body, "volume", &volume_percent) ||
         parse_json_int_field(body, "volume_percent", &volume_percent);
  }

  /* Fallback: query string ?value=0..100 (legacy callers). */
  if (!ok) {
    char query[64] = {0};
    char value_str[16] = {0};
    if (httpd_req_get_url_query_str(req, query, sizeof(query)) == ESP_OK) {
      if (httpd_query_key_value(query, "value", value_str, sizeof(value_str)) == ESP_OK) {
        ok = parse_volume_percent_value(value_str, &volume_percent);
      }
    }
  }

  if (!ok) {
    httpd_resp_set_status(req, "400 Bad Request");
    return httpd_resp_send(
        req,
        "{\"ok\":false,\"error\":\"missing_or_invalid_value\",\"hint\":\"POST {\\\"volume\\\":0..100} or ?value=0..100\"}",
        HTTPD_RESP_USE_STRLEN);
  }

  esp_err_t err = nino_audio_set_volume_percent(volume_percent);
  if (err != ESP_OK) {
    return httpd_resp_send_500(req);
  }

  ESP_LOGI(TAG, "Voice/HTTP: speaker volume %d%%", volume_percent);
  char body[96];
  int vol = nino_audio_get_volume_percent();
  int n = snprintf(body, sizeof(body),
                   "{\"ok\":true,\"volume\":%d,\"volume_percent\":%d}", vol,
                   vol);
  if (n <= 0 || n >= (int)sizeof(body)) {
    return httpd_resp_send_500(req);
  }
  return httpd_resp_send(req, body, n);
}

static esp_err_t servo_360_handler(httpd_req_t *req) {
  if (req->method != HTTP_POST) {
    httpd_resp_set_status(req, "405 Method Not Allowed");
    httpd_resp_set_type(req, "application/json");
    return httpd_resp_send(req, "{\"ok\":false,\"error\":\"POST only\"}",
                           HTTPD_RESP_USE_STRLEN);
  }

  if (req->content_len > 0) {
    char discard[64];
    int remaining = req->content_len;
    while (remaining > 0) {
      int chunk = remaining > (int)sizeof(discard) ? (int)sizeof(discard) : remaining;
      int r = httpd_req_recv(req, discard, chunk);
      if (r <= 0) {
        break;
      }
      remaining -= r;
    }
  }

  nino_servo_motion_stop();

  if (nino_servo_recplay_is_busy()) {
    httpd_resp_set_type(req, "application/json");
    httpd_resp_set_hdr(req, "Access-Control-Allow-Origin", "*");
    httpd_resp_set_status(req, "409 Conflict");
    const char *err = nino_servo_recplay_mode() == NINO_SERVO_MODE_RECORD
                          ? "{\"ok\":false,\"error\":\"busy_record\"}"
                          : "{\"ok\":false,\"error\":\"busy_play\"}";
    return httpd_resp_send(req, err, HTTPD_RESP_USE_STRLEN);
  }

  esp_err_t err = nino_servo_dxl_spin_360();
  httpd_resp_set_type(req, "application/json");
  httpd_resp_set_hdr(req, "Access-Control-Allow-Origin", "*");

  if (err == ESP_ERR_INVALID_STATE) {
    if (!nino_servo_dxl_is_ready()) {
      httpd_resp_set_status(req, "503 Service Unavailable");
      return httpd_resp_send(req, "{\"ok\":false,\"error\":\"servos_not_ready\"}",
                             HTTPD_RESP_USE_STRLEN);
    }
    httpd_resp_set_status(req, "409 Conflict");
    return httpd_resp_send(req, "{\"ok\":false,\"error\":\"already_running\"}",
                           HTTPD_RESP_USE_STRLEN);
  }
  if (err != ESP_OK) {
    return httpd_resp_send_500(req);
  }

  ESP_LOGI(TAG, "Voice/HTTP: ID2 360 spin started");
  return httpd_resp_send(req, "{\"ok\":true,\"started\":true}", HTTPD_RESP_USE_STRLEN);
}

/* POST /demo  {"play": true}  → queue the embedded DEMO_main.wav clip. */
static esp_err_t demo_handler(httpd_req_t *req) {
  if (req->method != HTTP_POST) {
    httpd_resp_set_status(req, "405 Method Not Allowed");
    httpd_resp_set_type(req, "application/json");
    return httpd_resp_send(req, "{\"ok\":false,\"error\":\"POST only\"}",
                           HTTPD_RESP_USE_STRLEN);
  }

  /* Body is optional; default to play. Only a literal false disables it. */
  bool play = true;
  char body[64];
  int total = 0;
  while (total < (int)sizeof(body) - 1) {
    int r = httpd_req_recv(req, body + total, (int)sizeof(body) - 1 - total);
    if (r <= 0) {
      break;
    }
    total += r;
  }
  body[total] = '\0';
  /* Drain any bytes beyond our small buffer so the socket stays healthy. */
  char discard[64];
  while (httpd_req_recv(req, discard, sizeof(discard)) > 0) {
  }
  if (strstr(body, "\"play\"") != NULL && strstr(body, "false") != NULL) {
    play = false;
  }

  httpd_resp_set_type(req, "application/json");
  httpd_resp_set_hdr(req, "Access-Control-Allow-Origin", "*");

  if (!play) {
    return httpd_resp_send(req, "{\"ok\":true,\"played\":false}",
                           HTTPD_RESP_USE_STRLEN);
  }

  esp_err_t err = nino_push_buttons_trigger_demo();
  if (err != ESP_OK) {
    httpd_resp_set_status(req, "503 Service Unavailable");
    return httpd_resp_send(req, "{\"ok\":false,\"error\":\"demo_unavailable\"}",
                           HTTPD_RESP_USE_STRLEN);
  }

  ESP_LOGI(TAG, "HTTP /demo: play DEMO_main.wav");
  return httpd_resp_send(req, "{\"ok\":true,\"played\":true}",
                         HTTPD_RESP_USE_STRLEN);
}

#define WIFI_PROV_JSON_MAX 384

static void wifi_http_set_cors(httpd_req_t *req) {
  httpd_resp_set_hdr(req, "Access-Control-Allow-Origin", "*");
  httpd_resp_set_hdr(req, "Access-Control-Allow-Methods", "GET, POST, OPTIONS");
  httpd_resp_set_hdr(req, "Access-Control-Allow-Headers", "Content-Type");
}

static const char *json_value_start(const char *body, const char *key) {
  if (body == NULL || key == NULL) {
    return NULL;
  }
  char needle[48];
  snprintf(needle, sizeof(needle), "\"%s\"", key);
  const char *p = strstr(body, needle);
  if (p == NULL) {
    return NULL;
  }
  p = strchr(p + strlen(needle), ':');
  if (p == NULL) {
    return NULL;
  }
  p++;
  while (*p == ' ' || *p == '\t' || *p == '\r' || *p == '\n') {
    p++;
  }
  if (*p != '"') {
    return NULL;
  }
  return p + 1;
}

static bool json_copy_quoted_value(const char *start, char *out, size_t out_sz) {
  if (start == NULL || out == NULL || out_sz == 0) {
    return false;
  }
  size_t n = 0;
  for (const char *p = start; *p != '\0'; ++p) {
    if (*p == '"' && (p == start || *(p - 1) != '\\')) {
      break;
    }
    if (*p == '\\' && *(p + 1) != '\0') {
      ++p;
    }
    if (n + 1 >= out_sz) {
      return false;
    }
    out[n++] = *p;
  }
  out[n] = '\0';
  return true;
}

static int face_track_status_json(char *buf, size_t buf_sz) {
  nino_face_tracker_status_t status = {};
  nino_face_tracker_get_status(&status);

  return snprintf(
      buf, buf_sz,
      "{\"ok\":true,\"enabled\":%s,\"detector_ready\":%s,\"face_found\":%s,"
      "\"pan_goal\":%d,\"tilt_goal\":%d,"
      "\"last_face_cx\":%d,\"last_face_cy\":%d,"
      "\"last_frame_w\":%d,\"last_frame_h\":%d,"
      "\"last_frame_sequence\":%lu,"
      "\"paused_for_motion\":%s,\"paused_for_spin\":%s,"
      "\"paused_for_servo\":%s}",
      status.enabled ? "true" : "false",
      status.detector_ready ? "true" : "false",
      status.face_found ? "true" : "false", status.pan_goal, status.tilt_goal,
      status.last_face_cx, status.last_face_cy, status.last_frame_w,
      status.last_frame_h, (unsigned long)status.last_frame_sequence,
      status.paused_for_motion ? "true" : "false",
      status.paused_for_spin ? "true" : "false",
      status.paused_for_servo ? "true" : "false");
}

static int app_status_json(char *buf, size_t buf_sz) {
  char sta_ip[16] = "0.0.0.0";
  wifi_config_get_sta_ip(sta_ip, sizeof(sta_ip));
  char mdns_host[MDNS_HOSTNAME_MAX + 1];
  mdns_hostname_for_device(mdns_host, sizeof(mdns_host));
  const char *fw_version = PROJECT_VER;
  nino_face_tracker_status_t face_track = {};
  nino_face_tracker_get_status(&face_track);
  char servo_frag[128];
  int sn = nino_servo_recplay_status_json(servo_frag, sizeof(servo_frag));
  if (sn < 0 || sn >= (int)sizeof(servo_frag)) {
    snprintf(servo_frag, sizeof(servo_frag), "\"ready\":false,\"mode\":\"idle\",\"ids_online\":[1,2]");
  }

  return snprintf(
      buf, buf_sz,
      "{\"ok\":true,\"device_name\":\"%s\",\"device_id\":\"%s\","
      "\"wifi_ssid\":\"%s\","
      "\"volume\":%d,\"firmware\":\"%s\",\"sta_connected\":%s,"
      "\"ip\":\"%s\",\"mdns_host\":\"%s.local\","
      "\"face_track\":{\"enabled\":%s,\"detector_ready\":%s,"
      "\"face_found\":%s},"
      "\"servo\":{%s}}",
      s_device_name, s_device_id, s_sta_ssid, nino_audio_get_volume_percent(),
      fw_version, s_sta_connected ? "true" : "false", sta_ip, mdns_host,
      face_track.enabled ? "true" : "false",
      face_track.detector_ready ? "true" : "false",
      face_track.face_found ? "true" : "false", servo_frag);
}

int wifi_config_status_json(char *buf, size_t buf_sz) {
  wifi_mode_t mode;
  if (esp_wifi_get_mode(&mode) != ESP_OK) {
    mode = s_wifi_mode;
  }
  const char *mode_str = (mode == WIFI_MODE_AP)    ? "ap"
                         : (mode == WIFI_MODE_STA) ? "sta"
                                                   : "apsta";
  char ap_ip[16] = "0.0.0.0";
  char sta_ip[16] = "0.0.0.0";
  wifi_config_get_ap_ip(ap_ip, sizeof(ap_ip));
  wifi_config_get_sta_ip(sta_ip, sizeof(sta_ip));
  return snprintf(
      buf, buf_sz,
      "{\"ok\":true,\"ap_ssid\":\"%s\",\"ble_name\":\"%s\","
      "\"ble_service\":\"%s\",\"mode\":\"%s\",\"sta_connected\":%s,"
      "\"sta_ssid\":\"%s\",\"ap_ip\":\"%s\",\"sta_ip\":\"%s\","
      "\"provisioned\":%s}",
      WIFI_CONFIG_AP_SSID, wifi_prov_ble_device_name(), WIFI_PROV_BLE_SVC_UUID,
      mode_str, s_sta_connected ? "true" : "false", s_sta_ssid, ap_ip, sta_ip,
      wifi_config_is_provisioned() ? "true" : "false");
}

static esp_err_t face_track_handler(httpd_req_t *req) {
  if (req->method == HTTP_OPTIONS) {
    httpd_resp_set_status(req, "204 No Content");
    wifi_http_set_cors(req);
    return httpd_resp_send(req, NULL, 0);
  }

  httpd_resp_set_type(req, "application/json");
  wifi_http_set_cors(req);

  if (req->method == HTTP_GET) {
    char body[384];
    int n = face_track_status_json(body, sizeof(body));
    if (n < 0 || n >= (int)sizeof(body)) {
      return httpd_resp_send_500(req);
    }
    return httpd_resp_send(req, body, n);
  }

  if (req->method != HTTP_POST) {
    httpd_resp_set_status(req, "405 Method Not Allowed");
    return httpd_resp_send(req, "{\"ok\":false,\"error\":\"GET or POST only\"}",
                           HTTPD_RESP_USE_STRLEN);
  }

  if (req->content_len <= 0 || req->content_len > 128) {
    httpd_resp_set_status(req, "400 Bad Request");
    return httpd_resp_send(
        req,
        "{\"ok\":false,\"error\":\"bad_body\","
        "\"hint\":\"POST {\\\"enabled\\\":true} or {\\\"action\\\":\\\"on\\\"}\"}",
        HTTPD_RESP_USE_STRLEN);
  }

  char body[129] = {0};
  int read_n = 0;
  while (read_n < req->content_len) {
    int r = httpd_req_recv(req, body + read_n, req->content_len - read_n);
    if (r <= 0) {
      httpd_resp_set_status(req, "400 Bad Request");
      return httpd_resp_send(req, "{\"ok\":false,\"error\":\"recv\"}",
                             HTTPD_RESP_USE_STRLEN);
    }
    read_n += r;
  }
  body[read_n] = '\0';

  bool want_enable = false;
  bool have_command = false;
  if (parse_json_bool_field(body, "enabled", &want_enable)) {
    have_command = true;
  } else {
    char action[8] = {0};
    const char *action_start = json_value_start(body, "action");
    if (action_start != NULL &&
        json_copy_quoted_value(action_start, action, sizeof(action))) {
      if (strcmp(action, "on") == 0) {
        want_enable = true;
        have_command = true;
      } else if (strcmp(action, "off") == 0) {
        want_enable = false;
        have_command = true;
      }
    }
  }

  if (!have_command) {
    httpd_resp_set_status(req, "400 Bad Request");
    return httpd_resp_send(
        req,
        "{\"ok\":false,\"error\":\"missing_or_invalid_command\","
        "\"hint\":\"POST {\\\"enabled\\\":true} or {\\\"action\\\":\\\"on\\\"}\"}",
        HTTPD_RESP_USE_STRLEN);
  }

  if (want_enable && !nino_face_detect_is_ready()) {
    httpd_resp_set_status(req, "503 Service Unavailable");
    return httpd_resp_send(req, "{\"ok\":false,\"error\":\"detector_not_ready\"}",
                           HTTPD_RESP_USE_STRLEN);
  }

  nino_face_tracker_set_enabled(want_enable);
  ESP_LOGI(TAG, "HTTP face track -> %s", want_enable ? "ON" : "OFF");

  char response[384];
  int n = face_track_status_json(response, sizeof(response));
  if (n < 0 || n >= (int)sizeof(response)) {
    return httpd_resp_send_500(req);
  }
  return httpd_resp_send(req, response, n);
}

static esp_err_t status_handler(httpd_req_t *req) {
  if (req->method != HTTP_GET) {
    httpd_resp_set_status(req, "405 Method Not Allowed");
    httpd_resp_set_type(req, "application/json");
    return httpd_resp_send(req, "{\"ok\":false,\"error\":\"GET only\"}",
                           HTTPD_RESP_USE_STRLEN);
  }

  char body[640];
  int n = app_status_json(body, sizeof(body));
  if (n < 0 || n >= (int)sizeof(body)) {
    return httpd_resp_send_500(req);
  }

  httpd_resp_set_type(req, "application/json");
  wifi_http_set_cors(req);
  return httpd_resp_send(req, body, n);
}

#if CONFIG_HTTPD_WS_SUPPORT
static esp_err_t status_ws_send(httpd_req_t *req) {
  char body[640];
  int n = app_status_json(body, sizeof(body));
  if (n < 0 || n >= (int)sizeof(body)) {
    return ESP_FAIL;
  }
  httpd_ws_frame_t out = {
      .type = HTTPD_WS_TYPE_TEXT,
      .payload = (uint8_t *)body,
      .len = (size_t)n,
  };
  return httpd_ws_send_frame(req, &out);
}

static esp_err_t status_ws_handler(httpd_req_t *req) {
  if (req->method == HTTP_GET) {
    return ESP_OK; // websocket handshake
  }

  httpd_ws_frame_t in = {};
  in.type = HTTPD_WS_TYPE_TEXT;
  esp_err_t err = httpd_ws_recv_frame(req, &in, 0);
  if (err != ESP_OK) {
    return err;
  }

  char *payload = NULL;
  if (in.len > 0) {
    payload = calloc(1, in.len + 1);
    if (payload == NULL) {
      return ESP_ERR_NO_MEM;
    }
    in.payload = (uint8_t *)payload;
    err = httpd_ws_recv_frame(req, &in, in.len);
    if (err != ESP_OK) {
      free(payload);
      return err;
    }
  }

  bool send_status = true;
  if (payload != NULL && in.type == HTTPD_WS_TYPE_TEXT) {
    if (strcmp(payload, "status") != 0 && strcmp(payload, "get_status") != 0 &&
        strcmp(payload, "ping") != 0) {
      send_status = false;
    }
  }

  if (!send_status) {
    const char *msg = "{\"ok\":false,\"error\":\"send 'status'\"}";
    httpd_ws_frame_t out = {
        .type = HTTPD_WS_TYPE_TEXT,
        .payload = (uint8_t *)msg,
        .len = strlen(msg),
    };
    err = httpd_ws_send_frame(req, &out);
  } else {
    err = status_ws_send(req);
  }

  free(payload);
  return err;
}
#endif

static esp_err_t wifi_prov_status_handler(httpd_req_t *req) {
  if (req->method != HTTP_GET) {
    httpd_resp_set_status(req, "405 Method Not Allowed");
    return httpd_resp_sendstr(req, "{\"ok\":false,\"error\":\"GET only\"}");
  }
  char body[320];
  int n = wifi_config_status_json(body, sizeof(body));
  if (n < 0 || n >= (int)sizeof(body)) {
    return httpd_resp_send_500(req);
  }
  httpd_resp_set_type(req, "application/json");
  wifi_http_set_cors(req);
  return httpd_resp_send(req, body, (size_t)n);
}

static esp_err_t wifi_prov_config_handler(httpd_req_t *req) {
  if (req->method == HTTP_OPTIONS) {
    httpd_resp_set_status(req, "204 No Content");
    wifi_http_set_cors(req);
    return httpd_resp_send(req, NULL, 0);
  }
  if (req->method != HTTP_POST) {
    httpd_resp_set_status(req, "405 Method Not Allowed");
    httpd_resp_set_type(req, "application/json");
    wifi_http_set_cors(req);
    return httpd_resp_send(req, "{\"ok\":false,\"error\":\"POST only\"}",
                           HTTPD_RESP_USE_STRLEN);
  }
  if (req->content_len <= 0 || req->content_len >= WIFI_PROV_JSON_MAX) {
    httpd_resp_set_status(req, "400 Bad Request");
    httpd_resp_set_type(req, "application/json");
    wifi_http_set_cors(req);
    return httpd_resp_send(req, "{\"ok\":false,\"error\":\"bad_body\"}",
                           HTTPD_RESP_USE_STRLEN);
  }

  char *body = malloc((size_t)req->content_len + 1);
  if (body == NULL) {
    return httpd_resp_send_500(req);
  }
  int received = 0;
  while (received < req->content_len) {
    int r = httpd_req_recv(req, body + received, req->content_len - received);
    if (r <= 0) {
      free(body);
      httpd_resp_set_status(req, "400 Bad Request");
      httpd_resp_set_type(req, "application/json");
      wifi_http_set_cors(req);
      return httpd_resp_send(req, "{\"ok\":false,\"error\":\"recv\"}",
                             HTTPD_RESP_USE_STRLEN);
    }
    received += r;
  }
  body[received] = '\0';

  char ssid[WIFI_CONFIG_STA_SSID_MAX] = "";
  char pass[WIFI_CONFIG_STA_PASS_MAX] = "";
  const char *ssid_start = json_value_start(body, "ssid");
  if (!json_copy_quoted_value(ssid_start, ssid, sizeof(ssid)) || ssid[0] == '\0') {
    free(body);
    httpd_resp_set_status(req, "400 Bad Request");
    httpd_resp_set_type(req, "application/json");
    wifi_http_set_cors(req);
    return httpd_resp_send(req, "{\"ok\":false,\"error\":\"missing_ssid\"}",
                           HTTPD_RESP_USE_STRLEN);
  }
  const char *pass_start = json_value_start(body, "password");
  if (pass_start != NULL) {
    (void)json_copy_quoted_value(pass_start, pass, sizeof(pass));
  }
  free(body);

  ESP_LOGI(TAG, "WiFi provision: SSID %s", ssid);
  if (wifi_config_set_sta_credentials(ssid, pass) != ESP_OK) {
    httpd_resp_set_status(req, "400 Bad Request");
    httpd_resp_set_type(req, "application/json");
    wifi_http_set_cors(req);
    return httpd_resp_send(req, "{\"ok\":false,\"error\":\"invalid_ssid\"}",
                           HTTPD_RESP_USE_STRLEN);
  }

  esp_err_t err = wifi_config_sta_connect(WIFI_MODE_STA);
  httpd_resp_set_type(req, "application/json");
  wifi_http_set_cors(req);
  if (err != ESP_OK) {
    httpd_resp_set_status(req, "500 Internal Server Error");
    return httpd_resp_send(req, "{\"ok\":false,\"error\":\"connect_failed\"}",
                           HTTPD_RESP_USE_STRLEN);
  }
  return httpd_resp_send(req, "{\"ok\":true,\"status\":\"connecting\"}",
                         HTTPD_RESP_USE_STRLEN);
}

static esp_err_t save_device_name_to_nvs(void) {
  nvs_handle_t h;
  esp_err_t err = nvs_open(NVS_NAMESPACE, NVS_READWRITE, &h);
  if (err != ESP_OK) {
    return err;
  }
  err = nvs_set_str(h, NVS_KEY_DEVICE_NAME, s_device_name);
  if (err == ESP_OK) {
    err = nvs_commit(h);
  }
  nvs_close(h);
  return err;
}

static esp_err_t device_name_handler(httpd_req_t *req) {
  if (req->method == HTTP_OPTIONS) {
    httpd_resp_set_status(req, "204 No Content");
    wifi_http_set_cors(req);
    return httpd_resp_send(req, NULL, 0);
  }

  httpd_resp_set_type(req, "application/json");
  wifi_http_set_cors(req);

  if (req->method == HTTP_GET) {
    char body[96];
    int n = snprintf(body, sizeof(body), "{\"ok\":true,\"device_name\":\"%s\"}",
                     s_device_name);
    if (n <= 0 || n >= (int)sizeof(body)) {
      return httpd_resp_send_500(req);
    }
    return httpd_resp_send(req, body, n);
  }

  if (req->method != HTTP_POST) {
    httpd_resp_set_status(req, "405 Method Not Allowed");
    return httpd_resp_send(req, "{\"ok\":false,\"error\":\"GET or POST only\"}",
                           HTTPD_RESP_USE_STRLEN);
  }

  if (req->content_len <= 0 || req->content_len >= WIFI_PROV_JSON_MAX) {
    httpd_resp_set_status(req, "400 Bad Request");
    return httpd_resp_send(req, "{\"ok\":false,\"error\":\"bad_body\"}",
                           HTTPD_RESP_USE_STRLEN);
  }

  char *body = malloc((size_t)req->content_len + 1);
  if (body == NULL) {
    return httpd_resp_send_500(req);
  }
  int received = 0;
  while (received < req->content_len) {
    int r = httpd_req_recv(req, body + received, req->content_len - received);
    if (r <= 0) {
      free(body);
      httpd_resp_set_status(req, "400 Bad Request");
      return httpd_resp_send(req, "{\"ok\":false,\"error\":\"recv\"}",
                             HTTPD_RESP_USE_STRLEN);
    }
    received += r;
  }
  body[received] = '\0';

  char next_name[WIFI_PROV_BLE_DEVICE_NAME_MAX + 1];
  const char *name_start = json_value_start(body, "device_name");
  bool copied =
      json_copy_quoted_value(name_start, next_name, sizeof(next_name));
  free(body);

  if (!copied || !is_valid_device_name(next_name)) {
    httpd_resp_set_status(req, "400 Bad Request");
    return httpd_resp_send(req,
                           "{\"ok\":false,\"error\":\"invalid_device_name\"}",
                           HTTPD_RESP_USE_STRLEN);
  }

  copy_device_name(s_device_name, sizeof(s_device_name), next_name);
  wifi_prov_ble_set_device_name(s_device_name);
  if (s_mdns_started) {
    mdns_stop_service();
    mdns_start_service();
  }

  esp_err_t err = save_device_name_to_nvs();
  if (err != ESP_OK) {
    ESP_LOGW(TAG, "Failed to save device name to NVS: %s", esp_err_to_name(err));
    httpd_resp_set_status(req, "500 Internal Server Error");
    return httpd_resp_send(req, "{\"ok\":false,\"error\":\"nvs_save_failed\"}",
                           HTTPD_RESP_USE_STRLEN);
  }

  char out[112];
  int n = snprintf(out, sizeof(out), "{\"ok\":true,\"device_name\":\"%s\"}",
                   s_device_name);
  if (n <= 0 || n >= (int)sizeof(out)) {
    return httpd_resp_send_500(req);
  }
  return httpd_resp_send(req, out, n);
}

static void load_voice_ws_from_nvs(void) {
  s_voice_ws_url[0] = '\0';
  nvs_handle_t h;
  if (nvs_open(NVS_NAMESPACE, NVS_READONLY, &h) != ESP_OK) {
    return;
  }
  size_t sz = sizeof(s_voice_ws_url);
  (void)nvs_get_str(h, NVS_KEY_VOICE_WS, s_voice_ws_url, &sz);
  nvs_close(h);
  ensure_voice_ws_url_has_device_id();
}

static void apply_voice_ws_url_from_server(const char *uri) {
  if (uri == NULL || uri[0] == '\0') {
    return;
  }
  strncpy(s_voice_ws_url, uri, sizeof(s_voice_ws_url) - 1);
  s_voice_ws_url[sizeof(s_voice_ws_url) - 1] = '\0';
  ensure_voice_ws_url_has_device_id();
  nvs_handle_t h;
  if (nvs_open(NVS_NAMESPACE, NVS_READWRITE, &h) == ESP_OK) {
    (void)nvs_set_str(h, NVS_KEY_VOICE_WS, s_voice_ws_url);
    (void)nvs_commit(h);
    nvs_close(h);
  }
  nino_voice_assist_set_ws_uri(s_voice_ws_url);
  begin_voice_server_link();
  schedule_wifi_network_report();
  ESP_LOGI(TAG, "Voice WS URL from server: %s", s_voice_ws_url);
}

static int cmd_voice(int argc, char **argv) {
  if (argc >= 2 && strcmp(argv[1], "connect") == 0) {
    if (argc < 3) {
      printf("Usage: voice connect <IPv4> [port]   (default port 8000)\n"
             "  Saves ws://<ip>:<port>/voice-query?device_id=... then use: start [seconds]\n");
      return 0;
    }
    int port = 8000;
    if (argc >= 4) {
      port = atoi(argv[3]);
      if (port < 1 || port > 65535) {
        printf("Invalid port\n");
        return 1;
      }
    }
    int n = snprintf(s_voice_ws_url, sizeof(s_voice_ws_url), "ws://%s:%d/voice-query",
                     argv[2], port);
    if (n <= 0 || (size_t)n >= sizeof(s_voice_ws_url)) {
      printf("Voice URL too long or invalid\n");
      return 1;
    }
    ensure_voice_ws_url_has_device_id();
    nvs_handle_t h;
    if (nvs_open(NVS_NAMESPACE, NVS_READWRITE, &h) == ESP_OK) {
      (void)nvs_set_str(h, NVS_KEY_VOICE_WS, s_voice_ws_url);
      (void)nvs_commit(h);
      nvs_close(h);
      printf("Saved voice url to NVS\n");
    } else {
      printf("NVS open failed\n");
    }
    nino_voice_assist_set_ws_uri(s_voice_ws_url);
    begin_voice_server_link();
    schedule_wifi_network_report();
    printf("Voice assistant: %s\n", s_voice_ws_url);
    printf("Type start to record AUX IN and send to the server\n");
    return 0;
  }
  if (argc >= 2 && strcmp(argv[1], "url") == 0) {
    if (argc < 3) {
      printf("voice url: \"%s\"\n", s_voice_ws_url);
      return 0;
    }
    strncpy(s_voice_ws_url, argv[2], sizeof(s_voice_ws_url) - 1);
    s_voice_ws_url[sizeof(s_voice_ws_url) - 1] = '\0';
    ensure_voice_ws_url_has_device_id();
    nvs_handle_t h;
    if (nvs_open(NVS_NAMESPACE, NVS_READWRITE, &h) == ESP_OK) {
      nvs_set_str(h, NVS_KEY_VOICE_WS, s_voice_ws_url);
      nvs_commit(h);
      nvs_close(h);
      printf("Saved voice url to NVS\n");
    } else {
      printf("NVS open failed\n");
    }
    nino_voice_assist_set_ws_uri(s_voice_ws_url);
    begin_voice_server_link();
    schedule_wifi_network_report();
    return 0;
  }
  if (argc >= 2 && strcmp(argv[1], "status") == 0) {
    printf("voice in: %s (listen %s)\n",
           nino_mic_source_name(nino_mic_preferred_source()),
           nino_voice_assist_aux_listen_is_running() ? "armed" : "busy");
    printf("device_id: %s\n", s_device_id);
    printf("voice url: \"%s\"\n", s_voice_ws_url[0] ? s_voice_ws_url : "(not set)");
    printf("Aux-in energy starts a VAD capture after Sirena wake; or type start [seconds]\n");
    return 0;
  }
  printf("Usage: voice connect <ip> [port] | voice url [<ws-uri>] | voice status\n");
  return 0;
}

static int cmd_device(int argc, char **argv) {
  if (argc >= 2 && strcmp(argv[1], "id") == 0) {
    if (argc < 3) {
      printf("device_id: %s\n", s_device_id);
      return 0;
    }
    if (!is_valid_device_id(argv[2])) {
      printf("Invalid device_id (use 1-%d chars: A-Z a-z 0-9 - _)\n", DEVICE_ID_MAX);
      return 1;
    }
    copy_device_id(s_device_id, sizeof(s_device_id), argv[2]);
    esp_err_t err = save_device_id_to_nvs();
    if (err != ESP_OK) {
      printf("NVS save failed: %s\n", esp_err_to_name(err));
      return 1;
    }
    ensure_voice_ws_url_has_device_id();
    if (s_voice_ws_url[0] != '\0') {
      nvs_handle_t h;
      if (nvs_open(NVS_NAMESPACE, NVS_READWRITE, &h) == ESP_OK) {
        (void)nvs_set_str(h, NVS_KEY_VOICE_WS, s_voice_ws_url);
        (void)nvs_commit(h);
        nvs_close(h);
      }
      nino_voice_assist_set_ws_uri(s_voice_ws_url);
    }
    /* Hostname / TXT include device_id — refresh advertisement if online. */
    if (s_mdns_started) {
      mdns_stop_service();
      mdns_start_service();
    }
    printf("device_id set to %s\n", s_device_id);
    if (s_voice_ws_url[0] != '\0') {
      printf("voice url updated: %s\n", s_voice_ws_url);
    }
    return 0;
  }
  printf("Usage: device id [<id>]\n");
  return 0;
}

static int cmd_start(int argc, char **argv) {
  uint32_t seconds = 5;
  if (argc >= 2) {
    int s = atoi(argv[1]);
    if (s < 1 || s > 10) {
      printf("Usage: start [seconds]   (1-10, default 5)\n");
      return 1;
    }
    seconds = (uint32_t)s;
  }
  if (!nino_voice_assist_has_ws_uri()) {
    printf("Voice PC not linked. First: voice connect <PC_LAN_IP> 8000\n");
    return 1;
  }
  printf("Recording %u s from ES8311 AUX IN...\n", (unsigned)seconds);
  esp_err_t err = nino_voice_assist_run_query(seconds * 1000U);
  if (err != ESP_OK) {
    printf("start failed: %s\n", esp_err_to_name(err));
    return 1;
  }
  printf("Sent WAV to server — reply will play on the speaker\n");
  return 0;
}

static void start_cli_register(void) {
  const esp_console_cmd_t cmd = {
      .command = "start",
      .help = "Record AUX IN (default 5 s) and send WAV to the PC voice server",
      .hint = NULL,
      .func = &cmd_start,
      .argtable = NULL,
  };
  ESP_ERROR_CHECK(esp_console_cmd_register(&cmd));
}

static void voice_cli_register(void) {
  const esp_console_cmd_t cmd = {
      .command = "voice",
      .help = "voice connect <PC_IP> [port] | voice status",
      .hint = NULL,
      .func = &cmd_voice,
      .argtable = NULL,
  };
  ESP_ERROR_CHECK(esp_console_cmd_register(&cmd));
}

static void device_cli_register(void) {
  const esp_console_cmd_t cmd = {
      .command = "device",
      .help = "device id [<id>]  — print or set stable multi-robot device_id",
      .hint = NULL,
      .func = &cmd_device,
      .argtable = NULL,
  };
  ESP_ERROR_CHECK(esp_console_cmd_register(&cmd));
}

static int cmd_servo_360(int argc, char **argv) {
  (void)argc;
  (void)argv;

  if (!nino_servo_dxl_is_ready()) {
    printf("Servos not ready — connect U2D2 on J18 hub and wait for joint mode\n");
    return 1;
  }

  esp_err_t err = nino_servo_dxl_spin_360();
  if (err == ESP_ERR_INVALID_STATE) {
    printf("360 spin already running\n");
    return 1;
  }
  if (err != ESP_OK) {
    printf("360 spin failed: %s\n", esp_err_to_name(err));
    return 1;
  }

  printf("ID2 360 spin started (512 -> 0 -> 1023 -> 512)\n");
  return 0;
}

static int cmd_servo_status(int argc, char **argv) {
  (void)argc;
  (void)argv;

  printf("servo bus: %s\n",
         nino_servo_dxl_bus_open() ? "open" : "not open (connect U2D2 on J18 hub)");
  printf("servo ready: %s\n",
         nino_servo_dxl_is_ready() ? "yes" : "no");
  printf("spin360: %s\n",
         nino_servo_dxl_spin_is_active() ? "running" : "idle");
  printf("track hon: %s\n",
         nino_servo_dxl_track_hon_is_active() ? "running" : "idle");

  if (!nino_servo_dxl_bus_open()) {
    printf("servo chain: (bus not open)\n");
    return 1;
  }

  uint8_t ids[32];
  size_t count = 0;
  esp_err_t err = nino_servo_dxl_scan_chain(ids, sizeof(ids) / sizeof(ids[0]), &count);
  if (err == ESP_ERR_NOT_FOUND || count == 0) {
    printf("servo chain: none found (check power, TTL wiring, IDs, 1 Mbps baud)\n");
    return 1;
  }
  if (err != ESP_OK) {
    printf("servo chain scan failed: %s\n", esp_err_to_name(err));
    return 1;
  }

  printf("servo chain (%u):", (unsigned)count);
  for (size_t i = 0; i < count; i++) {
    printf(" ID%u", ids[i]);
    if (ids[i] == 1) {
      printf("(tilt)");
    } else if (ids[i] == 2) {
      printf("(pan)");
    }
  }
  printf("\n");
  return 0;
}

static int cmd_servo(int argc, char **argv) {
  if (argc >= 2 && strcmp(argv[1], "status") == 0) {
    return cmd_servo_status(0, NULL);
  }
  if (argc >= 2 && strcmp(argv[1], "360") == 0) {
    return cmd_servo_360(argc - 1, argv + 1);
  }
  printf("Usage: servo status | servo 360\n");
  return 0;
}

static void servo_cli_register(void) {
  const esp_console_cmd_t servo_cmd = {
      .command = "servo",
      .help = "servo status | servo 360",
      .hint = NULL,
      .func = &cmd_servo,
      .argtable = NULL,
  };
  ESP_ERROR_CHECK(esp_console_cmd_register(&servo_cmd));

  const esp_console_cmd_t spin_cmd = {
      .command = "360",
      .help = "ID2 full rotation: home to 512 if needed, then 512->0->1023->512",
      .hint = NULL,
      .func = &cmd_servo_360,
      .argtable = NULL,
  };
  ESP_ERROR_CHECK(esp_console_cmd_register(&spin_cmd));
}

static int cmd_track(int argc, char **argv) {
  if (argc < 2) {
    printf("Usage: track on | off | status\n");
    return 0;
  }

  if (strcmp(argv[1], "on") == 0) {
    if (!nino_face_detect_is_ready()) {
      printf("Face detector not ready yet\n");
      return 1;
    }
    nino_face_tracker_set_enabled(true);
    printf("Pan/tilt tracking ON (tilt ID 1, pan ID 2)\n");
    return 0;
  }

  if (strcmp(argv[1], "off") == 0) {
    nino_face_tracker_set_enabled(false);
    printf("Pan/tilt tracking OFF\n");
    return 0;
  }

  if (strcmp(argv[1], "status") == 0) {
    nino_face_tracker_status_t status = {};
    nino_face_tracker_get_status(&status);
    printf("track: %s\n", status.enabled ? "ON" : "OFF");
    printf("detector: %s\n", status.detector_ready ? "ready" : "not ready");
    printf("pan goal: %d\n", status.pan_goal);
    printf("tilt goal: %d\n", status.tilt_goal);
    printf("last frame seq: %lu\n", (unsigned long)status.last_frame_sequence);
    printf("face: %s\n", status.face_found ? "found" : "not found");
    if (status.face_found && status.last_frame_w > 0 && status.last_frame_h > 0) {
      printf("face cx/cy/frame: %d/%d (%dx%d)\n", status.last_face_cx,
             status.last_face_cy, status.last_frame_w, status.last_frame_h);
    }
    if (status.paused_for_motion || status.paused_for_spin ||
        status.paused_for_servo) {
      printf("paused:");
      if (status.paused_for_motion) {
        printf(" audio-motion");
      }
      if (status.paused_for_spin) {
        printf(" spin360-or-hon");
      }
      if (status.paused_for_servo) {
        printf(" servo-not-ready");
      }
      printf("\n");
    } else {
      printf("paused: no\n");
    }
    return 0;
  }

  printf("Usage: track on | off | status\n");
  return 0;
}

static void track_cli_register(void) {
  const esp_console_cmd_t cmd = {
      .command = "track",
      .help = "track on | off | status  (pan+tilt face tracking on servo IDs 1/2)",
      .hint = NULL,
      .func = &cmd_track,
      .argtable = NULL,
  };
  ESP_ERROR_CHECK(esp_console_cmd_register(&cmd));
}

static int cmd_hstop(int argc, char **argv) {
  (void)argc;
  (void)argv;

  esp_err_t err = nino_servo_dxl_track_hon_stop();
  if (err == ESP_ERR_INVALID_STATE) {
    printf("track hon is not running\n");
    return 1;
  }
  if (err != ESP_OK) {
    printf("hstop failed: %s\n", esp_err_to_name(err));
    return 1;
  }

  printf("hstop accepted: stopping track hon, waiting 2s, then moving ID2 to neutral 512\n");
  return 0;
}

static void hstop_cli_register(void) {
  const esp_console_cmd_t cmd = {
      .command = "hstop",
      .help = "Stop track hon loop, wait 2 seconds, then return ID2 to neutral (512)",
      .hint = NULL,
      .func = &cmd_hstop,
      .argtable = NULL,
  };
  ESP_ERROR_CHECK(esp_console_cmd_register(&cmd));
}

static int cmd_dinner(int argc, char **argv) {
  (void)argc;
  (void)argv;

  const size_t wav_len = (size_t)(schedule_dinnner_wav_end - schedule_dinnner_wav_start);
  if (wav_len < 44) {
    printf("Embedded schedule_dinnner.wav is missing or invalid\n");
    return 1;
  }

  esp_err_t err = nino_audio_queue_wav_copy(schedule_dinnner_wav_start, wav_len, false,
                                            NINO_AUDIO_SERVO_FULL, false);
  if (err != ESP_OK) {
    printf("Could not queue dinner clip: %s\n", esp_err_to_name(err));
    return 1;
  }

  printf("Dinner clip queued: speaker + L/R/U/D motion\n");
  return 0;
}

static void dinner_cli_register(void) {
  const esp_console_cmd_t cmd = {
      .command = "dinner",
      .help = "Play schedule_dinnner.wav with L/R/U/D servo motion",
      .hint = NULL,
      .func = &cmd_dinner,
      .argtable = NULL,
  };
  ESP_ERROR_CHECK(esp_console_cmd_register(&cmd));
}

static int cmd_bday(int argc, char **argv) {
  (void)argc;
  (void)argv;

  const size_t wav_len = (size_t)(bday_surprise_wav_end - bday_surprise_wav_start);
  if (wav_len < 44) {
    printf("Embedded Bday_Surprise.wav is missing or invalid\n");
    return 1;
  }

  esp_err_t err = nino_audio_queue_wav_copy(bday_surprise_wav_start, wav_len, false,
                                            NINO_AUDIO_SERVO_FULL, false);
  if (err != ESP_OK) {
    printf("Could not queue birthday clip: %s\n", esp_err_to_name(err));
    return 1;
  }

  printf("Birthday surprise queued: speaker + L/R/U/D motion\n");
  return 0;
}

static void bday_cli_register(void) {
  const esp_console_cmd_t cmd = {
      .command = "bday",
      .help = "Play Bday_Surprise.wav with L/R/U/D servo motion",
      .hint = NULL,
      .func = &cmd_bday,
      .argtable = NULL,
  };
  ESP_ERROR_CHECK(esp_console_cmd_register(&cmd));
}

static int cmd_speaker(int argc, char **argv) {
  if (argc >= 2 && strcmp(argv[1], "volume") == 0) {
    if (argc >= 3) {
      int volume = -1;
      if (!parse_volume_percent_value(argv[2], &volume)) {
        printf("Usage: speaker volume [0-100]\n");
        return 1;
      }
      esp_err_t err = nino_audio_set_volume_percent(volume);
      if (err != ESP_OK) {
        printf("Failed to set volume: %s\n", esp_err_to_name(err));
        return 1;
      }
    }
    printf("speaker volume: %d%%\n", nino_audio_get_volume_percent());
    return 0;
  }

  if (argc >= 2 && strcmp(argv[1], "mute") == 0) {
    bool muted = true;
    if (argc >= 3) {
      if (strcmp(argv[2], "off") == 0 || strcmp(argv[2], "0") == 0) {
        muted = false;
      } else if (strcmp(argv[2], "on") == 0 || strcmp(argv[2], "1") == 0) {
        muted = true;
      } else if (strcmp(argv[2], "toggle") == 0) {
        muted = !nino_audio_is_muted();
      } else {
        printf("Usage: speaker mute [on|off|toggle]\n");
        return 1;
      }
    }
    nino_mute_set(muted);
    printf("speaker %s\n", nino_audio_is_muted() ? "MUTED (solid red LED)" : "unmuted");
    return 0;
  }

  if (argc >= 2 && (strcmp(argv[1], "unmute") == 0 || strcmp(argv[1], "on") == 0)) {
    nino_mute_set(false);
    printf("speaker unmuted\n");
    return 0;
  }

  printf("speaker volume: %d%%  mute: %s\n", nino_audio_get_volume_percent(),
         nino_audio_is_muted() ? "on" : "off");
  printf("Usage: speaker volume [0-100]\n");
  printf("       speaker mute [on|off|toggle]\n");
  return 0;
}

static void speaker_cli_register(void) {
  const esp_console_cmd_t cmd = {
      .command = "speaker",
      .help = "speaker volume [0-100] | speaker mute [on|off|toggle]",
      .hint = NULL,
      .func = &cmd_speaker,
      .argtable = NULL,
  };
  ESP_ERROR_CHECK(esp_console_cmd_register(&cmd));
}

static void start_http_server(void) {
  httpd_config_t config = HTTPD_DEFAULT_CONFIG();
  config.server_port = HTTP_SERVER_PORT;
  config.stack_size = 8192;
  config.max_uri_handlers = 48;
  config.max_open_sockets = 7; /* ESP-IDF max on this target (httpd reserves 3 internally) */
  config.lru_purge_enable = true;
  config.recv_wait_timeout = 45;
  config.send_wait_timeout = 45;
  config.core_id = APP_CORE_NET;

  ESP_ERROR_CHECK(httpd_start(&s_http_server, &config));

  const httpd_uri_t index_uri = {
      .uri = "/",
      .method = HTTP_GET,
      .handler = index_handler,
      .user_ctx = NULL,
  };
  const httpd_uri_t stream_uri = {
      .uri = "/stream",
      .method = HTTP_GET,
      .handler = stream_handler,
      .user_ctx = NULL,
  };
  const httpd_uri_t view_uri = {
      .uri = "/view",
      .method = HTTP_GET,
      .handler = stream_route_handler,
      .user_ctx = NULL,
  };
  const httpd_uri_t stream_mjpeg_uri = {
      .uri = "/stream.mjpeg",
      .method = HTTP_GET,
      .handler = stream_handler,
      .user_ctx = NULL,
  };
  const httpd_uri_t snapshot_uri = {
      .uri = "/snapshot.jpg",
      .method = HTTP_GET,
      .handler = snapshot_handler,
      .user_ctx = NULL,
  };
  const httpd_uri_t play_wav_uri = {
      .uri = "/play_wav",
      .method = HTTP_POST,
      .handler = play_wav_handler,
      .user_ctx = NULL,
  };
  const httpd_uri_t music_play_uri = {
      .uri = "/music/play",
      .method = HTTP_POST,
      .handler = music_play_handler,
      .user_ctx = NULL,
  };
  const httpd_uri_t music_stop_uri = {
      .uri = "/music/stop",
      .method = HTTP_POST,
      .handler = music_stop_handler,
      .user_ctx = NULL,
  };
  const httpd_uri_t music_status_uri = {
      .uri = "/music/status",
      .method = HTTP_GET,
      .handler = music_status_handler,
      .user_ctx = NULL,
  };
  const httpd_uri_t servo_360_uri = {
      .uri = "/servo/360",
      .method = HTTP_POST,
      .handler = servo_360_handler,
      .user_ctx = NULL,
  };
  const httpd_uri_t eye_expression_uri = {
      .uri = "/eye/expression",
      .method = HTTP_POST,
      .handler = eye_expression_handler,
      .user_ctx = NULL,
  };
  const httpd_uri_t demo_uri = {
      .uri = "/demo",
      .method = HTTP_POST,
      .handler = demo_handler,
      .user_ctx = NULL,
  };
  const httpd_uri_t speaker_volume_get_uri = {
      .uri = "/speaker/volume",
      .method = HTTP_GET,
      .handler = speaker_volume_handler,
      .user_ctx = NULL,
  };
  const httpd_uri_t speaker_volume_post_uri = {
      .uri = "/speaker/volume",
      .method = HTTP_POST,
      .handler = speaker_volume_handler,
      .user_ctx = NULL,
  };
  const httpd_uri_t volume_get_uri = {
      .uri = "/volume",
      .method = HTTP_GET,
      .handler = speaker_volume_handler,
      .user_ctx = NULL,
  };
  const httpd_uri_t volume_post_uri = {
      .uri = "/volume",
      .method = HTTP_POST,
      .handler = speaker_volume_handler,
      .user_ctx = NULL,
  };
  const httpd_uri_t status_uri = {
      .uri = "/status",
      .method = HTTP_GET,
      .handler = status_handler,
      .user_ctx = NULL,
  };
  const httpd_uri_t face_track_get_uri = {
      .uri = "/face/track",
      .method = HTTP_GET,
      .handler = face_track_handler,
      .user_ctx = NULL,
  };
  const httpd_uri_t face_track_post_uri = {
      .uri = "/face/track",
      .method = HTTP_POST,
      .handler = face_track_handler,
      .user_ctx = NULL,
  };
  const httpd_uri_t face_track_opts_uri = {
      .uri = "/face/track",
      .method = HTTP_OPTIONS,
      .handler = face_track_handler,
      .user_ctx = NULL,
  };
#if CONFIG_HTTPD_WS_SUPPORT
  const httpd_uri_t status_ws_uri = {
      .uri = "/ws/status",
      .method = HTTP_GET,
      .handler = status_ws_handler,
      .user_ctx = NULL,
      .is_websocket = true,
      .handle_ws_control_frames = false,
      .supported_subprotocol = NULL,
  };
#endif
  const httpd_uri_t wifi_prov_config_uri = {
      .uri = "/api/wifi/config",
      .method = HTTP_POST,
      .handler = wifi_prov_config_handler,
      .user_ctx = NULL,
  };
  const httpd_uri_t wifi_prov_config_opts_uri = {
      .uri = "/api/wifi/config",
      .method = HTTP_OPTIONS,
      .handler = wifi_prov_config_handler,
      .user_ctx = NULL,
  };
  const httpd_uri_t wifi_prov_status_uri = {
      .uri = "/api/wifi/status",
      .method = HTTP_GET,
      .handler = wifi_prov_status_handler,
      .user_ctx = NULL,
  };
  const httpd_uri_t device_name_get_uri = {
      .uri = "/device/name",
      .method = HTTP_GET,
      .handler = device_name_handler,
      .user_ctx = NULL,
  };
  const httpd_uri_t device_name_post_uri = {
      .uri = "/device/name",
      .method = HTTP_POST,
      .handler = device_name_handler,
      .user_ctx = NULL,
  };
  const httpd_uri_t device_name_opts_uri = {
      .uri = "/device/name",
      .method = HTTP_OPTIONS,
      .handler = device_name_handler,
      .user_ctx = NULL,
  };

  ESP_ERROR_CHECK(httpd_register_uri_handler(s_http_server, &index_uri));
  ESP_ERROR_CHECK(httpd_register_uri_handler(s_http_server, &stream_uri));
  ESP_ERROR_CHECK(httpd_register_uri_handler(s_http_server, &view_uri));
  ESP_ERROR_CHECK(httpd_register_uri_handler(s_http_server, &stream_mjpeg_uri));
  ESP_ERROR_CHECK(httpd_register_uri_handler(s_http_server, &snapshot_uri));
  ESP_ERROR_CHECK(httpd_register_uri_handler(s_http_server, &play_wav_uri));
  ESP_ERROR_CHECK(httpd_register_uri_handler(s_http_server, &music_play_uri));
  ESP_ERROR_CHECK(httpd_register_uri_handler(s_http_server, &music_stop_uri));
  ESP_ERROR_CHECK(httpd_register_uri_handler(s_http_server, &music_status_uri));
  ESP_ERROR_CHECK(httpd_register_uri_handler(s_http_server, &servo_360_uri));
  ESP_ERROR_CHECK(nino_servo_recplay_register_http(s_http_server));
  ESP_ERROR_CHECK(httpd_register_uri_handler(s_http_server, &eye_expression_uri));
  ESP_ERROR_CHECK(httpd_register_uri_handler(s_http_server, &demo_uri));
  ESP_ERROR_CHECK(
      httpd_register_uri_handler(s_http_server, &speaker_volume_get_uri));
  ESP_ERROR_CHECK(
      httpd_register_uri_handler(s_http_server, &speaker_volume_post_uri));
  ESP_ERROR_CHECK(httpd_register_uri_handler(s_http_server, &volume_get_uri));
  ESP_ERROR_CHECK(httpd_register_uri_handler(s_http_server, &volume_post_uri));
  ESP_ERROR_CHECK(httpd_register_uri_handler(s_http_server, &status_uri));
  ESP_ERROR_CHECK(httpd_register_uri_handler(s_http_server, &face_track_get_uri));
  ESP_ERROR_CHECK(httpd_register_uri_handler(s_http_server, &face_track_post_uri));
  ESP_ERROR_CHECK(httpd_register_uri_handler(s_http_server, &face_track_opts_uri));
#if CONFIG_HTTPD_WS_SUPPORT
  ESP_ERROR_CHECK(httpd_register_uri_handler(s_http_server, &status_ws_uri));
#endif
  ESP_ERROR_CHECK(
      httpd_register_uri_handler(s_http_server, &wifi_prov_config_uri));
  ESP_ERROR_CHECK(
      httpd_register_uri_handler(s_http_server, &wifi_prov_config_opts_uri));
  ESP_ERROR_CHECK(
      httpd_register_uri_handler(s_http_server, &wifi_prov_status_uri));
  ESP_ERROR_CHECK(
      httpd_register_uri_handler(s_http_server, &device_name_get_uri));
  ESP_ERROR_CHECK(
      httpd_register_uri_handler(s_http_server, &device_name_post_uri));
  ESP_ERROR_CHECK(
      httpd_register_uri_handler(s_http_server, &device_name_opts_uri));
}

static void usb_lib_task(void *arg) {
  (void)arg;

  while (true) {
    uint32_t event_flags = 0;
    usb_host_lib_handle_events(portMAX_DELAY, &event_flags);

    if (event_flags & USB_HOST_LIB_EVENT_FLAGS_NO_CLIENTS) {
      ESP_LOGW(TAG, "USB host: no clients (do not free devices — hub+camera+U2D2)");
    }
    if (event_flags & USB_HOST_LIB_EVENT_FLAGS_ALL_FREE) {
      ESP_LOGI(TAG, "USB host reports all devices freed");
    }
  }
}

static void stream_event_callback(const uvc_host_stream_event_data_t *event,
                                  void *user_ctx) {
  (void)user_ctx;

  switch (event->type) {
  case UVC_HOST_TRANSFER_ERROR:
    ESP_LOGE(TAG, "USB transfer error: %s",
             esp_err_to_name(event->transfer_error.error));
    break;
  case UVC_HOST_DEVICE_DISCONNECTED:
    ESP_LOGW(TAG, "UVC device disconnected");
    s_device_connected = false;
    ESP_ERROR_CHECK(
        uvc_host_stream_close(event->device_disconnected.stream_hdl));
    break;
  case UVC_HOST_FRAME_BUFFER_OVERFLOW:
    ESP_LOGW(TAG, "Frame buffer overflow, increase frame_size if needed");
    break;
  case UVC_HOST_FRAME_BUFFER_UNDERFLOW:
    ESP_LOGW(TAG, "Frame buffer underflow, processing is too slow");
    break;
#ifdef UVC_HOST_SUSPEND_RESUME_API_SUPPORTED
  case UVC_HOST_DEVICE_SUSPENDED:
    ESP_LOGW(TAG, "UVC device suspended");
    break;
  case UVC_HOST_DEVICE_RESUMED:
    ESP_LOGI(TAG, "UVC device resumed");
    break;
#endif
  default:
    ESP_LOGW(TAG, "Unhandled stream event: %d", event->type);
    break;
  }
}

static bool frame_callback(const uvc_host_frame_t *frame, void *user_ctx) {
  QueueHandle_t frame_queue = (QueueHandle_t)user_ctx;
  BaseType_t sent = xQueueSendToBack(frame_queue, &frame, 0);
  if (sent != pdPASS) {
    return true;
  }
  return false;
}

static bool select_stream_format(const uvc_host_frame_info_t *frame_list,
                                 size_t frame_count,
                                 uvc_host_stream_format_t *selected_format) {
  const uvc_host_frame_info_t *best = NULL;
  const uvc_host_frame_info_t *fallback_mjpeg = NULL;

  for (size_t i = 0; i < frame_count; ++i) {
    const uvc_host_frame_info_t *candidate = &frame_list[i];
    float fps = frame_interval_to_fps(candidate->default_interval);

    ESP_LOGI(TAG, "Camera mode %u: %s %ux%u @ %.1f fps", (unsigned)i,
             format_to_str(candidate->format), candidate->h_res,
             candidate->v_res, fps);

    if (candidate->format != UVC_VS_FORMAT_MJPEG) {
      continue;
    }

    if (fallback_mjpeg == NULL) {
      fallback_mjpeg = candidate;
    }

    if (candidate->h_res == UVC_TARGET_WIDTH &&
        candidate->v_res == UVC_TARGET_HEIGHT) {
      best = candidate;
      if (fps > 0.0f && fps <= UVC_TARGET_FPS) {
        break;
      }
    }
  }

  if (best == NULL) {
    best = fallback_mjpeg;
  }
  if (best == NULL) {
    return false;
  }

  selected_format->h_res = best->h_res;
  selected_format->v_res = best->v_res;
  {
    const float default_fps = frame_interval_to_fps(best->default_interval);
    if (UVC_TARGET_FPS > 0.0f && default_fps > 0.0f) {
      selected_format->fps =
          (UVC_TARGET_FPS < default_fps) ? UVC_TARGET_FPS : default_fps;
    } else {
      selected_format->fps = default_fps;
    }
  }
  selected_format->format = best->format;
  return true;
}

static void uvc_stream_task(void *arg) {
  (void)arg;

  while (true) {
    if (!s_device_connected) {
      vTaskDelay(pdMS_TO_TICKS(250));
      continue;
    }

    uvc_host_stream_hdl_t stream = NULL;
    uvc_host_stream_config_t stream_config = {
        .event_cb = stream_event_callback,
        .frame_cb = frame_callback,
        .user_ctx = s_frame_queue,
        .usb =
            {
                .dev_addr = s_selected_stream.dev_addr,
                .vid = UVC_HOST_ANY_VID,
                .pid = UVC_HOST_ANY_PID,
                .uvc_stream_index = s_selected_stream.stream_index,
            },
        .vs_format = s_selected_stream.format,
        .advanced =
            {
                .number_of_frame_buffers = UVC_FRAME_BUFFERS,
                .frame_size = UVC_FRAME_SIZE_BYTES,
                .frame_heap_caps = MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT,
                .number_of_urbs = UVC_URB_COUNT,
                .urb_size = UVC_URB_SIZE,
                .user_frame_buffers = NULL,
            },
    };

    ESP_LOGI(TAG,
             "Opening camera addr=%u stream=%u format=%s %ux%u @ %.1f fps, "
             "frame_size=%u, urbs=%u x %u",
             stream_config.usb.dev_addr, stream_config.usb.uvc_stream_index,
             format_to_str(stream_config.vs_format.format),
             stream_config.vs_format.h_res, stream_config.vs_format.v_res,
             stream_config.vs_format.fps,
             (unsigned)stream_config.advanced.frame_size,
             (unsigned)stream_config.advanced.number_of_urbs,
             (unsigned)stream_config.advanced.urb_size);

    esp_err_t err = uvc_host_stream_open(
        &stream_config, pdMS_TO_TICKS(UVC_OPEN_TIMEOUT_MS), &stream);
    if (err != ESP_OK) {
      ESP_LOGW(TAG, "Failed to open UVC stream: %s", esp_err_to_name(err));
      vTaskDelay(pdMS_TO_TICKS(1000));
      continue;
    }

    uvc_host_desc_print(stream);
    ESP_ERROR_CHECK(uvc_host_stream_start(stream));
    ESP_LOGI(TAG, "Camera stream started");

    while (s_device_connected) {
      uvc_host_frame_t *frame = NULL;
      if (xQueueReceive(s_frame_queue, &frame, pdMS_TO_TICKS(2000)) != pdPASS) {
        const int64_t now_us = esp_timer_get_time();
        if (s_last_uvc_timeout_log_us == 0 ||
            (now_us - s_last_uvc_timeout_log_us) >=
                (int64_t)UVC_FRAME_TIMEOUT_LOG_INTERVAL_MS * 1000LL) {
          s_last_uvc_timeout_log_us = now_us;
          ESP_LOGW(TAG, "Timed out waiting for a UVC frame");
        }
        continue;
      }

      latest_frame_store(frame);

      if ((s_latest_frame.sequence % 30U) == 1U) {
        ESP_LOGI(TAG, "Frame %lu: %ux%u %s len=%u",
                 (unsigned long)s_latest_frame.sequence, frame->vs_format.h_res,
                 frame->vs_format.v_res, format_to_str(frame->vs_format.format),
                 (unsigned)frame->data_len);
      }

      ESP_ERROR_CHECK(uvc_host_frame_return(stream, frame));
    }

    ESP_LOGI(TAG, "Stream loop exiting");
    vTaskDelay(pdMS_TO_TICKS(500));
  }
}

static void uvc_driver_event_callback(const uvc_host_driver_event_data_t *event,
                                      void *user_ctx) {
  (void)user_ctx;

  if (event->type != UVC_HOST_DRIVER_EVENT_DEVICE_CONNECTED) {
    return;
  }

  if (s_device_connected) {
    ESP_LOGW(TAG, "Ignoring additional UVC device on addr=%u",
             event->device_connected.dev_addr);
    return;
  }

  size_t frame_info_count = event->device_connected.frame_info_num;
  if (frame_info_count == 0) {
    ESP_LOGW(TAG, "Camera connected but no frame descriptors were reported");
    return;
  }

  free_frame_info_list();
  s_frame_info_list = calloc(frame_info_count, sizeof(*s_frame_info_list));
  if (s_frame_info_list == NULL) {
    ESP_LOGE(TAG, "Failed to allocate frame descriptor list");
    return;
  }
  s_frame_info_count = frame_info_count;

  ESP_ERROR_CHECK(uvc_host_get_frame_list(
      event->device_connected.dev_addr,
      event->device_connected.uvc_stream_index,
      (uvc_host_frame_info_t(*)[])s_frame_info_list, &s_frame_info_count));

  s_selected_stream.dev_addr = event->device_connected.dev_addr;
  s_selected_stream.stream_index = event->device_connected.uvc_stream_index;
  if (!select_stream_format(s_frame_info_list, s_frame_info_count,
                            &s_selected_stream.format)) {
    ESP_LOGE(TAG, "No MJPEG format available for HTTP streaming");
    free_frame_info_list();
    return;
  }

  ESP_LOGI(TAG, "Selected format: %s %ux%u @ %.1f fps",
           format_to_str(s_selected_stream.format.format),
           s_selected_stream.format.h_res, s_selected_stream.format.v_res,
           s_selected_stream.format.fps);

  s_device_connected = true;
  if (!s_stream_task_created) {
    BaseType_t ok = xTaskCreatePinnedToCore(
        uvc_stream_task, "uvc_stream", UVC_STREAM_TASK_STACK_SIZE, NULL,
        UVC_STREAM_TASK_PRIORITY, &s_stream_task_handle, APP_CORE_USB);
    assert(ok == pdPASS);
    s_stream_task_created = true;
  }
}

void app_main(void) {
  esp_log_level_set("esp_driver_usb", ESP_LOG_WARN);
  esp_log_level_set("uvc", ESP_LOG_WARN);
  /* uvc-isoc "missed EoF" spam is recoverable; keep it quiet in normal runtime. */
  esp_log_level_set("uvc-isoc", ESP_LOG_NONE);

  esp_err_t err = nvs_flash_init();
  if (err == ESP_ERR_NVS_NO_FREE_PAGES ||
      err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
    ESP_ERROR_CHECK(nvs_flash_erase());
    err = nvs_flash_init();
  }
  ESP_ERROR_CHECK(err);

#if CONFIG_ESP_HOSTED_ENABLED
  /* After scheduler start; constructor init exhausts internal DRAM (idle-task assert). */
  ESP_ERROR_CHECK(esp_hosted_init());
#endif

  /* Eye panels come up first so the robot shows its idle face during boot. */
  if (nino_display_init() == ESP_OK) {
    nino_eye_begin(); /* defaults to NINO_EYE_IDLE */
    ESP_LOGI(TAG, "NINO eyes ready (%s) — serial: eye <name> | rgb status",
             NINO_DISPLAY_LABEL);
  } else {
    ESP_LOGW(TAG, "%s eye displays init failed; running without eyes", NINO_DISPLAY_LABEL);
  }

  ESP_ERROR_CHECK(nino_voice_assist_init_mutex());
  load_device_id_from_nvs();
  load_voice_ws_from_nvs();
  nino_voice_assist_set_ws_uri(s_voice_ws_url);
  if (s_voice_ws_url[0] != '\0') {
    ESP_LOGI(TAG, "Loaded voice URL from NVS: %s", s_voice_ws_url);
  }

  s_frame_mutex = xSemaphoreCreateMutex();
  assert(s_frame_mutex != NULL);
  s_frame_queue = xQueueCreate(UVC_FRAME_QUEUE_LEN, sizeof(uvc_host_frame_t *));
  assert(s_frame_queue != NULL);
  nino_face_tracker_init();
  nino_servo_recplay_init();

  if (nino_rgb_led_init() != ESP_OK) {
    ESP_LOGW(TAG, "RGB LED init failed (GPIO 2/3/4)");
  }

  /* Wi-Fi init brings Hosted SDIO transport up. BLE must start AFTER that —
   * starting earlier races slave reset / SDIO and reboots the host. */
  if (wifi_init_all() != ESP_OK) {
    ESP_LOGW(TAG, "Running without Wi-Fi (ESP-Hosted SDIO to C6 not ready)");
  }
  s_boot_unprovisioned = !wifi_config_is_provisioned();
  BaseType_t ble_task_ok = xTaskCreatePinnedToCore(
      wifi_provisioning_task, "wifi_prov", 4096, NULL, 6, NULL, APP_CORE_NET);
  if (ble_task_ok != pdPASS) {
    ESP_LOGW(TAG, "BLE Wi-Fi provisioning task not started");
  }

  if (nino_audio_init() != ESP_OK) {
    ESP_LOGW(TAG,
             "Speaker (BSP audio) init failed; POST /play_wav may not work");
  }
  if (nino_battery_adc_init() != ESP_OK) {
    ESP_LOGW(TAG, "GPIO20 battery ADC init failed (22k / 2x3.3k divider)");
  }
  if (nino_music_init() != ESP_OK) {
    ESP_LOGW(TAG, "Music stream init failed; POST /music/play will not work");
  }
  (void)nino_audio_load_saved_volume();
  if (nino_voice_preload_wake_chime() != ESP_OK) {
    ESP_LOGW(TAG, "Wake chime preload failed — first beep may be slower");
  }
  ESP_ERROR_CHECK(nino_audio_queue_start());
  s_audio_queue_ready = true;
  schedule_wifi_connected_chime();
  if (!wifi_config_is_provisioned()) {
    /* No saved network: tell the user to open the app while BLE provisioning
     * is coming up, without waiting for USB/camera startup. */
    (void)play_go_app_clip();
  }
  if (nino_push_buttons_start() != ESP_OK) {
    ESP_LOGW(TAG, "GPIO push button task not started");
  }
  if (nino_battery_endurance_init() != ESP_OK) {
    ESP_LOGW(TAG, "Hardware test (GPIO48) not started");
  }
  xTaskCreatePinnedToCore(multicast_discovery_task, "discovery", 4096, NULL, 5,
                          NULL, APP_CORE_NET);
  xTaskCreatePinnedToCore(tcp_message_server_task, "tcp_server", 4096, NULL, 5,
                          NULL, APP_CORE_NET);

  console_init();
  start_http_server();
  /* HTTP-only mode for now: keep status/websocket on port 80. */

  ESP_LOGI(TAG, "Installing USB host stack");
  const usb_host_config_t usb_host_config = {
      .peripheral_map = 0,
      .skip_phy_setup = false,
      .intr_flags = ESP_INTR_FLAG_LOWMED,
  };
  esp_err_t usb_err = usb_host_install(&usb_host_config);
  if (usb_err != ESP_OK) {
    ESP_LOGE(TAG, "usb_host_install failed: %s", esp_err_to_name(usb_err));
    ESP_ERROR_CHECK(usb_err);
  }

  /* Lib task must run before clients enumerate hub downstream devices (camera + U2D2). */
  BaseType_t ok = xTaskCreatePinnedToCore(
      usb_lib_task, "usb_lib", USB_LIB_TASK_STACK_SIZE, NULL,
      USB_LIB_TASK_PRIORITY, NULL, APP_CORE_USB);
  assert(ok == pdPASS);
  vTaskDelay(pdMS_TO_TICKS(300));

  if (nino_servo_dxl_start() != ESP_OK) {
    ESP_LOGW(TAG, "Dynamixel servo task not started (connect U2D2 on HOST Type-A or external hub)");
  }

  BaseType_t track_ok = xTaskCreatePinnedToCore(
      face_track_task, "face_track", FACE_TRACK_TASK_STACK_SIZE, NULL,
      FACE_TRACK_TASK_PRIORITY, &s_face_track_task_handle, APP_CORE_NET);
  if (track_ok != pdPASS) {
    s_face_track_task_handle = NULL;
    ESP_LOGW(TAG, "Face tracking task not started");
  }

  ESP_LOGI(TAG, "Installing UVC host driver");
  const uvc_host_driver_config_t uvc_driver_config = {
      .driver_task_stack_size = UVC_DRIVER_TASK_STACK_SIZE,
      .driver_task_priority = UVC_DRIVER_TASK_PRIORITY,
      .xCoreID = APP_CORE_USB,
      .create_background_task = true,
      .event_cb = uvc_driver_event_callback,
      .user_ctx = NULL,
  };
  ESP_ERROR_CHECK(uvc_host_install(&uvc_driver_config));

  ESP_LOGI(TAG, "HOST Type-A / external hub: UVC camera + FTDI U2D2 (Dynamixel)");
  ESP_LOGI(TAG, "Audio: ES8311 Aux-in (Sirena) → energy VAD capture; speaker is playback only");
  ESP_LOGI(
      TAG,
      "Open / in a browser on your camera's IP address (check 'wifi status')");

  /* Boot sequence complete: greet with Hello-home after the Wi-Fi clip. */
  xTaskCreatePinnedToCore(hello_home_task, "hello_home", 4096, NULL, 4, NULL,
                          APP_CORE_NET);
}
```

## 13.9 `main/nino_eye.c` / `nino_eye.h` — complete files

`nino_eye_set_state()` refuses other tasks while the GPIO48 hub soak owns the displays.

### `main/nino_eye.h` — complete file (120 lines)

```c
#pragma once

#include <stdbool.h>

#ifdef __cplusplus
extern "C" {
#endif

/*
 * NINO eye animation engine.
 *
 * States rendered on dual eye panels (SSD1351 OLED or ST7735 TFT): idle +
 * animated emotions + med capsule + static RGB565 emoji bitmaps
 * (jai_bhalaiah … bigsmile). State changes are instant and non-blocking: the
 * running animation switches on its next frame.
 *
 * Integration:
 *   1) nino_display_init();  // bring up the displays (once; Kconfig selects panel)
 *   2) nino_eye_begin();     // spawns the animator task, returns immediately
 *   3) call any per-emotion trigger (or nino_eye_apply_expression) from any task.
 */
typedef enum {
    NINO_EYE_IDLE = 0,
    NINO_EYE_HAPPY,
    NINO_EYE_TIRED,
    NINO_EYE_THINKING,
    NINO_EYE_CURIOUS_QUIZ,
    NINO_EYE_SAD,
    NINO_EYE_SURPRISED,
    NINO_EYE_LISTENING,
    NINO_EYE_RECALLING,
    NINO_EYE_MAD,
    NINO_EYE_MED,
    NINO_EYE_JAI_BHALAIAH,
    NINO_EYE_SMILE,
    NINO_EYE_SPARKLE,
    NINO_EYE_PENCIL,
    NINO_EYE_RADIO,
    NINO_EYE_TV,
    NINO_EYE_BULB,
    NINO_EYE_ROBOT,
    NINO_EYE_BIGSMILE,
    NINO_EYE_STATE_COUNT,
} nino_eye_state_t;

/** Alias kept for older call sites / server tags. */
#define NINO_EYE_TWINKLE NINO_EYE_SPARKLE

void nino_eye_begin(void);

/** Break out of the current animation loop and redraw (e.g. after SPI CS reclaim). */
void nino_eye_restart_current(void);

void nino_eye_set_state(nino_eye_state_t state);
nino_eye_state_t nino_eye_get_state(void);

/** Parse a console token / line: state names (prefer names over digits). */
bool nino_eye_apply_command(const char *line);

/**
 * Map a lowercase expression name (as sent by the PC server, e.g. "sad",
 * "happy", "curious", "recalling", "sparkle", …) to a state. Returns
 * NINO_EYE_STATE_COUNT if the name is unknown / NULL / empty.
 */
nino_eye_state_t nino_eye_state_from_name(const char *name);

/** Reverse lookup for logs / console (returns "?" if unknown). */
const char *nino_eye_state_to_name(nino_eye_state_t state);

/**
 * Apply a server expression tag: shows the matching emotion, or returns the
 * eyes to idle when @p name is NULL/empty/unknown. Mirrors the server contract
 * where a missing eye_expression key means "stay idle for this reply".
 */
void nino_eye_apply_expression(const char *name);

/**
 * Demo-only faster idle blink pace (shorter open-hold before blink).
 * Normal idle (~5.6 s/cycle) is unchanged when disabled. Enable around the
 * UK IFA demo cue timeline so short idle gaps can still show a blink.
 */
void nino_eye_set_demo_idle_pace(bool enabled);

/* ---- Per-emotion triggers ---- */
void nino_eye_idle(void);
void nino_eye_happy(void);
void nino_eye_tired(void);
void nino_eye_thinking(void);
void nino_eye_curious(void);
void nino_eye_sad(void);
void nino_eye_surprised(void);
void nino_eye_listening(void);
void nino_eye_recalling(void);
void nino_eye_mad(void);
/** Static slanted red/white capsule pill — shown while a medical reminder plays. */
void nino_eye_med(void);
/** 🔥 fire emoji bitmap (jai Bhalaiah). */
void nino_eye_jai_bhalaiah(void);
/** 😊 WhatsApp-style smile bitmap. */
void nino_eye_smile(void);
/** ✨ sparkle bitmap. */
void nino_eye_sparkle(void);
/** Alias for nino_eye_sparkle(). */
void nino_eye_twinkle(void);
/** ✏️ pencil emoji bitmap. */
void nino_eye_pencil(void);
/** 📻 radio emoji bitmap. */
void nino_eye_radio(void);
/** 📺 TV emoji bitmap. */
void nino_eye_tv(void);
/** 💡 bulb emoji bitmap. */
void nino_eye_bulb(void);
/** 🤖 robot emoji bitmap. */
void nino_eye_robot(void);
/** 😄 open-mouth big smile bitmap. */
void nino_eye_bigsmile(void);

#ifdef __cplusplus
}
#endif
```

### `main/nino_eye.c` — complete file (2717 lines)

```c
#include "nino_eye.h"

#include <ctype.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <math.h>
#include "battery_endurance.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_rom_sys.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "nino_display.h"
#include "sdkconfig.h"
#if CONFIG_NINO_EYE_DISPLAY_TFT
#include "tft_neutral.h"
#endif
#include "fire_emoji.h"
#include "smile_emoji.h"
#include "sparkle_emoji.h"
#include "pencil_emoji.h"
#include "radio_emoji.h"
#include "tv_emoji.h"
#include "bulb_emoji.h"
#include "robot_emoji.h"
#include "bigsmile_emoji.h"

static const char *TAG = "nino_eye";

/* One panel per eye — 128x96 OLED or 128x128 TFT (see nino_display.h). */
#define LOGICAL_WIDTH   OLED_WIDTH
#define LOGICAL_HEIGHT  OLED_HEIGHT
#define EYE_CX          (LOGICAL_WIDTH / 2)
/* Same downward shift on both panels — keeps the eye at a similar vertical ratio. */
#define NINO_VOFFSET    8
#define EYE_CY          (LOGICAL_HEIGHT / 2 + NINO_VOFFSET)
/* Legacy draw-time trim (0 = off); prefer NINO_VOFFSET for vertical centering. */
#define NINO_VSHIFT     0

/* Only this central region may be drawn/erased (eye, heart, blink). The rest of
 * the screen is never touched after boot — matches "only the oval changes". */
#define EYE_CLIP_HALF_W   46
#define EYE_CLIP_Y0       (EYE_CY - 52)
#define EYE_CLIP_Y1       (EYE_CY + 44)

/*
 * Two independent clocks (do not conflate them):
 *
 * 1) Animation FPS — how often we rewrite GDDRAM during motion sweeps.
 *    Locked to ~30.3 FPS (33 ms) to match default phone video capture so the
 *    camera does not sample mid-blink against a 100+ FPS software beat.
 * 2) Panel scan FPS — SSD1351 continuously refreshes the glass from GDDRAM
 *    (CLOCKDIV in ssd1351.c). SPI writes only update RAM; there is no
 *    display stop / refresh command after each image. Between animation
 *    frames we simply wait; the panel keeps showing the last RAM contents.
 *
 * Motion frames are composed in RAM; only changed pixels are SPI-written
 * (delta spans), and both OLEDs latch the same bits together (broadcast CS).
 */
#define NINO_EYE_FRAME_MS   33
#define BLINK_FRAME_MS      NINO_EYE_FRAME_MS
#define BLINK_CLOSE_STEP    2
#define MAX_GAZE_POINTS 5

/* Jetson neutral animation (spi_render.py) — 128×128 TFT values; scaled for OLED. */
#if CONFIG_NINO_EYE_DISPLAY_TFT
#define NEU_MAX_RADIUS          34
#define NEU_LOOK_DIST           28
#else
#define NEU_MAX_RADIUS          26
#define NEU_LOOK_DIST           21
#endif
#define NEU_HOLD_OPEN_MS        1600
#define NEU_SHUTTER_STEP_MS     9
#define NEU_WHITE_MS            480
#define NEU_DIAMETER_STEP_MS    11
#define NEU_SHUTTER_STEP_PX     4
#define NEU_DIAMETER_STEP_PX    2

/* 1 = cycle all states for testing. 0 = hold current state (loops forever). */
#define DEMO_CYCLE          0

/* 1 = show an orientation test (TOP bar + top-left marker) instead of eyes. */
#define NINO_ORIENT_TEST    0

typedef enum {
    NINO_RENDER_BLINK,
    NINO_RENDER_STATIC,
    NINO_RENDER_HEART,
    NINO_RENDER_MED_CAPSULE,
    NINO_RENDER_FIRE,
    NINO_RENDER_SMILE,
    NINO_RENDER_SPARKLE,
    NINO_RENDER_EMOJI,
    NINO_RENDER_NEUTRAL,
} nino_render_mode_t;

typedef struct {
    const uint16_t *pixels;
    int w;
    int h;
} nino_emoji_bmp_t;

typedef struct {
    nino_render_mode_t mode;
    int rx;
    int ry;
    int top;
    int bottom;
    int hold_ms;
    int closed_hold_ms;
    int blink_step;
    int blink_ms;
    int state_ms;
    int gaze_offsets[MAX_GAZE_POINTS];
    int gaze_count;
    int heart_min_scale;
    int heart_max_scale;
    int heart_frame_ms;
    uint8_t eye_r;       /* emotion eye colour on white background */
    uint8_t eye_g;
    uint8_t eye_b;
} nino_state_profile_t;

static volatile nino_eye_state_t s_state = NINO_EYE_IDLE;
static volatile bool s_restart_requested = false;
/* When set, idle uses a shorter open-hold so demo cue gaps can blink. */
static volatile bool s_demo_idle_pace = false;

/* Demo idle: ~0.8 s open + ~0.6 s blink ≈ 1.4 s/cycle (vs ~5.6 s normal). */
#define DEMO_IDLE_HOLD_MS 1600

static const nino_state_profile_t s_profiles[NINO_EYE_STATE_COUNT] = {
#if CONFIG_NINO_EYE_DISPLAY_TFT
    /* Idle on TFT: tft_neutral.c (standalone oval blink, no OLED engine). */
    [NINO_EYE_IDLE] = {
        .mode = NINO_RENDER_BLINK,
        .eye_r = 0, .eye_g = 0, .eye_b = 0,
    },
#else
    /* OLED idle: Jetson neutral loop — hold, shutter close, white flash, diameter
     * open with gaze center → right → center → left → center. */
    [NINO_EYE_IDLE] = {
        .mode = NINO_RENDER_NEUTRAL,
        .rx = NEU_MAX_RADIUS,
        .ry = NEU_MAX_RADIUS,
        .eye_r = 0, .eye_g = 0, .eye_b = 0,
    },
#endif
    /* happy: kept as the single red heart symbol (no eyelid/pupil/blink). */
    [NINO_EYE_HAPPY] = {
        .mode = NINO_RENDER_HEART,
        .state_ms = 900,
        .heart_min_scale = 20,
        .heart_max_scale = 20,
        .heart_frame_ms = 900,
        .eye_r = 255, .eye_g = 40, .eye_b = 70,   /* happy = red heart (only coloured state) */
    },
    /* tired: low eye with heavy lowered lid (bottom sliver visible). Slow blink
     * cadence via hold_ms; sweep paced at NINO_EYE_FRAME_MS for phone capture. */
    [NINO_EYE_TIRED] = {
        .mode = NINO_RENDER_STATIC,
        .rx = 24,
        .ry = 30,
        .top = EYE_CY + 4,
        .bottom = EYE_CY + 30,
        .hold_ms = 4500,
        .closed_hold_ms = 300,
        .blink_step = 2,
        .blink_ms = NINO_EYE_FRAME_MS,
        .state_ms = 4000,
        .eye_r = 0, .eye_g = 0, .eye_b = 0,
    },
    /* thinking: a normal solid eye like idle that slowly rolls around the top
     * (looking up + side to side). No blink. */
    [NINO_EYE_THINKING] = {
        .mode = NINO_RENDER_STATIC,
        .rx = 24,
        .ry = 30,
        .state_ms = 4000,
        .eye_r = 0, .eye_g = 0, .eye_b = 0,
    },
    /* curious: wide enlarged eye that tilts up + to a side and holds, then
     * blinks across to the other side (head-tilt, inquisitive). */
    [NINO_EYE_CURIOUS_QUIZ] = {
        .mode = NINO_RENDER_BLINK,
        .rx = 28,
        .ry = 33,
        .hold_ms = 2500,
        .closed_hold_ms = 120,
        .blink_step = 2,
        .blink_ms = NINO_EYE_FRAME_MS,
        .gaze_offsets = {0},
        .gaze_count = 1,
        .eye_r = 0, .eye_g = 0, .eye_b = 0,
    },
    /* sad: heavy upper lid covering top 40% of eye. Slow ~6 s lidded blink. */
    [NINO_EYE_SAD] = {
        .mode = NINO_RENDER_STATIC,
        .rx = 24,
        .ry = 30,
        .top = EYE_CY - 6,
        .bottom = EYE_CY + 30,
        .hold_ms = 6000,
        .closed_hold_ms = 300,
        .blink_step = 2,
        .blink_ms = NINO_EYE_FRAME_MS,
        .state_ms = 4000,
        .eye_r = 0, .eye_g = 0, .eye_b = 0,
    },
    /* surprised: widest/tallest eye; one fast snap-open on entry, then hold wide
     * (no frantic blink). blink_step tunes snap geometry; pace is NINO_EYE_FRAME_MS. */
    [NINO_EYE_SURPRISED] = {
        .mode = NINO_RENDER_STATIC,
        .rx = 27,
        .ry = 36,
        .hold_ms = 5000,
        .blink_step = 4,
        .blink_ms = NINO_EYE_FRAME_MS,
        .state_ms = 5000,
        .eye_r = 0, .eye_g = 0, .eye_b = 0,
    },
    /* listening: same wide enlarged eye as curious, but centered - it blinks in
     * place (no left/right tilt). ~3 s blink cycle. */
    [NINO_EYE_LISTENING] = {
        .mode = NINO_RENDER_BLINK,
        .rx = 30,
        .ry = 36,
        .hold_ms = 6000,
        .closed_hold_ms = 120,
        .blink_step = 2,
        .blink_ms = NINO_EYE_FRAME_MS,
        .gaze_offsets = {0},
        .gaze_count = 1,
        .eye_r = 0, .eye_g = 0, .eye_b = 0,
    },
    /* recalling: normal soft eye, slow upward memory-gaze path; slow blink when
     * shifting between look-points (calmer than thinking's roll). */
    [NINO_EYE_RECALLING] = {
        .mode = NINO_RENDER_BLINK,
        .rx = 24,
        .ry = 28,
        .hold_ms = 3600,
        .closed_hold_ms = 280,
        .blink_step = 2,
        .blink_ms = NINO_EYE_FRAME_MS,
        .gaze_offsets = {0},
        .gaze_count = 1,
        .eye_r = 0, .eye_g = 0, .eye_b = 0,
    },
    /* mad: idle-size eye that shakes frantically - 3 s fast left<->right, then
     * 2 s fast up<->down, repeating. hold_ms = horizontal phase, state_ms =
     * vertical phase; per-frame pace is NINO_EYE_FRAME_MS (~30 FPS). */
    [NINO_EYE_MAD] = {
        .mode = NINO_RENDER_STATIC,
        .rx = 24,
        .ry = 30,
        .hold_ms = 3000,
        .state_ms = 2000,
        .blink_ms = NINO_EYE_FRAME_MS,
        .eye_r = 0, .eye_g = 0, .eye_b = 0,
    },
    /* med: red/white capsule pill, slanted (45 deg from vertical), static symbol. */
    [NINO_EYE_MED] = {
        .mode = NINO_RENDER_MED_CAPSULE,
        .state_ms = 900,
        .heart_max_scale = 17,   /* half-length of capsule body (reuse field) */
        .eye_r = 255, .eye_g = 0, .eye_b = 0,
    },
    /* jai Bhalaiah: exact fire emoji bitmap, static on black background. */
    [NINO_EYE_JAI_BHALAIAH] = {
        .mode = NINO_RENDER_FIRE,
        .state_ms = 900,
        .eye_r = 255, .eye_g = 110, .eye_b = 0,
    },
    /* smile: exact WhatsApp-style 😊 bitmap, static, a bit smaller than fire. */
    [NINO_EYE_SMILE] = {
        .mode = NINO_RENDER_SMILE,
        .state_ms = 900,
        .eye_r = 255, .eye_g = 200, .eye_b = 0,
    },
    /* sparkle: WhatsApp-style ✨ bitmap, static on black background. */
    [NINO_EYE_SPARKLE] = {
        .mode = NINO_RENDER_SPARKLE,
        .state_ms = 900,
        .eye_r = 255, .eye_g = 220, .eye_b = 40,
    },
    [NINO_EYE_PENCIL] = {
        .mode = NINO_RENDER_EMOJI,
        .state_ms = 900,
        .eye_r = 255, .eye_g = 140, .eye_b = 40,
    },
    [NINO_EYE_RADIO] = {
        .mode = NINO_RENDER_EMOJI,
        .state_ms = 900,
        .eye_r = 120, .eye_g = 140, .eye_b = 160,
    },
    [NINO_EYE_TV] = {
        .mode = NINO_RENDER_EMOJI,
        .state_ms = 900,
        .eye_r = 100, .eye_g = 160, .eye_b = 220,
    },
    [NINO_EYE_BULB] = {
        .mode = NINO_RENDER_EMOJI,
        .state_ms = 900,
        .eye_r = 255, .eye_g = 220, .eye_b = 40,
    },
    [NINO_EYE_ROBOT] = {
        .mode = NINO_RENDER_EMOJI,
        .state_ms = 900,
        .eye_r = 80, .eye_g = 180, .eye_b = 220,
    },
    [NINO_EYE_BIGSMILE] = {
        .mode = NINO_RENDER_EMOJI,
        .state_ms = 900,
        .eye_r = 255, .eye_g = 200, .eye_b = 0,
    },
};

static const nino_emoji_bmp_t s_emoji_pencil   = { s_pencil_emoji,   PENCIL_EMOJI_W,   PENCIL_EMOJI_H };
static const nino_emoji_bmp_t s_emoji_radio    = { s_radio_emoji,    RADIO_EMOJI_W,    RADIO_EMOJI_H };
static const nino_emoji_bmp_t s_emoji_tv       = { s_tv_emoji,       TV_EMOJI_W,       TV_EMOJI_H };
static const nino_emoji_bmp_t s_emoji_bulb     = { s_bulb_emoji,     BULB_EMOJI_W,     BULB_EMOJI_H };
static const nino_emoji_bmp_t s_emoji_robot    = { s_robot_emoji,    ROBOT_EMOJI_W,    ROBOT_EMOJI_H };
static const nino_emoji_bmp_t s_emoji_bigsmile = { s_bigsmile_emoji, BIGSMILE_EMOJI_W, BIGSMILE_EMOJI_H };

/* Background is a warm-blue white (not pure 255,255,255 — that reads pink on
 * one panel and blue on the other). Eyes stay black. */
static uint16_t s_eye_color = 0x0000;

static uint16_t color_bg(void)
{
#if CONFIG_NINO_EYE_DISPLAY_TFT
    return ssd1351_color(255, 255, 255);
#else
    return ssd1351_color(225, 236, 255);
#endif
}

/* Jetson neutral uses pure white background during the blink cycle. */
static uint16_t color_neu_white(void)
{
    return ssd1351_color(255, 255, 255);
}

static uint16_t color_eye(void)
{
    return s_eye_color;
}

static uint16_t color_red(void)
{
    return ssd1351_color(255, 0, 0);
}

static uint16_t color_capsule_body(void)
{
    return ssd1351_color(210, 210, 210);
}

static uint16_t color_capsule_outline(void)
{
    return ssd1351_color(40, 40, 40);
}

static void set_eye_color(const nino_state_profile_t *profile)
{
    s_eye_color = ssd1351_color(profile->eye_r, profile->eye_g, profile->eye_b);
}

static int ellipse_half_width(int rx, int ry, int dy)
{
    int dy2 = dy * dy;
    int ry2 = ry * ry;
    if (dy2 > ry2) {
        return -1;
    }

    int64_t target = (int64_t)rx * rx * (ry2 - dy2);
    int dx = 0;
    while ((int64_t)(dx + 1) * (dx + 1) * ry2 <= target) {
        dx++;
    }
    return dx;
}

/*
 * Dirty-rectangle tracking. AABB alone is not enough for present: blitting the
 * whole box rewrites unchanged background pixels inside it, and this Waveshare
 * SSD1351 flashes when background is rewritten (phone flicker up close).
 * Present uses a second "what's on the glass" buffer and SPI-writes only
 * pixels that actually changed (horizontal delta spans).
 */
static int s_dirty_x0 = LOGICAL_WIDTH;
static int s_dirty_y0 = LOGICAL_HEIGHT;
static int s_dirty_x1 = -1;
static int s_dirty_y1 = -1;

/* Compose buffer + mirror of GDDRAM contents after the last successful present. */
static uint16_t *s_fb = NULL;
static uint16_t *s_fb_hw = NULL;
static bool s_fb_batch = false;

static void dirty_reset(void)
{
    s_dirty_x0 = LOGICAL_WIDTH;
    s_dirty_y0 = LOGICAL_HEIGHT;
    s_dirty_x1 = -1;
    s_dirty_y1 = -1;
}

static void dirty_add(int x0, int y0, int x1, int y1)
{
    if (x0 < s_dirty_x0) s_dirty_x0 = x0;
    if (y0 < s_dirty_y0) s_dirty_y0 = y0;
    if (x1 > s_dirty_x1) s_dirty_x1 = x1;
    if (y1 > s_dirty_y1) s_dirty_y1 = y1;
}

static bool fb_init(void)
{
    if (s_fb != NULL && s_fb_hw != NULL) {
        return true;
    }
    const size_t bytes = (size_t)LOGICAL_WIDTH * (size_t)LOGICAL_HEIGHT * sizeof(uint16_t);
    if (s_fb == NULL) {
        s_fb = (uint16_t *)heap_caps_malloc(bytes, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
        if (s_fb == NULL) {
            s_fb = (uint16_t *)malloc(bytes);
        }
    }
    if (s_fb_hw == NULL) {
        s_fb_hw = (uint16_t *)heap_caps_malloc(bytes, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
        if (s_fb_hw == NULL) {
            s_fb_hw = (uint16_t *)malloc(bytes);
        }
    }
    if (s_fb == NULL || s_fb_hw == NULL) {
        ESP_LOGE(TAG, "eye framebuffer alloc failed (%u bytes x2)", (unsigned)bytes);
        return false;
    }
    memset(s_fb, 0, bytes);
    memset(s_fb_hw, 0, bytes);
    ESP_LOGI(TAG, "eye framebuffer ready %dx%d (~%u FPS, delta-span present)",
             LOGICAL_WIDTH, LOGICAL_HEIGHT, (unsigned)(1000 / NINO_EYE_FRAME_MS));
    return true;
}

static void fb_present_full(void)
{
    if (s_fb == NULL) {
        return;
    }
    ssd1351_draw_bitmap(0, 0, LOGICAL_WIDTH, LOGICAL_HEIGHT, s_fb);
    if (s_fb_hw != NULL) {
        memcpy(s_fb_hw, s_fb,
               (size_t)LOGICAL_WIDTH * (size_t)LOGICAL_HEIGHT * sizeof(uint16_t));
    }
    dirty_reset();
}

/*
 * SPI only pixels that differ from what is already on the glass. Unchanged
 * background is never rewritten — that is what stopped the close-range flash.
 */
static void fb_present(void)
{
    if (s_fb == NULL || s_fb_hw == NULL) {
        return;
    }
    if (s_dirty_x1 < s_dirty_x0 || s_dirty_y1 < s_dirty_y0) {
        return;
    }

    int x0 = s_dirty_x0;
    int y0 = s_dirty_y0;
    int x1 = s_dirty_x1;
    int y1 = s_dirty_y1;
    if (x0 < 0) {
        x0 = 0;
    }
    if (y0 < 0) {
        y0 = 0;
    }
    if (x1 >= LOGICAL_WIDTH) {
        x1 = LOGICAL_WIDTH - 1;
    }
    if (y1 >= LOGICAL_HEIGHT) {
        y1 = LOGICAL_HEIGHT - 1;
    }
    if (x1 < x0 || y1 < y0) {
        dirty_reset();
        return;
    }

    for (int y = y0; y <= y1; y++) {
        uint16_t *row = &s_fb[(size_t)y * (size_t)LOGICAL_WIDTH];
        uint16_t *hw = &s_fb_hw[(size_t)y * (size_t)LOGICAL_WIDTH];
        int x = x0;
        while (x <= x1) {
            while (x <= x1 && row[x] == hw[x]) {
                x++;
            }
            if (x > x1) {
                break;
            }
            const int xs = x;
            while (x <= x1 && row[x] != hw[x]) {
                x++;
            }
            const int xe = x - 1;
            const int w = xe - xs + 1;
            ssd1351_draw_bitmap(xs, y, w, 1, &row[xs]);
            memcpy(&hw[xs], &row[xs], (size_t)w * sizeof(uint16_t));
        }
    }
    dirty_reset();
}

static void fb_batch_begin(void)
{
    s_fb_batch = true;
    dirty_reset();
}

static void fb_batch_end(void)
{
    s_fb_batch = false;
    fb_present();
}

static void fb_hw_note_span(int x, int y, int width, uint16_t color)
{
    if (s_fb_hw == NULL || width <= 0) {
        return;
    }
    uint16_t *row = &s_fb_hw[(size_t)y * (size_t)LOGICAL_WIDTH + (size_t)x];
    for (int i = 0; i < width; i++) {
        row[i] = color;
    }
}

static void draw_landscape_hline(int x, int y, int width, uint16_t color)
{
    int logical_y = y;

    if (logical_y < EYE_CLIP_Y0 || logical_y > EYE_CLIP_Y1 || width <= 0) {
        return;
    }

    const int clip_x0 = EYE_CX - EYE_CLIP_HALF_W;
    const int clip_x1 = EYE_CX + EYE_CLIP_HALF_W;
    if (x + width <= clip_x0 || x > clip_x1) {
        return;
    }
    if (x < clip_x0) {
        width -= (clip_x0 - x);
        x = clip_x0;
    }
    if (x + width - 1 > clip_x1) {
        width = clip_x1 - x + 1;
    }
    if (width <= 0) {
        return;
    }

    y -= NINO_VSHIFT;
    if (y < 0 || y >= LOGICAL_HEIGHT) {
        return;
    }

    if (x < 0) {
        width += x;
        x = 0;
    }
    if (x + width > LOGICAL_WIDTH) {
        width = LOGICAL_WIDTH - x;
    }
    if (width <= 0) {
        return;
    }

    /* Always keep the shadow buffer in sync with what we intend to show. */
    if (s_fb != NULL) {
        uint16_t *row = &s_fb[(size_t)y * (size_t)LOGICAL_WIDTH + (size_t)x];
        for (int i = 0; i < width; i++) {
            row[i] = color;
        }
    }

    /* Dirty uses the same coords as the framebuffer / SPI window. */
    dirty_add(x, y, x + width - 1, y);

    /* During a batch with a shadow buffer, defer SPI until fb_batch_end(). */
    if (!s_fb_batch || s_fb == NULL) {
        ssd1351_fill_rect(x, y, width, 1, color);
        fb_hw_note_span(x, y, width, color);
    }
}

#if NINO_ORIENT_TEST
static void draw_landscape_rect(int x, int y, int width, int height, uint16_t color)
{
    for (int row = 0; row < height; row++) {
        draw_landscape_hline(x, y + row, width, color);
    }
}
#endif

static void clear_screen(uint16_t color)
{
    if (s_fb != NULL) {
        const size_t n = (size_t)LOGICAL_WIDTH * (size_t)LOGICAL_HEIGHT;
        for (size_t i = 0; i < n; i++) {
            s_fb[i] = color;
        }
        /* Full clear must rewrite every pixel once (boot / state reset only). */
        fb_present_full();
    } else {
        ssd1351_fill_screen(color);
        dirty_reset();
    }
}

/*
 * Remember the EXACT shape currently painted, so when the next state begins it
 * can un-draw that shape along its own outline (writing background over only
 * the pixels that were the eye) instead of erasing a white rectangle. Erasing a
 * rectangle re-writes background pixels that were already white, and on this
 * OLED freshly-written white reads slightly different from held white -> the
 * "window". Un-drawing by shape never touches the untouched background.
 */
typedef enum { PREV_NONE, PREV_ELLIPSE, PREV_HEART, PREV_BLOB, PREV_CAPSULE, PREV_FIRE, PREV_SMILE, PREV_SPARKLE, PREV_EMOJI } prev_kind_t;
static prev_kind_t s_prev_kind = PREV_NONE;
static int s_prev_cx, s_prev_rx, s_prev_ry, s_prev_top, s_prev_bottom;
static int s_prev_blob_cy;
static int s_prev_heart_cx, s_prev_heart_cy, s_prev_heart_scale;
static int s_prev_cap_cx, s_prev_cap_cy, s_prev_cap_half_len, s_prev_cap_radius;
static int s_prev_fire_cx, s_prev_fire_cy;
static int s_prev_smile_cx, s_prev_smile_cy;
static int s_prev_sparkle_cx, s_prev_sparkle_cy;
static int s_prev_emoji_cx, s_prev_emoji_cy;
static const nino_emoji_bmp_t *s_prev_emoji_bmp;

static void remember_ellipse(int cx, int rx, int ry, int top, int bottom)
{
    s_prev_kind = PREV_ELLIPSE;
    s_prev_cx = cx;
    s_prev_rx = rx;
    s_prev_ry = ry;
    s_prev_top = top;
    s_prev_bottom = bottom;
}

static void remember_heart(int cx, int cy, int scale)
{
    s_prev_kind = PREV_HEART;
    s_prev_heart_cx = cx;
    s_prev_heart_cy = cy;
    s_prev_heart_scale = scale;
}

static void remember_capsule(int cx, int cy, int half_len, int radius)
{
    s_prev_kind = PREV_CAPSULE;
    s_prev_cap_cx = cx;
    s_prev_cap_cy = cy;
    s_prev_cap_half_len = half_len;
    s_prev_cap_radius = radius;
}

static void remember_fire(int cx, int cy)
{
    s_prev_kind = PREV_FIRE;
    s_prev_fire_cx = cx;
    s_prev_fire_cy = cy;
}

static void remember_smile(int cx, int cy)
{
    s_prev_kind = PREV_SMILE;
    s_prev_smile_cx = cx;
    s_prev_smile_cy = cy;
}

static void remember_sparkle(int cx, int cy)
{
    s_prev_kind = PREV_SPARKLE;
    s_prev_sparkle_cx = cx;
    s_prev_sparkle_cy = cy;
}

static void remember_emoji(int cx, int cy, const nino_emoji_bmp_t *bmp)
{
    s_prev_kind = PREV_EMOJI;
    s_prev_emoji_cx = cx;
    s_prev_emoji_cy = cy;
    s_prev_emoji_bmp = bmp;
}

/* Blob = a solid eye drawn at an arbitrary center (cx, cy), e.g. the thinking
 * eye that is shifted/rolled around. Erasing fills that ellipse with bg. */
static void remember_blob(int cx, int cy, int rx, int ry)
{
    s_prev_kind = PREV_BLOB;
    s_prev_cx = cx;
    s_prev_blob_cy = cy;
    s_prev_rx = rx;
    s_prev_ry = ry;
}

static void erase_eye_rows(int center_x, int rx, int ry, int top, int bottom)
{
    if (top < 0) {
        top = 0;
    }
    if (bottom >= LOGICAL_HEIGHT) {
        bottom = LOGICAL_HEIGHT - 1;
    }
    if (top > bottom) {
        return;
    }

    /* Erase EXACTLY the eye footprint (identical columns to draw_eye_rows, no
     * margin). We only flip black eye pixels back to white and never re-touch
     * the surrounding white background, so the static background never flashes
     * (no "window"). */
    for (int y = top; y <= bottom; y++) {
        int dx = ellipse_half_width(rx, ry, y - EYE_CY);
        if (dx < 0) {
            continue;
        }
        draw_landscape_hline(center_x - dx, y, (dx * 2) + 1, color_bg());
    }
}

static void draw_eye_rows(int center_x, int rx, int ry, int top, int bottom, uint16_t fill)
{
    if (top < 0) {
        top = 0;
    }
    if (bottom >= LOGICAL_HEIGHT) {
        bottom = LOGICAL_HEIGHT - 1;
    }
    if (top > bottom) {
        return;
    }

    for (int y = top; y <= bottom; y++) {
        int dx = ellipse_half_width(rx, ry, y - EYE_CY);
        if (dx < 0) {
            continue;
        }
        draw_landscape_hline(center_x - dx, y, (dx * 2) + 1, fill);
    }
}

static void draw_full_eye(int center_x, int rx, int ry)
{
    /* The previous shape was already un-drawn on entry, so just draw the eye;
     * no white rectangle box is painted. */
    draw_eye_rows(center_x, rx, ry, EYE_CY - ry, EYE_CY + ry, color_eye());
    remember_ellipse(center_x, rx, ry, EYE_CY - ry, EYE_CY + ry);
}

/* Filled ellipse centered at an arbitrary (cx, cy). */
static void fill_ellipse(int cx, int cy, int rx, int ry, uint16_t color)
{
    for (int dy = -ry; dy <= ry; dy++) {
        int dx = ellipse_half_width(rx, ry, dy);
        if (dx < 0) {
            continue;
        }
        draw_landscape_hline(cx - dx, cy + dy, (dx * 2) + 1, color);
    }
}

/* Draw rows [top, bottom] of an ellipse centered at an arbitrary (cx, cy),
 * exact footprint (used for blinks at off-center positions). */
static void draw_blob_rows(int cx, int cy, int rx, int ry, int top, int bottom, uint16_t fill)
{
    if (top < 0) {
        top = 0;
    }
    if (bottom >= LOGICAL_HEIGHT) {
        bottom = LOGICAL_HEIGHT - 1;
    }
    for (int y = top; y <= bottom; y++) {
        int dx = ellipse_half_width(rx, ry, y - cy);
        if (dx < 0) {
            continue;
        }
        draw_landscape_hline(cx - dx, y, (dx * 2) + 1, fill);
    }
}

static bool heart_pixel(int lx, int ly, int cx, int cy, int scale)
{
    int x = lx - cx;
    int y = ly - cy;
    int lobe_radius = (scale * 7) / 12;
    int lobe_dx = scale / 2;
    int lobe_y = -scale / 3;

    int left_dx = x + lobe_dx;
    int right_dx = x - lobe_dx;
    int lobe_dy = y - lobe_y;
    bool in_left_lobe = (left_dx * left_dx) + (lobe_dy * lobe_dy) <= lobe_radius * lobe_radius;
    bool in_right_lobe = (right_dx * right_dx) + (lobe_dy * lobe_dy) <= lobe_radius * lobe_radius;

    if (y < -(scale / 5)) {
        return in_left_lobe || in_right_lobe;
    }

    float fx = x / (float)scale;
    float fy = (cy - ly) / (float)scale;
    float a = (fx * fx) + (fy * fy) - 1.0f;
    bool in_smooth_point = ((a * a * a) - (fx * fx * fy * fy * fy)) <= 0.0f;

    return in_left_lobe || in_right_lobe || in_smooth_point;
}

static void draw_heart(int cx, int cy, int scale, uint16_t color)
{
    int x0 = cx - (scale * 2);
    int x1 = cx + (scale * 2);
    int y0 = cy - scale - 2;
    int y1 = cy + scale + 4;
    if (x0 < 0) {
        x0 = 0;
    }
    if (x1 >= LOGICAL_WIDTH) {
        x1 = LOGICAL_WIDTH - 1;
    }
    if (y0 < 0) {
        y0 = 0;
    }
    if (y1 >= LOGICAL_HEIGHT) {
        y1 = LOGICAL_HEIGHT - 1;
    }

    for (int y = y0; y <= y1; y++) {
        int span_start = -1;
        for (int x = x0; x <= x1; x++) {
            bool in_heart = heart_pixel(x, y, cx, cy, scale);
            if (in_heart && span_start < 0) {
                span_start = x;
            } else if (!in_heart && span_start >= 0) {
                draw_landscape_hline(span_start, y, x - span_start, color);
                span_start = -1;
            }
        }

        if (span_start >= 0) {
            draw_landscape_hline(span_start, y, x1 - span_start + 1, color);
        }
    }
}

/* Slanted capsule pill (red cap on +u end, gray body with red beads on -u end).
 * half_len = half of total capsule length along axis; radius = cap radius.
 * Slant ~30 deg from vertical, red cap toward upper-right (matches reference). */
#define MED_CAPSULE_SLANT_DEG  45

static float capsule_dist_local(float u, float v, float half_len, float radius)
{
    float body = half_len - radius;
    if (body < 0.0f) {
        body = 0.0f;
    }
    if (u < -body) {
        float du = u + body;
        return sqrtf((du * du) + (v * v));
    }
    if (u > body) {
        float du = u - body;
        return sqrtf((du * du) + (v * v));
    }
    return fabsf(v);
}

static bool capsule_bead_at(float u, float v)
{
    static const struct { float u; float v; } beads[] = {
        {-7.0f,  2.0f},
        {-4.0f, -2.5f},
        {-1.0f,  3.0f},
        { 2.0f,  0.5f},
        {-5.0f,  4.0f},
    };
    for (size_t i = 0; i < sizeof(beads) / sizeof(beads[0]); i++) {
        float du = u - beads[i].u;
        float dv = v - beads[i].v;
        if ((du * du) + (dv * dv) <= 3.5f) {
            return true;
        }
    }
    return false;
}

static uint16_t capsule_pixel_color(float u, float v, float half_len, float radius, bool erase)
{
    float dist = capsule_dist_local(u, v, half_len, radius);
    if (dist > radius + 0.5f) {
        return 0;
    }
    if (erase) {
        return color_bg();
    }
    if (dist > radius - 1.1f) {
        return color_capsule_outline();
    }
    if (u > 0.0f) {
        return color_red();
    }
    if (capsule_bead_at(u, v)) {
        return color_red();
    }
    return color_capsule_body();
}

static void draw_capsule(int cx, int cy, int half_len, int radius)
{
    const float slant = (float)MED_CAPSULE_SLANT_DEG * (3.14159265f / 180.0f);
    /* Axis tilts upper-right: 30 deg from vertical. */
    const float cos_a = sinf(slant);
    const float sin_a = -cosf(slant);
    const float flen = (float)half_len;
    const float fr = (float)radius;

    int x0 = cx - half_len - radius - 4;
    int x1 = cx + half_len + radius + 4;
    int y0 = cy - half_len - radius - 4;
    int y1 = cy + half_len + radius + 4;
    if (x0 < 0) {
        x0 = 0;
    }
    if (x1 >= LOGICAL_WIDTH) {
        x1 = LOGICAL_WIDTH - 1;
    }
    if (y0 < 0) {
        y0 = 0;
    }
    if (y1 >= LOGICAL_HEIGHT) {
        y1 = LOGICAL_HEIGHT - 1;
    }

    for (int y = y0; y <= y1; y++) {
        int span_start = -1;
        uint16_t span_color = 0;
        for (int x = x0; x <= x1; x++) {
            float dx = (float)(x - cx);
            float dy = (float)(y - cy);
            float u = (dx * cos_a) + (dy * sin_a);
            float v = (-dx * sin_a) + (dy * cos_a);
            uint16_t pix = capsule_pixel_color(u, v, flen, fr, false);
            if (pix != 0) {
                if (span_start < 0) {
                    span_start = x;
                    span_color = pix;
                } else if (pix != span_color) {
                    draw_landscape_hline(span_start, y, x - span_start, span_color);
                    span_start = x;
                    span_color = pix;
                }
            } else if (span_start >= 0) {
                draw_landscape_hline(span_start, y, x - span_start, span_color);
                span_start = -1;
            }
        }
        if (span_start >= 0) {
            draw_landscape_hline(span_start, y, x1 - span_start + 1, span_color);
        }
    }
}

static void erase_capsule(int cx, int cy, int half_len, int radius)
{
    const float slant = (float)MED_CAPSULE_SLANT_DEG * (3.14159265f / 180.0f);
    const float cos_a = sinf(slant);
    const float sin_a = -cosf(slant);
    const float flen = (float)half_len;
    const float fr = (float)radius;

    int x0 = cx - half_len - radius - 4;
    int x1 = cx + half_len + radius + 4;
    int y0 = cy - half_len - radius - 4;
    int y1 = cy + half_len + radius + 4;
    if (x0 < 0) {
        x0 = 0;
    }
    if (x1 >= LOGICAL_WIDTH) {
        x1 = LOGICAL_WIDTH - 1;
    }
    if (y0 < 0) {
        y0 = 0;
    }
    if (y1 >= LOGICAL_HEIGHT) {
        y1 = LOGICAL_HEIGHT - 1;
    }

    for (int y = y0; y <= y1; y++) {
        int span_start = -1;
        for (int x = x0; x <= x1; x++) {
            float dx = (float)(x - cx);
            float dy = (float)(y - cy);
            float u = (dx * cos_a) + (dy * sin_a);
            float v = (-dx * sin_a) + (dy * cos_a);
            bool inside = capsule_dist_local(u, v, flen, fr) <= fr + 0.5f;
            if (inside && span_start < 0) {
                span_start = x;
            } else if (!inside && span_start >= 0) {
                draw_landscape_hline(span_start, y, x - span_start, color_bg());
                span_start = -1;
            }
        }
        if (span_start >= 0) {
            draw_landscape_hline(span_start, y, x1 - span_start + 1, color_bg());
        }
    }
}

/*
 * Fire emoji (jai Bhalaiah): exact bitmap from the reference 🔥 image.
 * Drawn static on the white background; black/transparent pixels are skipped.
 * (cx, cy) is the center of the bitmap.
 */
static void draw_fire(int cx, int cy)
{
    const int x0 = cx - (FIRE_EMOJI_W / 2);
    const int y0 = cy - (FIRE_EMOJI_H / 2);

    for (int row = 0; row < FIRE_EMOJI_H; row++) {
        int y = y0 + row;
        if (y < 0 || y >= LOGICAL_HEIGHT) {
            continue;
        }
        int span_start = -1;
        uint16_t span_color = 0;
        for (int col = 0; col < FIRE_EMOJI_W; col++) {
            uint16_t pix = s_fire_emoji[(row * FIRE_EMOJI_W) + col];
            int x = x0 + col;
            if (pix != 0 && x >= 0 && x < LOGICAL_WIDTH) {
                if (span_start < 0) {
                    span_start = x;
                    span_color = pix;
                } else if (pix != span_color) {
                    draw_landscape_hline(span_start, y, x - span_start, span_color);
                    span_start = x;
                    span_color = pix;
                }
            } else if (span_start >= 0) {
                draw_landscape_hline(span_start, y, x - span_start, span_color);
                span_start = -1;
            }
        }
        if (span_start >= 0) {
            int end = x0 + FIRE_EMOJI_W;
            if (end > LOGICAL_WIDTH) {
                end = LOGICAL_WIDTH;
            }
            draw_landscape_hline(span_start, y, end - span_start, span_color);
        }
    }
}

static void erase_fire(int cx, int cy)
{
    const int x0 = cx - (FIRE_EMOJI_W / 2);
    const int y0 = cy - (FIRE_EMOJI_H / 2);

    for (int row = 0; row < FIRE_EMOJI_H; row++) {
        int y = y0 + row;
        if (y < 0 || y >= LOGICAL_HEIGHT) {
            continue;
        }
        int span_start = -1;
        for (int col = 0; col < FIRE_EMOJI_W; col++) {
            uint16_t pix = s_fire_emoji[(row * FIRE_EMOJI_W) + col];
            int x = x0 + col;
            if (pix != 0 && x >= 0 && x < LOGICAL_WIDTH) {
                if (span_start < 0) {
                    span_start = x;
                }
            } else if (span_start >= 0) {
                draw_landscape_hline(span_start, y, x - span_start, color_bg());
                span_start = -1;
            }
        }
        if (span_start >= 0) {
            int end = x0 + FIRE_EMOJI_W;
            if (end > LOGICAL_WIDTH) {
                end = LOGICAL_WIDTH;
            }
            draw_landscape_hline(span_start, y, end - span_start, color_bg());
        }
    }
}

/*
 * Smile emoji: exact WhatsApp-style 😊 bitmap (smaller than fire).
 * Drawn static on the white background; transparent (0) pixels are skipped.
 */
static void draw_smile(int cx, int cy)
{
    const int x0 = cx - (SMILE_EMOJI_W / 2);
    const int y0 = cy - (SMILE_EMOJI_H / 2);

    for (int row = 0; row < SMILE_EMOJI_H; row++) {
        int y = y0 + row;
        if (y < 0 || y >= LOGICAL_HEIGHT) {
            continue;
        }
        int span_start = -1;
        uint16_t span_color = 0;
        for (int col = 0; col < SMILE_EMOJI_W; col++) {
            uint16_t pix = s_smile_emoji[(row * SMILE_EMOJI_W) + col];
            int x = x0 + col;
            if (pix != 0 && x >= 0 && x < LOGICAL_WIDTH) {
                if (span_start < 0) {
                    span_start = x;
                    span_color = pix;
                } else if (pix != span_color) {
                    draw_landscape_hline(span_start, y, x - span_start, span_color);
                    span_start = x;
                    span_color = pix;
                }
            } else if (span_start >= 0) {
                draw_landscape_hline(span_start, y, x - span_start, span_color);
                span_start = -1;
            }
        }
        if (span_start >= 0) {
            int end = x0 + SMILE_EMOJI_W;
            if (end > LOGICAL_WIDTH) {
                end = LOGICAL_WIDTH;
            }
            draw_landscape_hline(span_start, y, end - span_start, span_color);
        }
    }
}

static void erase_smile(int cx, int cy)
{
    const int x0 = cx - (SMILE_EMOJI_W / 2);
    const int y0 = cy - (SMILE_EMOJI_H / 2);

    for (int row = 0; row < SMILE_EMOJI_H; row++) {
        int y = y0 + row;
        if (y < 0 || y >= LOGICAL_HEIGHT) {
            continue;
        }
        int span_start = -1;
        for (int col = 0; col < SMILE_EMOJI_W; col++) {
            uint16_t pix = s_smile_emoji[(row * SMILE_EMOJI_W) + col];
            int x = x0 + col;
            if (pix != 0 && x >= 0 && x < LOGICAL_WIDTH) {
                if (span_start < 0) {
                    span_start = x;
                }
            } else if (span_start >= 0) {
                draw_landscape_hline(span_start, y, x - span_start, color_bg());
                span_start = -1;
            }
        }
        if (span_start >= 0) {
            int end = x0 + SMILE_EMOJI_W;
            if (end > LOGICAL_WIDTH) {
                end = LOGICAL_WIDTH;
            }
            draw_landscape_hline(span_start, y, end - span_start, color_bg());
        }
    }
}

/*
 * Sparkle emoji: WhatsApp-style ✨ bitmap.
 * Drawn static on the white background; transparent (0) pixels are skipped.
 */
static void draw_sparkle(int cx, int cy)
{
    const int x0 = cx - (SPARKLE_EMOJI_W / 2);
    const int y0 = cy - (SPARKLE_EMOJI_H / 2);

    for (int row = 0; row < SPARKLE_EMOJI_H; row++) {
        int y = y0 + row;
        if (y < 0 || y >= LOGICAL_HEIGHT) {
            continue;
        }
        int span_start = -1;
        uint16_t span_color = 0;
        for (int col = 0; col < SPARKLE_EMOJI_W; col++) {
            uint16_t pix = s_sparkle_emoji[(row * SPARKLE_EMOJI_W) + col];
            int x = x0 + col;
            if (pix != 0 && x >= 0 && x < LOGICAL_WIDTH) {
                if (span_start < 0) {
                    span_start = x;
                    span_color = pix;
                } else if (pix != span_color) {
                    draw_landscape_hline(span_start, y, x - span_start, span_color);
                    span_start = x;
                    span_color = pix;
                }
            } else if (span_start >= 0) {
                draw_landscape_hline(span_start, y, x - span_start, span_color);
                span_start = -1;
            }
        }
        if (span_start >= 0) {
            int end = x0 + SPARKLE_EMOJI_W;
            if (end > LOGICAL_WIDTH) {
                end = LOGICAL_WIDTH;
            }
            draw_landscape_hline(span_start, y, end - span_start, span_color);
        }
    }
}

static void erase_sparkle(int cx, int cy)
{
    const int x0 = cx - (SPARKLE_EMOJI_W / 2);
    const int y0 = cy - (SPARKLE_EMOJI_H / 2);

    for (int row = 0; row < SPARKLE_EMOJI_H; row++) {
        int y = y0 + row;
        if (y < 0 || y >= LOGICAL_HEIGHT) {
            continue;
        }
        int span_start = -1;
        for (int col = 0; col < SPARKLE_EMOJI_W; col++) {
            uint16_t pix = s_sparkle_emoji[(row * SPARKLE_EMOJI_W) + col];
            int x = x0 + col;
            if (pix != 0 && x >= 0 && x < LOGICAL_WIDTH) {
                if (span_start < 0) {
                    span_start = x;
                }
            } else if (span_start >= 0) {
                draw_landscape_hline(span_start, y, x - span_start, color_bg());
                span_start = -1;
            }
        }
        if (span_start >= 0) {
            int end = x0 + SPARKLE_EMOJI_W;
            if (end > LOGICAL_WIDTH) {
                end = LOGICAL_WIDTH;
            }
            draw_landscape_hline(span_start, y, end - span_start, color_bg());
        }
    }
}

/* Generic RGB565 emoji bitmap (0 = transparent), same path as smile/sparkle. */
static void draw_emoji_bmp(int cx, int cy, const nino_emoji_bmp_t *bmp)
{
    const int x0 = cx - (bmp->w / 2);
    const int y0 = cy - (bmp->h / 2);

    for (int row = 0; row < bmp->h; row++) {
        int y = y0 + row;
        if (y < 0 || y >= LOGICAL_HEIGHT) {
            continue;
        }
        int span_start = -1;
        uint16_t span_color = 0;
        for (int col = 0; col < bmp->w; col++) {
            uint16_t pix = bmp->pixels[(row * bmp->w) + col];
            int x = x0 + col;
            if (pix != 0 && x >= 0 && x < LOGICAL_WIDTH) {
                if (span_start < 0) {
                    span_start = x;
                    span_color = pix;
                } else if (pix != span_color) {
                    draw_landscape_hline(span_start, y, x - span_start, span_color);
                    span_start = x;
                    span_color = pix;
                }
            } else if (span_start >= 0) {
                draw_landscape_hline(span_start, y, x - span_start, span_color);
                span_start = -1;
            }
        }
        if (span_start >= 0) {
            int end = x0 + bmp->w;
            if (end > LOGICAL_WIDTH) {
                end = LOGICAL_WIDTH;
            }
            draw_landscape_hline(span_start, y, end - span_start, span_color);
        }
    }
}

static void erase_emoji_bmp(int cx, int cy, const nino_emoji_bmp_t *bmp)
{
    const int x0 = cx - (bmp->w / 2);
    const int y0 = cy - (bmp->h / 2);

    for (int row = 0; row < bmp->h; row++) {
        int y = y0 + row;
        if (y < 0 || y >= LOGICAL_HEIGHT) {
            continue;
        }
        int span_start = -1;
        for (int col = 0; col < bmp->w; col++) {
            uint16_t pix = bmp->pixels[(row * bmp->w) + col];
            int x = x0 + col;
            if (pix != 0 && x >= 0 && x < LOGICAL_WIDTH) {
                if (span_start < 0) {
                    span_start = x;
                }
            } else if (span_start >= 0) {
                draw_landscape_hline(span_start, y, x - span_start, color_bg());
                span_start = -1;
            }
        }
        if (span_start >= 0) {
            int end = x0 + bmp->w;
            if (end > LOGICAL_WIDTH) {
                end = LOGICAL_WIDTH;
            }
            draw_landscape_hline(span_start, y, end - span_start, color_bg());
        }
    }
}

/*
 * Un-draw the previous glyph by shape (background colour over its footprint).
 * Full-screen clear is avoided — on TFT that looked like a pop; on OLED it
 * caused a white flash. Blink frames compose in the shadow FB, then one SPI
 * present per animation step (~30 FPS).
 */
static void erase_prev_eye(void)
{
    fb_batch_begin();
    switch (s_prev_kind) {
    case PREV_ELLIPSE:
        draw_eye_rows(s_prev_cx, s_prev_rx, s_prev_ry, s_prev_top, s_prev_bottom, color_bg());
        break;
    case PREV_HEART:
        draw_heart(s_prev_heart_cx, s_prev_heart_cy, s_prev_heart_scale, color_bg());
        break;
    case PREV_BLOB:
        fill_ellipse(s_prev_cx, s_prev_blob_cy, s_prev_rx, s_prev_ry, color_bg());
        break;
    case PREV_CAPSULE:
        erase_capsule(s_prev_cap_cx, s_prev_cap_cy, s_prev_cap_half_len, s_prev_cap_radius);
        break;
    case PREV_FIRE:
        erase_fire(s_prev_fire_cx, s_prev_fire_cy);
        break;
    case PREV_SMILE:
        erase_smile(s_prev_smile_cx, s_prev_smile_cy);
        break;
    case PREV_SPARKLE:
        erase_sparkle(s_prev_sparkle_cx, s_prev_sparkle_cy);
        break;
    case PREV_EMOJI:
        if (s_prev_emoji_bmp != NULL) {
            erase_emoji_bmp(s_prev_emoji_cx, s_prev_emoji_cy, s_prev_emoji_bmp);
        }
        break;
    default:
        break;
    }
    fb_batch_end();
    s_prev_kind = PREV_NONE;
    s_prev_emoji_bmp = NULL;
}

static nino_eye_state_t current_state(void)
{
    return (nino_eye_state_t)s_state;
}

/*
 * Deadline-based wait so SPI draw time does not stack on top of frame_ms
 * (which made motion look low-FPS / jittery on phone video). Long waits still
 * yield to FreeRTOS; sub-ms remainders spin with esp_rom_delay_us.
 */
static bool delay_ms_interruptible(int total_ms, nino_eye_state_t expected)
{
    if (total_ms <= 0) {
        if (s_restart_requested) {
            s_restart_requested = false;
            return false;
        }
        return current_state() == expected;
    }

    const int64_t deadline_us = esp_timer_get_time() + (int64_t)total_ms * 1000LL;
    while (esp_timer_get_time() < deadline_us) {
        if (s_restart_requested) {
            s_restart_requested = false;
            return false;
        }
        if (current_state() != expected) {
            return false;
        }

        const int64_t remaining_us = deadline_us - esp_timer_get_time();
        if (remaining_us <= 0) {
            break;
        }

        /* Yield in ~1 ms slices so other tasks / the idle task keep running. */
        if (remaining_us > 1000) {
            int slice_ms = (int)(remaining_us / 1000);
            if (slice_ms > 25) {
                slice_ms = 25;
            }
            TickType_t ticks = pdMS_TO_TICKS(slice_ms);
            if (ticks == 0) {
                ticks = 1;
            }
            vTaskDelay(ticks);
        } else {
            esp_rom_delay_us((uint32_t)remaining_us);
            break;
        }
    }

    return current_state() == expected;
}

/* Wait the remainder of a frame budget after drawing (true FPS = 1000/frame_ms). */
static bool pace_frame_interruptible(int64_t frame_start_us, int frame_ms,
                                     nino_eye_state_t expected)
{
    const int64_t budget_us = (int64_t)frame_ms * 1000LL;
    const int64_t elapsed_us = esp_timer_get_time() - frame_start_us;
    const int64_t remain_us = budget_us - elapsed_us;
    if (remain_us <= 0) {
        if (s_restart_requested) {
            s_restart_requested = false;
            return false;
        }
        return current_state() == expected;
    }
    return delay_ms_interruptible((int)((remain_us + 999) / 1000), expected);
}

static void fill_rect_rows(int x, int y, int w, int h, uint16_t color)
{
    if (h <= 0 || w <= 0) {
        return;
    }
    for (int row = 0; row < h; row++) {
        draw_landscape_hline(x, y + row, w, color);
    }
}

static float ease_out_cubic(float t)
{
    if (t <= 0.0f) {
        return 0.0f;
    }
    if (t >= 1.0f) {
        return 1.0f;
    }
    const float u = 1.0f - t;
    return 1.0f - (u * u * u);
}

typedef struct {
    int seg;
    int phase;
    int64_t mark_ms;
    int shutter_y;
    int diameter_d;
    int from_x;
    int to_x;
} neutral_anim_t;

static neutral_anim_t s_neu;

static int64_t neu_now_ms(void)
{
    return esp_timer_get_time() / 1000;
}

static void neu_load_seg(int seg)
{
    static const int k_from[4] = {0, NEU_LOOK_DIST, 0, -NEU_LOOK_DIST};
    static const int k_to[4] = {NEU_LOOK_DIST, 0, -NEU_LOOK_DIST, 0};
    seg &= 3;
    s_neu.seg = seg;
    s_neu.from_x = k_from[seg];
    s_neu.to_x = k_to[seg];
}

static void neu_reset(void)
{
    s_neu.seg = 0;
    neu_load_seg(0);
    s_neu.phase = 0;
    s_neu.mark_ms = neu_now_ms();
    s_neu.shutter_y = 0;
    s_neu.diameter_d = 0;
}

static void neu_paint_shutter_close(int shutter_y, int look_x)
{
    const int cx = EYE_CX + look_x;
    const int cy = EYE_CY;
    const int r = NEU_MAX_RADIUS;
    const uint16_t white = color_neu_white();

    fill_rect_rows(EYE_CX - EYE_CLIP_HALF_W, EYE_CLIP_Y0,
                   EYE_CLIP_HALF_W * 2 + 1, EYE_CLIP_Y1 - EYE_CLIP_Y0 + 1, white);
    fill_ellipse(cx, cy, r, r, color_eye());
    const int shutter_top = cy + r - shutter_y;
    if (shutter_top < LOGICAL_HEIGHT) {
        const int h = LOGICAL_HEIGHT - shutter_top;
        if (h > 0) {
            fill_rect_rows(0, shutter_top, LOGICAL_WIDTH, h, white);
        }
    }
    remember_blob(cx, cy, r, r);
}

static void neu_paint_diameter_open(int open_dist, int look_x)
{
    const int cx = EYE_CX + look_x;
    const int cy = EYE_CY;
    const int r = NEU_MAX_RADIUS;
    const uint16_t white = color_neu_white();

    fill_rect_rows(EYE_CX - EYE_CLIP_HALF_W, EYE_CLIP_Y0,
                   EYE_CLIP_HALF_W * 2 + 1, EYE_CLIP_Y1 - EYE_CLIP_Y0 + 1, white);
    fill_ellipse(cx, cy, r, r, color_eye());
    if (open_dist > 0) {
        const int top_h = cy - open_dist;
        if (top_h > 0) {
            fill_rect_rows(0, 0, LOGICAL_WIDTH, top_h, white);
        }
        const int bot_y = cy + open_dist;
        if (bot_y < LOGICAL_HEIGHT) {
            fill_rect_rows(0, bot_y, LOGICAL_WIDTH, LOGICAL_HEIGHT - bot_y, white);
        }
    }
    remember_blob(cx, cy, r, r);
}

/*
 * Jetson neutral: hold open → shutter close → white flash → diameter open at
 * next gaze (center → right → center → left → center …).
 */
static void run_neutral_nino(const nino_state_profile_t *profile, nino_eye_state_t expected)
{
    (void)profile;
    neu_reset();
    fb_batch_begin();
    erase_prev_eye();
    neu_paint_diameter_open(NEU_MAX_RADIUS, s_neu.from_x);
    fb_batch_end();

    while (current_state() == expected) {
        if (s_restart_requested) {
            s_restart_requested = false;
            neu_reset();
            fb_batch_begin();
            neu_paint_diameter_open(NEU_MAX_RADIUS, s_neu.from_x);
            fb_batch_end();
            continue;
        }

        const int64_t now = neu_now_ms();

        if (s_neu.phase == 0) {
            if (now - s_neu.mark_ms >= NEU_HOLD_OPEN_MS) {
                s_neu.phase = 1;
                s_neu.shutter_y = 0;
                s_neu.mark_ms = now;
            } else if (!delay_ms_interruptible(20, expected)) {
                return;
            }
        } else if (s_neu.phase == 1) {
            if (now - s_neu.mark_ms >= NEU_SHUTTER_STEP_MS) {
                s_neu.mark_ms = now;
                fb_batch_begin();
                neu_paint_shutter_close(s_neu.shutter_y, s_neu.from_x);
                fb_batch_end();
                s_neu.shutter_y += NEU_SHUTTER_STEP_PX;
                if (s_neu.shutter_y > NEU_MAX_RADIUS * 2) {
                    fb_batch_begin();
                    fill_rect_rows(EYE_CX - EYE_CLIP_HALF_W, EYE_CLIP_Y0,
                                   EYE_CLIP_HALF_W * 2 + 1,
                                   EYE_CLIP_Y1 - EYE_CLIP_Y0 + 1, color_neu_white());
                    fb_batch_end();
                    s_prev_kind = PREV_NONE;
                    s_neu.phase = 2;
                    s_neu.mark_ms = now;
                }
            } else if (!delay_ms_interruptible(5, expected)) {
                return;
            }
        } else if (s_neu.phase == 2) {
            if (now - s_neu.mark_ms >= NEU_WHITE_MS) {
                s_neu.phase = 3;
                s_neu.diameter_d = 0;
                s_neu.mark_ms = now;
            } else if (!delay_ms_interruptible(20, expected)) {
                return;
            }
        } else {
            if (now - s_neu.mark_ms >= NEU_DIAMETER_STEP_MS) {
                s_neu.mark_ms = now;
                const float t =
                    fminf(1.0f, (float)s_neu.diameter_d / (float)NEU_MAX_RADIUS);
                const int dd = (int)(ease_out_cubic(t) * (float)NEU_MAX_RADIUS);
                fb_batch_begin();
                neu_paint_diameter_open(dd, s_neu.to_x);
                fb_batch_end();
                s_neu.diameter_d += NEU_DIAMETER_STEP_PX;
                if (s_neu.diameter_d > NEU_MAX_RADIUS) {
                    s_neu.seg = (s_neu.seg + 1) & 3;
                    neu_load_seg(s_neu.seg);
                    s_neu.phase = 0;
                    s_neu.mark_ms = now;
                }
            } else if (!delay_ms_interruptible(5, expected)) {
                return;
            }
        }
    }
}

static int blink_eye_to_position(const nino_state_profile_t *profile,
                                 int current_x,
                                 int next_x,
                                 nino_eye_state_t expected)
{
    int rx = profile->rx;
    int ry = profile->ry;
    int step = profile->blink_step > 0 ? profile->blink_step : BLINK_CLOSE_STEP;
    if (step <= 0) {
        step = 1;
    }
    const int frame_ms = NINO_EYE_FRAME_MS;

    int open_hold_ms = profile->hold_ms / 2;
    if (expected == NINO_EYE_IDLE && s_demo_idle_pace) {
        open_hold_ms = DEMO_IDLE_HOLD_MS / 2;
    }
    if (!delay_ms_interruptible(open_hold_ms, expected)) {
        return current_x;
    }

    /* Geometric close: erase oval rows from top/bottom toward center. Each
     * step is composed in RAM then blitted once (~30 FPS, camera-safe). */
    int previous_open = ry;
    for (int open = ry - step; open >= 0; open -= step) {
        if (current_state() != expected) {
            return current_x;
        }
        const int64_t frame_start = esp_timer_get_time();
        fb_batch_begin();
        erase_eye_rows(current_x, rx, ry, EYE_CY - previous_open, EYE_CY - open - 1);
        erase_eye_rows(current_x, rx, ry, EYE_CY + open + 1, EYE_CY + previous_open);
        fb_batch_end();
        previous_open = open;
        if (!pace_frame_interruptible(frame_start, frame_ms, expected)) {
            return current_x;
        }
    }

    fb_batch_begin();
    erase_eye_rows(current_x, rx, ry, EYE_CY - ry, EYE_CY + ry);
    fb_batch_end();
    if (!delay_ms_interruptible(profile->closed_hold_ms, expected)) {
        return current_x;
    }

    int previous_reveal = 0;
    fb_batch_begin();
    draw_eye_rows(next_x, rx, ry, EYE_CY, EYE_CY, color_eye());
    fb_batch_end();
    for (int open = step; open <= ry; open += step) {
        if (current_state() != expected) {
            return next_x;
        }
        const int64_t frame_start = esp_timer_get_time();
        fb_batch_begin();
        draw_eye_rows(next_x, rx, ry, EYE_CY - open, EYE_CY - previous_reveal - 1, color_eye());
        draw_eye_rows(next_x, rx, ry, EYE_CY + previous_reveal + 1, EYE_CY + open, color_eye());
        fb_batch_end();
        previous_reveal = open;
        if (!pace_frame_interruptible(frame_start, frame_ms, expected)) {
            return next_x;
        }
    }

    if (previous_reveal < ry) {
        fb_batch_begin();
        draw_eye_rows(next_x, rx, ry, EYE_CY - ry, EYE_CY - previous_reveal - 1, color_eye());
        draw_eye_rows(next_x, rx, ry, EYE_CY + previous_reveal + 1, EYE_CY + ry, color_eye());
        fb_batch_end();
    }

    remember_ellipse(next_x, rx, ry, EYE_CY - ry, EYE_CY + ry);
    return next_x;
}

#if CONFIG_NINO_EYE_DISPLAY_TFT
static bool tft_idle_should_run(void)
{
    if (s_restart_requested) {
        s_restart_requested = false;
        return false;
    }
    return current_state() == NINO_EYE_IDLE;
}
#endif

static void draw_static_eye(const nino_state_profile_t *profile)
{
    fb_batch_begin();
    erase_prev_eye();
    draw_eye_rows(EYE_CX, profile->rx, profile->ry, profile->top, profile->bottom, color_eye());
    fb_batch_end();
    remember_ellipse(EYE_CX, profile->rx, profile->ry, profile->top, profile->bottom);
}

static void run_blink_profile_once(const nino_state_profile_t *profile, nino_eye_state_t expected)
{
    int center_x = EYE_CX;

    /* Un-draw the previous state's shape (only its own pixels), then draw the
     * eye ONCE. After that we only blink/move incrementally, so there is no
     * full-eye erase-and-redraw flash on every cycle. */
    fb_batch_begin();
    erase_prev_eye();
    draw_full_eye(center_x, profile->rx, profile->ry);
    fb_batch_end();

    while (current_state() == expected) {
        for (int i = 0; i < profile->gaze_count; i++) {
            if (current_state() != expected) {
                return;
            }
            center_x = blink_eye_to_position(profile,
                                             center_x,
                                             EYE_CX + profile->gaze_offsets[i],
                                             expected);
        }
    }
}

static void run_static_profile_once(const nino_state_profile_t *profile, nino_eye_state_t expected)
{
    /* Draw once, then hold without re-erasing/redrawing so the eye stays
     * perfectly steady (no periodic white flash that looks like a blink). */
    draw_static_eye(profile);
    while (delay_ms_interruptible(profile->state_ms, expected)) {
        /* still in this state: keep holding the same steady image */
    }
}

/*
 * Tired blink: the eye sits in its lidded window [top, bottom] (top 30% already
 * covered). On each cycle the lids close from both edges toward the window's
 * mid row, hold briefly, then reopen back to the lidded window. All erases use
 * the exact eye footprint, so the background is never re-touched (no "window").
 */
static void run_lidded_blink(const nino_state_profile_t *profile, nino_eye_state_t expected)
{
    const int rx = profile->rx;
    const int ry = profile->ry;
    const int top = profile->top;
    const int bottom = profile->bottom;
    const int cy = (top + bottom) / 2;
    int step = profile->blink_step > 0 ? profile->blink_step : BLINK_CLOSE_STEP;
    if (step <= 0) {
        step = 1;
    }
    const int frame_ms = NINO_EYE_FRAME_MS;

    fb_batch_begin();
    erase_prev_eye();
    draw_eye_rows(EYE_CX, rx, ry, top, bottom, color_eye());
    fb_batch_end();
    remember_ellipse(EYE_CX, rx, ry, top, bottom);

    while (current_state() == expected) {
        if (!delay_ms_interruptible(profile->hold_ms, expected)) {
            return;
        }

        int cur_top = top;
        int cur_bot = bottom;
        for (int off = step; ; off += step) {
            if (current_state() != expected) {
                return;
            }
            const int64_t frame_start = esp_timer_get_time();
            int new_top = top + off;
            int new_bot = bottom - off;
            if (new_top > cy) {
                new_top = cy;
            }
            if (new_bot < cy) {
                new_bot = cy;
            }
            fb_batch_begin();
            erase_eye_rows(EYE_CX, rx, ry, cur_top, new_top - 1);
            erase_eye_rows(EYE_CX, rx, ry, new_bot + 1, cur_bot);
            fb_batch_end();
            cur_top = new_top;
            cur_bot = new_bot;
            if (!pace_frame_interruptible(frame_start, frame_ms, expected)) {
                return;
            }
            if (new_top >= cy && new_bot <= cy) {
                break;
            }
        }

        fb_batch_begin();
        erase_eye_rows(EYE_CX, rx, ry, top, bottom);
        fb_batch_end();
        if (!delay_ms_interruptible(profile->closed_hold_ms, expected)) {
            return;
        }

        cur_top = cy;
        cur_bot = cy;
        fb_batch_begin();
        draw_eye_rows(EYE_CX, rx, ry, cy, cy, color_eye());
        fb_batch_end();
        for (int off = step; ; off += step) {
            if (current_state() != expected) {
                return;
            }
            const int64_t frame_start = esp_timer_get_time();
            int new_top = cy - off;
            int new_bot = cy + off;
            if (new_top < top) {
                new_top = top;
            }
            if (new_bot > bottom) {
                new_bot = bottom;
            }
            fb_batch_begin();
            draw_eye_rows(EYE_CX, rx, ry, new_top, cur_top - 1, color_eye());
            draw_eye_rows(EYE_CX, rx, ry, cur_bot + 1, new_bot, color_eye());
            fb_batch_end();
            cur_top = new_top;
            cur_bot = new_bot;
            if (!pace_frame_interruptible(frame_start, frame_ms, expected)) {
                return;
            }
            if (new_top <= top && new_bot >= bottom) {
                break;
            }
        }

        fb_batch_begin();
        draw_eye_rows(EYE_CX, rx, ry, top, bottom, color_eye());
        fb_batch_end();
        remember_ellipse(EYE_CX, rx, ry, top, bottom);
    }
}

/*
 * Thinking: a normal solid eye (like idle) that slowly rolls around the top -
 * up-left -> up -> up-right -> up ... - to convey pondering. No blink. The whole
 * eye moves; we erase the old position and draw the new one (both touch only
 * eye-shaped pixels, never the surrounding background).
 */
static void run_thinking_eye(const nino_state_profile_t *profile, nino_eye_state_t expected)
{
    const int rx = profile->rx;
    const int ry = profile->ry;
    /* Gaze sequence (dx, dy) relative to screen center, all shifted well up:
     * centre -> up -> left -> up -> right -> (loop). */
    static const int gx[] = {0,   0,  -14,   0,   14};
    static const int gy[] = {-10, -22, -16, -22, -16};
    const int gaze_n = (int)(sizeof(gx) / sizeof(gx[0]));

    fb_batch_begin();
    erase_prev_eye();
    fb_batch_end();

    int prev_cx = 0, prev_cy = 0;
    bool have_eye = false;
    int i = 0;
    while (current_state() == expected) {
        int ex = EYE_CX + gx[i];
        int ey = EYE_CY + gy[i];
        fb_batch_begin();
        if (have_eye) {
            fill_ellipse(prev_cx, prev_cy, rx, ry, color_bg());
        }
        fill_ellipse(ex, ey, rx, ry, color_eye());
        fb_batch_end();
        remember_blob(ex, ey, rx, ry);
        prev_cx = ex;
        prev_cy = ey;
        have_eye = true;
        i = (i + 1) % gaze_n;
        if (!delay_ms_interruptible(2800, expected)) {
            return;
        }
    }
}

/*
 * Blink that also moves the eye: close at (cx0, cy0), then open at (cx1, cy1).
 * Used to tilt the curious eye from one side to the other during the blink.
 * All erases/draws use the exact eye footprint, so the background is untouched.
 */
static bool blink_move_blob(int cx0, int cy0, int cx1, int cy1, int rx, int ry,
                            int step, int frame_ms, int closed_hold,
                            nino_eye_state_t expected)
{
    int previous_open = ry;
    for (int open = ry - step; open >= 0; open -= step) {
        if (current_state() != expected) {
            return false;
        }
        const int64_t frame_start = esp_timer_get_time();
        fb_batch_begin();
        draw_blob_rows(cx0, cy0, rx, ry, cy0 - previous_open, cy0 - open - 1, color_bg());
        draw_blob_rows(cx0, cy0, rx, ry, cy0 + open + 1, cy0 + previous_open, color_bg());
        fb_batch_end();
        previous_open = open;
        if (!pace_frame_interruptible(frame_start, frame_ms, expected)) {
            return false;
        }
    }

    fb_batch_begin();
    draw_blob_rows(cx0, cy0, rx, ry, cy0 - ry, cy0 + ry, color_bg());
    fb_batch_end();
    if (!delay_ms_interruptible(closed_hold, expected)) {
        return false;
    }

    int previous_reveal = 0;
    fb_batch_begin();
    draw_blob_rows(cx1, cy1, rx, ry, cy1, cy1, color_eye());
    fb_batch_end();
    for (int open = step; open <= ry; open += step) {
        if (current_state() != expected) {
            return false;
        }
        const int64_t frame_start = esp_timer_get_time();
        fb_batch_begin();
        draw_blob_rows(cx1, cy1, rx, ry, cy1 - open, cy1 - previous_reveal - 1, color_eye());
        draw_blob_rows(cx1, cy1, rx, ry, cy1 + previous_reveal + 1, cy1 + open, color_eye());
        fb_batch_end();
        previous_reveal = open;
        if (!pace_frame_interruptible(frame_start, frame_ms, expected)) {
            return false;
        }
    }

    if (previous_reveal < ry) {
        fb_batch_begin();
        draw_blob_rows(cx1, cy1, rx, ry, cy1 - ry, cy1 - previous_reveal - 1, color_eye());
        draw_blob_rows(cx1, cy1, rx, ry, cy1 + previous_reveal + 1, cy1 + ry, color_eye());
        fb_batch_end();
    }

    remember_blob(cx1, cy1, rx, ry);
    return true;
}

/*
 * Curious: a wide, enlarged eye that tilts up-and-to-a-side and holds that
 * inquisitive look, then blinks across to the other side and holds again.
 */
static void run_curious_eye(const nino_state_profile_t *profile, nino_eye_state_t expected)
{
    const int rx = profile->rx;
    const int ry = profile->ry;
    int step = profile->blink_step > 0 ? profile->blink_step : BLINK_CLOSE_STEP;
    if (step <= 0) {
        step = 1;
    }
    const int frame_ms = NINO_EYE_FRAME_MS;

    /* Tilted look-points: up-left then up-right (head-tilt feel). */
    static const int px[] = {-16, 16};
    static const int py[] = {-10, -10};
    const int n = (int)(sizeof(px) / sizeof(px[0]));

    fb_batch_begin();
    erase_prev_eye();
    int i = 0;
    int cx = EYE_CX + px[i];
    int cy = EYE_CY + py[i];
    fill_ellipse(cx, cy, rx, ry, color_eye());
    fb_batch_end();
    remember_blob(cx, cy, rx, ry);

    while (current_state() == expected) {
        if (!delay_ms_interruptible(profile->hold_ms, expected)) {
            return;
        }
        int next = (i + 1) % n;
        int ncx = EYE_CX + px[next];
        int ncy = EYE_CY + py[next];
        if (!blink_move_blob(cx, cy, ncx, ncy, rx, ry, step, frame_ms,
                             profile->closed_hold_ms, expected)) {
            return;
        }
        i = next;
        cx = ncx;
        cy = ncy;
    }
}

static void snap_open_eye(int cx, int rx, int ry, int step, int frame_ms, nino_eye_state_t expected)
{
    int previous_reveal = 0;
    fb_batch_begin();
    draw_eye_rows(cx, rx, ry, EYE_CY, EYE_CY, color_eye());
    fb_batch_end();
    for (int open = step; open <= ry; open += step) {
        if (current_state() != expected) {
            return;
        }
        const int64_t frame_start = esp_timer_get_time();
        fb_batch_begin();
        draw_eye_rows(cx, rx, ry, EYE_CY - open, EYE_CY - previous_reveal - 1, color_eye());
        draw_eye_rows(cx, rx, ry, EYE_CY + previous_reveal + 1, EYE_CY + open, color_eye());
        fb_batch_end();
        previous_reveal = open;
        if (!pace_frame_interruptible(frame_start, frame_ms, expected)) {
            return;
        }
    }
    if (previous_reveal < ry) {
        fb_batch_begin();
        draw_eye_rows(cx, rx, ry, EYE_CY - ry, EYE_CY - previous_reveal - 1, color_eye());
        draw_eye_rows(cx, rx, ry, EYE_CY + previous_reveal + 1, EYE_CY + ry, color_eye());
        fb_batch_end();
    }
}

/*
 * Surprised: snap-open on entry (geometry from blink_step), then hold.
 * Paced at NINO_EYE_FRAME_MS for phone 30 fps capture.
 */
static void run_surprised_eye(const nino_state_profile_t *profile, nino_eye_state_t expected)
{
    const int rx = profile->rx;
    const int ry = profile->ry;
    int step = profile->blink_step > 0 ? profile->blink_step : 8;
    if (step <= 0) {
        step = 1;
    }
    const int frame_ms = NINO_EYE_FRAME_MS;
    const int hold_ms = profile->hold_ms > 0 ? profile->hold_ms : 5000;

    fb_batch_begin();
    erase_prev_eye();
    fb_batch_end();
    snap_open_eye(EYE_CX, rx, ry, step, frame_ms, expected);
    if (current_state() != expected) {
        return;
    }
    remember_ellipse(EYE_CX, rx, ry, EYE_CY - ry, EYE_CY + ry);

    while (delay_ms_interruptible(hold_ms, expected)) {
        /* hold wide open */
    }
}

/*
 * Recalling: softer normal eye drifting upward through memory-gaze points
 * (centre -> up-left -> up -> up-right -> centre). Holds at each point, then
 * a slow blink while shifting to the next — introspective, not frantic.
 */
static void run_recalling_eye(const nino_state_profile_t *profile, nino_eye_state_t expected)
{
    const int rx = profile->rx;
    const int ry = profile->ry;
    int step = profile->blink_step > 0 ? profile->blink_step : 3;
    if (step <= 0) {
        step = 1;
    }
    const int frame_ms = NINO_EYE_FRAME_MS;

    static const int px[] = {0, -12, 0, 12, 0};
    static const int py[] = {-4, -12, -16, -12, -6};
    const int n = (int)(sizeof(px) / sizeof(px[0]));

    fb_batch_begin();
    erase_prev_eye();
    int i = 0;
    int cx = EYE_CX + px[i];
    int cy = EYE_CY + py[i];
    fill_ellipse(cx, cy, rx, ry, color_eye());
    fb_batch_end();
    remember_blob(cx, cy, rx, ry);

    while (current_state() == expected) {
        if (!delay_ms_interruptible(profile->hold_ms, expected)) {
            return;
        }
        int next = (i + 1) % n;
        int ncx = EYE_CX + px[next];
        int ncy = EYE_CY + py[next];
        if (!blink_move_blob(cx, cy, ncx, ncy, rx, ry, step, frame_ms,
                             profile->closed_hold_ms, expected)) {
            return;
        }
        i = next;
        cx = ncx;
        cy = ncy;
    }
}

/*
 * Move an already-drawn eye blob from (ox,oy) to (nx,ny): draw the new ellipse,
 * then erase only the old pixels NOT covered by the new one. The eye never fully
 * disappears (no flash) and only changed pixels are touched. Works for any
 * horizontal/vertical/diagonal move.
 */
static void move_eye_blob(int ox, int oy, int nx, int ny, int rx, int ry)
{
    fb_batch_begin();
    int ytop = ((oy < ny) ? oy : ny) - ry;
    int ybot = ((oy > ny) ? oy : ny) + ry;
    for (int y = ytop; y <= ybot; y++) {
        int nw = ellipse_half_width(rx, ry, y - ny);
        int ow = ellipse_half_width(rx, ry, y - oy);
        if (nw >= 0) {
            draw_landscape_hline(nx - nw, y, (nw * 2) + 1, color_eye());
        }
        if (ow < 0) {
            continue;
        }
        int oxl = ox - ow;
        int oxr = ox + ow;
        if (nw < 0) {
            draw_landscape_hline(oxl, y, (oxr - oxl) + 1, color_bg());
            continue;
        }
        int nxl = nx - nw;
        int nxr = nx + nw;
        if (oxl < nxl) {
            int r = (oxr < nxl - 1) ? oxr : (nxl - 1);
            draw_landscape_hline(oxl, y, (r - oxl) + 1, color_bg());
        }
        if (oxr > nxr) {
            int l = (oxl > nxr + 1) ? oxl : (nxr + 1);
            draw_landscape_hline(l, y, (oxr - l) + 1, color_bg());
        }
    }
    fb_batch_end();
}

/*
 * Mad: idle-size eye that shakes. Phase 1 left<->right for hold_ms, phase 2
 * up<->down for state_ms. Motion paced at NINO_EYE_FRAME_MS (~30 FPS).
 */
static void run_mad_eye(const nino_state_profile_t *profile, nino_eye_state_t expected)
{
    const int rx = profile->rx;
    const int ry = profile->ry;
    const int frame_ms = NINO_EYE_FRAME_MS;
    const int h_amp = 18;   /* horizontal shake amplitude (px from center) */
    const int v_amp = 12;   /* vertical shake amplitude */
    const int h_step = 4;   /* px/frame — smaller steps look smoother at 30 FPS */
    const int v_step = 4;

    fb_batch_begin();
    erase_prev_eye();
    int cx = EYE_CX;
    int cy = EYE_CY;
    fill_ellipse(cx, cy, rx, ry, color_eye());
    fb_batch_end();
    remember_blob(cx, cy, rx, ry);

    while (current_state() == expected) {
        /* Phase 1: fast horizontal shake. */
        int elapsed = 0;
        int target = EYE_CX + h_amp;
        while (elapsed < profile->hold_ms) {
            if (current_state() != expected) {
                return;
            }
            int ncx = cx;
            if (cx < target) {
                ncx = (cx + h_step > target) ? target : cx + h_step;
            } else if (cx > target) {
                ncx = (cx - h_step < target) ? target : cx - h_step;
            }
            if (ncx != cx) {
                const int64_t frame_start = esp_timer_get_time();
                move_eye_blob(cx, cy, ncx, cy, rx, ry);
                cx = ncx;
                remember_blob(cx, cy, rx, ry);
                if (!pace_frame_interruptible(frame_start, frame_ms, expected)) {
                    return;
                }
            } else if (!delay_ms_interruptible(frame_ms, expected)) {
                return;
            }
            if (cx == target) {
                target = (target > EYE_CX) ? (EYE_CX - h_amp) : (EYE_CX + h_amp);
            }
            elapsed += frame_ms;
        }
        if (cx != EYE_CX) {
            move_eye_blob(cx, cy, EYE_CX, cy, rx, ry);
            cx = EYE_CX;
            remember_blob(cx, cy, rx, ry);
        }

        /* Phase 2: fast vertical shake. */
        elapsed = 0;
        int vtarget = EYE_CY + v_amp;
        while (elapsed < profile->state_ms) {
            if (current_state() != expected) {
                return;
            }
            int ncy = cy;
            if (cy < vtarget) {
                ncy = (cy + v_step > vtarget) ? vtarget : cy + v_step;
            } else if (cy > vtarget) {
                ncy = (cy - v_step < vtarget) ? vtarget : cy - v_step;
            }
            if (ncy != cy) {
                const int64_t frame_start = esp_timer_get_time();
                move_eye_blob(cx, cy, cx, ncy, rx, ry);
                cy = ncy;
                remember_blob(cx, cy, rx, ry);
                if (!pace_frame_interruptible(frame_start, frame_ms, expected)) {
                    return;
                }
            } else if (!delay_ms_interruptible(frame_ms, expected)) {
                return;
            }
            if (cy == vtarget) {
                vtarget = (vtarget > EYE_CY) ? (EYE_CY - v_amp) : (EYE_CY + v_amp);
            }
            elapsed += frame_ms;
        }
        if (cy != EYE_CY) {
            move_eye_blob(cx, cy, cx, EYE_CY, rx, ry);
            cy = EYE_CY;
            remember_blob(cx, cy, rx, ry);
        }
    }
}

static void run_heart_profile_once(const nino_state_profile_t *profile, nino_eye_state_t expected)
{
    fb_batch_begin();
    erase_prev_eye();
    /* Heart's pointed bottom reaches lower than its lobes rise, so lift the
     * center well above mid-screen to keep the symbol visually centered. */
    draw_heart(EYE_CX, EYE_CY - 4, profile->heart_max_scale, color_red());
    fb_batch_end();
    remember_heart(EYE_CX, EYE_CY - 4, profile->heart_max_scale);
    while (delay_ms_interruptible(profile->state_ms, expected)) {
        /* Happy is intentionally still: one red symbol, no pulse or flicker. */
    }
}

static void run_med_profile_once(const nino_state_profile_t *profile, nino_eye_state_t expected)
{
    const int half_len = profile->heart_max_scale > 0 ? profile->heart_max_scale : 17;
    const int radius = (half_len * 5) / 8;
    if (radius < 6) {
        return;
    }

    fb_batch_begin();
    erase_prev_eye();
    draw_capsule(EYE_CX, EYE_CY, half_len, radius);
    fb_batch_end();
    remember_capsule(EYE_CX, EYE_CY, half_len, radius);
    while (delay_ms_interruptible(profile->state_ms, expected)) {
        /* Static slanted capsule, like happy. */
    }
}

static void run_jai_bhalaiah_profile_once(const nino_state_profile_t *profile, nino_eye_state_t expected)
{
    const int cx = EYE_CX;
    const int cy = EYE_CY + 2;

    fb_batch_begin();
    erase_prev_eye();
    draw_fire(cx, cy);
    fb_batch_end();
    remember_fire(cx, cy);
    while (delay_ms_interruptible(profile->state_ms, expected)) {
        /* Static fire emoji — exact bitmap from the reference image. */
    }
}

static void run_smile_profile_once(const nino_state_profile_t *profile, nino_eye_state_t expected)
{
    const int cx = EYE_CX;
    const int cy = EYE_CY;

    fb_batch_begin();
    erase_prev_eye();
    draw_smile(cx, cy);
    fb_batch_end();
    remember_smile(cx, cy);
    while (delay_ms_interruptible(profile->state_ms, expected)) {
        /* Static WhatsApp-style smile emoji. */
    }
}

static void run_sparkle_profile_once(const nino_state_profile_t *profile, nino_eye_state_t expected)
{
    const int cx = EYE_CX;
    const int cy = EYE_CY;

    fb_batch_begin();
    erase_prev_eye();
    draw_sparkle(cx, cy);
    fb_batch_end();
    remember_sparkle(cx, cy);
    while (delay_ms_interruptible(profile->state_ms, expected)) {
        /* Static WhatsApp-style sparkle emoji. */
    }
}

static void run_emoji_bmp_profile_once(const nino_state_profile_t *profile, nino_eye_state_t expected,
                                       const nino_emoji_bmp_t *bmp)
{
    const int cx = EYE_CX;
    const int cy = EYE_CY;

    fb_batch_begin();
    erase_prev_eye();
    draw_emoji_bmp(cx, cy, bmp);
    fb_batch_end();
    remember_emoji(cx, cy, bmp);
    while (delay_ms_interruptible(profile->state_ms, expected)) {
        /* Static emoji bitmap. */
    }
}

void nino_eye_set_state(nino_eye_state_t state)
{
    if (state >= NINO_EYE_STATE_COUNT) {
        return;
    }
    if (nino_battery_endurance_owns_actuators() && !nino_battery_endurance_is_self()) {
        return;
    }
    const bool same = (s_state == state);
    s_state = state;
    if (same) {
      /* Re-applying the same emotion must force a redraw (e.g. smile again). */
      s_restart_requested = true;
    }
    ESP_LOGI(TAG, "state set -> %d%s", (int)state, same ? " (restart)" : "");
}

void nino_eye_set_demo_idle_pace(bool enabled)
{
    s_demo_idle_pace = enabled;
    if (current_state() == NINO_EYE_IDLE) {
        /* Restart so the new open-hold is picked up immediately. */
        s_restart_requested = true;
    }
    ESP_LOGI(TAG, "demo idle pace %s", enabled ? "on (~1.4s/cycle)" : "off");
}

void nino_eye_idle(void)      { nino_eye_set_state(NINO_EYE_IDLE); }
void nino_eye_happy(void)     { nino_eye_set_state(NINO_EYE_HAPPY); }
void nino_eye_tired(void)     { nino_eye_set_state(NINO_EYE_TIRED); }
void nino_eye_thinking(void)  { nino_eye_set_state(NINO_EYE_THINKING); }
void nino_eye_curious(void)   { nino_eye_set_state(NINO_EYE_CURIOUS_QUIZ); }
void nino_eye_sad(void)       { nino_eye_set_state(NINO_EYE_SAD); }
void nino_eye_surprised(void) { nino_eye_set_state(NINO_EYE_SURPRISED); }
void nino_eye_listening(void) { nino_eye_set_state(NINO_EYE_LISTENING); }
void nino_eye_recalling(void) { nino_eye_set_state(NINO_EYE_RECALLING); }
void nino_eye_mad(void)       { nino_eye_set_state(NINO_EYE_MAD); }
void nino_eye_med(void)            { nino_eye_set_state(NINO_EYE_MED); }
void nino_eye_jai_bhalaiah(void)   { nino_eye_set_state(NINO_EYE_JAI_BHALAIAH); }
void nino_eye_smile(void)          { nino_eye_set_state(NINO_EYE_SMILE); }
void nino_eye_sparkle(void)        { nino_eye_set_state(NINO_EYE_SPARKLE); }
void nino_eye_twinkle(void)        { nino_eye_set_state(NINO_EYE_SPARKLE); }
void nino_eye_pencil(void)         { nino_eye_set_state(NINO_EYE_PENCIL); }
void nino_eye_radio(void)          { nino_eye_set_state(NINO_EYE_RADIO); }
void nino_eye_tv(void)             { nino_eye_set_state(NINO_EYE_TV); }
void nino_eye_bulb(void)           { nino_eye_set_state(NINO_EYE_BULB); }
void nino_eye_robot(void)          { nino_eye_set_state(NINO_EYE_ROBOT); }
void nino_eye_bigsmile(void)       { nino_eye_set_state(NINO_EYE_BIGSMILE); }

nino_eye_state_t nino_eye_state_from_name(const char *name)
{
    if (name == NULL || name[0] == '\0') {
        return NINO_EYE_STATE_COUNT;
    }

    /* Normalize like apply_command: lowercase + collapse spaces. */
    char token[40];
    size_t len = 0;
    bool last_space = false;
    for (size_t i = 0; name[i] != '\0' && len < sizeof(token) - 1; i++) {
        unsigned char c = (unsigned char)name[i];
        if (isspace(c)) {
            if (len > 0 && !last_space) {
                token[len++] = ' ';
                last_space = true;
            }
        } else {
            token[len++] = (char)tolower(c);
            last_space = false;
        }
    }
    while (len > 0 && token[len - 1] == ' ') {
        len--;
    }
    token[len] = '\0';

    if (strcmp(token, "idle") == 0 || strcmp(token, "neutral") == 0 ||
        strcmp(token, "normal") == 0) {
        return NINO_EYE_IDLE;
    } else if (strcmp(token, "happy") == 0) {
        return NINO_EYE_HAPPY;
    } else if (strcmp(token, "tired") == 0) {
        return NINO_EYE_TIRED;
    } else if (strcmp(token, "thinking") == 0) {
        return NINO_EYE_THINKING;
    } else if (strcmp(token, "curious") == 0 || strcmp(token, "quiz") == 0) {
        return NINO_EYE_CURIOUS_QUIZ;
    } else if (strcmp(token, "sad") == 0) {
        return NINO_EYE_SAD;
    } else if (strcmp(token, "surprised") == 0) {
        return NINO_EYE_SURPRISED;
    } else if (strcmp(token, "listening") == 0) {
        return NINO_EYE_LISTENING;
    } else if (strcmp(token, "recalling") == 0) {
        return NINO_EYE_RECALLING;
    } else if (strcmp(token, "mad") == 0) {
        return NINO_EYE_MAD;
    } else if (strcmp(token, "med") == 0) {
        return NINO_EYE_MED;
    } else if (strcmp(token, "jai bhalaiah") == 0 ||
               strcmp(token, "jai_bhalaiah") == 0 ||
               strcmp(token, "jaibhalaiah") == 0 ||
               strcmp(token, "fire") == 0) {
        return NINO_EYE_JAI_BHALAIAH;
    } else if (strcmp(token, "smile") == 0 || strcmp(token, "smiling") == 0) {
        return NINO_EYE_SMILE;
    } else if (strcmp(token, "sparkle") == 0 || strcmp(token, "sparkles") == 0 ||
               strcmp(token, "twinkle") == 0) {
        return NINO_EYE_SPARKLE;
    } else if (strcmp(token, "pencil") == 0) {
        return NINO_EYE_PENCIL;
    } else if (strcmp(token, "radio") == 0) {
        return NINO_EYE_RADIO;
    } else if (strcmp(token, "tv") == 0 || strcmp(token, "television") == 0) {
        return NINO_EYE_TV;
    } else if (strcmp(token, "bulb") == 0 || strcmp(token, "light") == 0) {
        return NINO_EYE_BULB;
    } else if (strcmp(token, "robot") == 0 || strcmp(token, "bot") == 0) {
        return NINO_EYE_ROBOT;
    } else if (strcmp(token, "bigsmile") == 0 || strcmp(token, "big smile") == 0) {
        return NINO_EYE_BIGSMILE;
    }
    return NINO_EYE_STATE_COUNT;
}

const char *nino_eye_state_to_name(nino_eye_state_t state)
{
    switch (state) {
    case NINO_EYE_IDLE:           return "idle";
    case NINO_EYE_HAPPY:          return "happy";
    case NINO_EYE_TIRED:          return "tired";
    case NINO_EYE_THINKING:       return "thinking";
    case NINO_EYE_CURIOUS_QUIZ:   return "curious";
    case NINO_EYE_SAD:            return "sad";
    case NINO_EYE_SURPRISED:      return "surprised";
    case NINO_EYE_LISTENING:      return "listening";
    case NINO_EYE_RECALLING:      return "recalling";
    case NINO_EYE_MAD:            return "mad";
    case NINO_EYE_MED:            return "med";
    case NINO_EYE_JAI_BHALAIAH:   return "fire";
    case NINO_EYE_SMILE:          return "smile";
    case NINO_EYE_SPARKLE:        return "sparkle";
    case NINO_EYE_PENCIL:         return "pencil";
    case NINO_EYE_RADIO:          return "radio";
    case NINO_EYE_TV:             return "tv";
    case NINO_EYE_BULB:           return "bulb";
    case NINO_EYE_ROBOT:          return "robot";
    case NINO_EYE_BIGSMILE:       return "bigsmile";
    default:                      return "?";
    }
}

void nino_eye_apply_expression(const char *name)
{
    nino_eye_state_t state = nino_eye_state_from_name(name);
    if (state >= NINO_EYE_STATE_COUNT) {
        nino_eye_set_state(NINO_EYE_IDLE);
        return;
    }
    nino_eye_set_state(state);
}

bool nino_eye_apply_command(const char *line)
{
    if (line == NULL) {
        return false;
    }

    while (*line && isspace((unsigned char)*line)) {
        line++;
    }
    if (*line == '\0') {
        return false;
    }

    /* Normalize the whole line to lowercase, collapse spaces, so both
     * "jai Bhalaiah" and "jai_bhalaiah" work. */
    char token[40];
    size_t len = 0;
    bool last_space = false;
    for (size_t i = 0; line[i] != '\0' && len < sizeof(token) - 1; i++) {
        unsigned char c = (unsigned char)line[i];
        if (isspace(c)) {
            if (len > 0 && !last_space) {
                token[len++] = ' ';
                last_space = true;
            }
        } else {
            token[len++] = (char)tolower(c);
            last_space = false;
        }
    }
    while (len > 0 && token[len - 1] == ' ') {
        len--;
    }
    token[len] = '\0';

    /* Single digit 0-9 only covers the first ten states; prefer names. */
    if (token[0] >= '0' && token[0] <= '9' && token[1] == '\0') {
        int value = token[0] - '0';
        if (value >= NINO_EYE_STATE_COUNT) {
            return false;
        }
        nino_eye_set_state((nino_eye_state_t)value);
        return true;
    }

    nino_eye_state_t state = nino_eye_state_from_name(token);
    if (state >= NINO_EYE_STATE_COUNT) {
        return false;
    }
    nino_eye_set_state(state);
    return true;
}

nino_eye_state_t nino_eye_get_state(void)
{
    return current_state();
}

static void run_current_state_once(void)
{
    nino_eye_state_t state = current_state();
    if (state >= NINO_EYE_STATE_COUNT) {
        return;
    }

    const nino_state_profile_t *profile = &s_profiles[state];
    set_eye_color(profile);
    switch (state) {
    case NINO_EYE_HAPPY:
        run_heart_profile_once(profile, state);
        break;
    case NINO_EYE_MED:
        run_med_profile_once(profile, state);
        break;
    case NINO_EYE_JAI_BHALAIAH:
        run_jai_bhalaiah_profile_once(profile, state);
        break;
    case NINO_EYE_SMILE:
        run_smile_profile_once(profile, state);
        break;
    case NINO_EYE_SPARKLE:
        run_sparkle_profile_once(profile, state);
        break;
    case NINO_EYE_PENCIL:
        run_emoji_bmp_profile_once(profile, state, &s_emoji_pencil);
        break;
    case NINO_EYE_RADIO:
        run_emoji_bmp_profile_once(profile, state, &s_emoji_radio);
        break;
    case NINO_EYE_TV:
        run_emoji_bmp_profile_once(profile, state, &s_emoji_tv);
        break;
    case NINO_EYE_BULB:
        run_emoji_bmp_profile_once(profile, state, &s_emoji_bulb);
        break;
    case NINO_EYE_ROBOT:
        run_emoji_bmp_profile_once(profile, state, &s_emoji_robot);
        break;
    case NINO_EYE_BIGSMILE:
        run_emoji_bmp_profile_once(profile, state, &s_emoji_bigsmile);
        break;
    case NINO_EYE_TIRED:
        run_lidded_blink(profile, state);
        break;
    case NINO_EYE_THINKING:
        run_thinking_eye(profile, state);
        break;
    case NINO_EYE_CURIOUS_QUIZ:
        run_curious_eye(profile, state);
        break;
    case NINO_EYE_SAD:
        run_lidded_blink(profile, state);
        break;
    case NINO_EYE_SURPRISED:
        run_surprised_eye(profile, state);
        break;
    case NINO_EYE_RECALLING:
        run_recalling_eye(profile, state);
        break;
    case NINO_EYE_MAD:
        run_mad_eye(profile, state);
        break;
    case NINO_EYE_IDLE:
#if CONFIG_NINO_EYE_DISPLAY_TFT
        tft_neutral_run(tft_idle_should_run);
#else
        if (profile->mode == NINO_RENDER_NEUTRAL) {
            run_neutral_nino(profile, state);
        } else {
            run_blink_profile_once(profile, state);
        }
#endif
        break;
    default:
        if (profile->mode == NINO_RENDER_STATIC) {
            run_static_profile_once(profile, state);
        } else {
            run_blink_profile_once(profile, state);
        }
        break;
    }
}

#if NINO_ORIENT_TEST
static void orientation_test(void)
{
    clear_screen(color_bg());

    /* Whole TOP half lit white (logical y = 0 .. mid). Bottom half stays black. */
    draw_landscape_rect(0, 0, LOGICAL_WIDTH, LOGICAL_HEIGHT / 2, ssd1351_color(255, 255, 255));

    /* Small RED square in the logical TOP-LEFT corner (x = 0, y = 0). */
    draw_landscape_rect(0, 0, 22, 22, ssd1351_color(255, 0, 0));

    while (true) {
        vTaskDelay(pdMS_TO_TICKS(1000));
    }
}
#endif

static void eye_engine_task(void *arg)
{
    (void)arg;
    ESP_LOGI(TAG, "eye engine task started (animation %d ms/frame ≈ %u FPS)",
             NINO_EYE_FRAME_MS, (unsigned)(1000 / NINO_EYE_FRAME_MS));
    if (!fb_init()) {
        ESP_LOGW(TAG, "continuing without framebuffer (row SPI may tear on camera)");
    }
    clear_screen(color_bg());

#if NINO_ORIENT_TEST
    orientation_test();
    return;
#endif

#if DEMO_CYCLE
    while (true) {
        for (nino_eye_state_t state = NINO_EYE_IDLE;
             state < NINO_EYE_STATE_COUNT;
             state = (nino_eye_state_t)(state + 1)) {
            s_state = state;
            run_current_state_once();
        }
    }
#else
    /* Keep whatever state was set before the task started (defaults to
     * NINO_EYE_IDLE) so an early nino_eye_<emotion>() call isn't overridden. */
    ESP_LOGI(TAG, "default state %d (drive via nino_eye_<emotion>() / apply_expression)", (int)s_state);

    while (true) {
        run_current_state_once();
    }
#endif
}

void nino_eye_begin(void)
{
    static bool s_engine_started = false;
    if (s_engine_started) {
        return;
    }
    s_engine_started = true;
    ESP_LOGI(TAG, "Nino eye starting (engine only)");
    xTaskCreate(eye_engine_task, "nino_eye", 8192, NULL, 5, NULL);
}

void nino_eye_restart_current(void)
{
    s_restart_requested = true;
    ESP_LOGI(TAG, "eye animation restart requested");
}
```

## 14. Completeness checklist

| Path | Lines | Bytes |
|------|------:|------:|
| `sdkconfig.defaults` | 35 | 1476 |
| `main/CMakeLists.txt` | 26 | 995 |
| `main/st7735.h` | 95 | 3075 |
| `main/st7735.c` | 442 | 12152 |
| `main/ssd1351.h` | 70 | 2387 |
| `main/ssd1351.c` | 415 | 12351 |
| `main/servo_dxl.h` | 77 | 2761 |
| `main/servo_dxl.c` | 2092 | 72943 |
| `main/battery_adc.h` | 38 | 1010 |
| `main/battery_adc.c` | 500 | 14311 |
| `main/low_battery.wav` | (binary) | 109876 |
| `main/push_buttons.h` | 31 | 1134 |
| `main/push_buttons.c` | 331 | 10566 |
| `main/rgb_led.h` | 53 | 2095 |
| `main/rgb_led.c` | 714 | 20407 |
| `main/audio_playback.h` | 83 | 3289 |
| `main/audio_playback.c` | 673 | 18870 |
| `main/battery_endurance.h` | 37 | 1211 |
| `main/battery_endurance.c` | 404 | 11362 |
| `main/main.c` | 4334 | 139015 |
| `main/nino_eye.h` | 120 | 3790 |
| `main/nino_eye.c` | 2717 | 89770 |

`main/main.c` and `main/nino_eye.c` / `nino_eye.h` are now embedded in full, same as every other source in the table. `main/low_battery.wav` is binary (size listed above).

---

*End of document.*

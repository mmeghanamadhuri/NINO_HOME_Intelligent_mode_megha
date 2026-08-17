# TFT eyes + RGB LED wiring (ESP32-P4)

## 2× 1.44" ST7735 TFT (eyes)

Shared SPI bus; separate chip-select per eye.

| Module pin | ESP32-P4 GPIO | Notes |
|------------|---------------|-------|
| SCK / SCL | **23** | Shared |
| SDA / MOSI | **22** | Shared |
| DC / A0 | **21** | Shared |
| RES / RESET | **20** | Shared |
| BL / LED | **19** | Backlight — or tie LED to 3.3 V |
| CS (left eye) | **32** | Left panel |
| CS (right eye) | **33** | Right panel |
| VCC | **3.3 V** | |
| GND | **GND** | |

Firmware: `main/st7735.c`, `main/st7735.h`, `main/tft_neutral.c`, `main/nino_display.h`  
Menuconfig: **NiNO Eye Displays → ST7735 TFT (1.44" SPI 128x128)**

## RGB LED (common anode)

| LED wire | ESP32-P4 GPIO | Notes |
|----------|---------------|-------|
| Red | **2** | PWM via LEDC |
| Green | **3** | PWM via LEDC |
| Blue / white channel | **4** | PWM via LEDC (use `rgb blue` or `rgb white` for white) |
| Black (common) | **3.3 V** | Common-anode: cathodes on GPIO, common to 3.3 V |

Firmware: `main/rgb_led.c`, `main/rgb_led.h`

Serial console examples:

```text
rgb red 255
rgb green 128
rgb blue 255
rgb white          # all channels on
rgb off
rgb status
```

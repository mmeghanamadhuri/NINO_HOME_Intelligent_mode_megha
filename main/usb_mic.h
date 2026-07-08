#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "esp_err.h"

/**
 * Route FSLS PHY 0 to GPIO header (24/25) before usb_host_install().
 * Call once from app_main when CONFIG_USB4MIC_USB_PHY_ON_HEADER is set.
 */
esp_err_t usb_mic_phy_init_for_header(void);

/** Install UAC driver and start 16 kHz mono capture (USB host must already be installed). */
esp_err_t usb_mic_start(void);

/**
 * Ignore UAC RX on this USB address (call when UVC camera enumerates on the J18 hub).
 * Prevents opening the camera's built-in microphone as the voice mic.
 */
void usb_mic_block_dev_addr(uint8_t dev_addr);

/** Print enumerated USB devices (for `voice status` debugging). */
void usb_mic_print_usb_devices(void);

/** True when UAC is open and live PCM is in the ring buffer. */
bool usb_mic_ready(void);

/** True when UAC device handle is open (may still be waiting for first PCM). */
bool usb_mic_uac_open(void);

/** Print mic addr/VID/PID, peak level, rx chunk count. */
void usb_mic_print_status(void);

/**
 * Read 16 kHz mono int16 PCM from the USB mic ring buffer.
 * @param samples Output buffer.
 * @param sample_count Number of int16 samples (not bytes).
 * @return ESP_OK when all samples were read, ESP_ERR_TIMEOUT if mic not ready / underrun.
 */
esp_err_t usb_mic_read(int16_t *samples, int sample_count);

/** Drop buffered PCM (call before VAD after wake chime). */
void usb_mic_flush(void);

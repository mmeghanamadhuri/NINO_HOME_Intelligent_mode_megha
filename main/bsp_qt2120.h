#pragma once

#include "driver/i2c_master.h"
#include "esp_err.h"
#include <stdbool.h>
#include <stdint.h>


#ifdef __cplusplus
extern "C" {
#endif

/**
 * @brief Initialize the QT2120 touch sensor
 *
 * @return ESP_OK on success, error code otherwise
 */
esp_err_t qt2120_init(void);

/**
 * @brief Initialize the QT2120 touch sensor on an existing I2C bus
 *
 * @param bus_handle I2C bus shared with the board peripherals
 * @return ESP_OK on success, error code otherwise
 */
esp_err_t qt2120_init_with_bus(i2c_master_bus_handle_t bus_handle);

/**
 * @brief Read the status of the touch keys
 *
 * @param keys Bitmask of pressed keys (Bit 0 = Key 0, etc.)
 * @return ESP_OK on success, error code otherwise
 */
esp_err_t qt2120_read_keys(uint8_t *keys);

/**
 * @brief Read the status of all 12 touch keys
 *
 * @param keys Bitmask of pressed keys (Bit 0 = Key 0, ... Bit 11 = Key 11)
 * @return ESP_OK on success, error code otherwise
 */
esp_err_t qt2120_read_keys12(uint16_t *keys);

/**
 * @brief Force the QT2120 to recalibrate its no-touch baseline
 *
 * @return ESP_OK on success, error code otherwise
 */
esp_err_t qt2120_calibrate(void);

/**
 * @brief Configure QT2120 with less sensitive/noise-resistant touch settings
 *
 * @return ESP_OK on success, error code otherwise
 */
esp_err_t qt2120_configure_conservative(void);

/**
 * @brief Check if the QT2120 is connected
 *
 * @return true if connected, false otherwise
 */
bool qt2120_is_connected(void);

/**
 * @brief Log the current status of the touch keys
 */
void qt2120_log_status(void);

#ifdef __cplusplus
}
#endif

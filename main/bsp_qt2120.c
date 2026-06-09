#include "bsp_qt2120.h"
#include "driver/i2c_master.h"
#include "esp_log.h"
#include <string.h>

static const char *TAG = "QT2120";

/* I2C Configuration */
#define QT2120_I2C_ADDR 0x1C

/* Register Addresses */
#define QT2120_REG_CHIP_ID 0x00
#define QT2120_REG_DETECTION 0x02
#define QT2120_REG_KEY_STATUS_0 0x03
#define QT2120_REG_KEY_STATUS_1 0x04
#define QT2120_REG_CALIBRATE 0x06
#define QT2120_REG_DI 0x0B
#define QT2120_REG_TRD 0x0C
#define QT2120_REG_DHT 0x0D
#define QT2120_REG_KEY0_DTHR 0x10

#define QT2120_KEY_COUNT 12
/* QT2120 power-on defaults: DTHR=10, DI=4. Older firmware used 50/10 and missed weak pads. */
#define QT2120_DETECT_THRESHOLD 12
#define QT2120_DETECTION_INTEGRATOR 4

/* Expected Chip ID */
#define QT2120_CHIP_ID_VALUE 0x3E

/* Device handle */
static i2c_master_dev_handle_t qt2120_handle = NULL;

static esp_err_t qt2120_read_reg(uint8_t reg_addr, uint8_t *data) {
  if (qt2120_handle == NULL) {
    return ESP_ERR_INVALID_STATE;
  }
  return i2c_master_transmit_receive(qt2120_handle, &reg_addr, 1, data, 1, -1);
}

static esp_err_t qt2120_write_reg(uint8_t reg_addr, uint8_t data) {
  if (qt2120_handle == NULL) {
    return ESP_ERR_INVALID_STATE;
  }

  uint8_t write_buf[2] = {reg_addr, data};
  return i2c_master_transmit(qt2120_handle, write_buf, sizeof(write_buf), -1);
}

bool qt2120_is_connected(void) {
  uint8_t chip_id = 0;
  if (qt2120_read_reg(QT2120_REG_CHIP_ID, &chip_id) != ESP_OK) {
    return false;
  }
  ESP_LOGI(TAG, "Chip ID: 0x%02X (expected: 0x%02X)", chip_id,
           QT2120_CHIP_ID_VALUE);
  return (chip_id == QT2120_CHIP_ID_VALUE);
}

esp_err_t qt2120_init_with_bus(i2c_master_bus_handle_t bus_handle) {
  if (bus_handle == NULL) {
    ESP_LOGE(TAG, "I2C bus not initialized");
    return ESP_ERR_INVALID_STATE;
  }

  /* Configure I2C device */
  i2c_device_config_t dev_cfg = {
      .dev_addr_length = I2C_ADDR_BIT_LEN_7,
      .device_address = QT2120_I2C_ADDR,
      .scl_speed_hz =
          100000, // QT2120 might prefer slower speed, but 400k often works.
                  // Let's start with 100k safe? Actually, same bus as IMU
                  // (400k). Device config can have different speed but max bus
                  // speed is limited by slowest. Let's try 400000 to match bus.
  };

  esp_err_t ret =
      i2c_master_bus_add_device(bus_handle, &dev_cfg, &qt2120_handle);
  if (ret != ESP_OK) {
    ESP_LOGE(TAG, "Failed to add I2C device: %s", esp_err_to_name(ret));
    return ret;
  }

  /* Check connection */
  if (!qt2120_is_connected()) {
    ESP_LOGE(TAG, "QT2120 not detected at 0x%02X", QT2120_I2C_ADDR);
    return ESP_FAIL;
  }

  // Default configuration is usually sufficient for basic key reading
  // Calibration happens automatically on power-up
  ESP_ERROR_CHECK(qt2120_configure_conservative());

  ESP_LOGI(TAG, "QT2120 initialized successfully");
  return ESP_OK;
}

esp_err_t qt2120_init(void) {
  ESP_LOGE(TAG, "Call qt2120_init_with_bus() with an initialized I2C bus");
  return ESP_ERR_INVALID_STATE;
}

esp_err_t qt2120_read_keys(uint8_t *keys) {
  uint16_t keys12 = 0;
  esp_err_t ret = qt2120_read_keys12(&keys12);
  if (ret != ESP_OK) {
    return ret;
  }

  *keys = (uint8_t)(keys12 & 0xFF);
  return ESP_OK;
}

esp_err_t qt2120_read_keys12(uint16_t *keys) {
  uint8_t status0 = 0;
  uint8_t status1 = 0;

  esp_err_t ret = qt2120_read_reg(QT2120_REG_KEY_STATUS_0, &status0);
  if (ret != ESP_OK) {
    return ret;
  }

  ret = qt2120_read_reg(QT2120_REG_KEY_STATUS_1, &status1);
  if (ret != ESP_OK) {
    return ret;
  }

  *keys = (uint16_t)status0 | (((uint16_t)status1 & 0x0F) << 8);
  return ESP_OK;
}

esp_err_t qt2120_calibrate(void) {
  return qt2120_write_reg(QT2120_REG_CALIBRATE, 0x01);
}

esp_err_t qt2120_configure_conservative(void) {
  esp_err_t ret = ESP_OK;

  ret |= qt2120_write_reg(QT2120_REG_DI, QT2120_DETECTION_INTEGRATOR);
  ret |= qt2120_write_reg(QT2120_REG_TRD, 10);
  ret |= qt2120_write_reg(QT2120_REG_DHT, 0);

  for (uint8_t key = 0; key < QT2120_KEY_COUNT; ++key) {
    ret |= qt2120_write_reg(QT2120_REG_KEY0_DTHR + key,
                            QT2120_DETECT_THRESHOLD);
  }

  if (ret == ESP_OK) {
    ESP_LOGI(TAG, "Touch config: threshold=%d, DI=%d",
             QT2120_DETECT_THRESHOLD, QT2120_DETECTION_INTEGRATOR);
  }

  return ret;
}

void qt2120_log_status(void) {
  uint8_t keys = 0;
  if (qt2120_read_keys(&keys) != ESP_OK) {
    ESP_LOGE(TAG, "Failed to read keys");
    return;
  }

  if (keys > 0) {
    ESP_LOGW(TAG, "Keys Pressed: 0x%02X", keys);
  } else {
    ESP_LOGI(TAG, "No Touch");
  }
}

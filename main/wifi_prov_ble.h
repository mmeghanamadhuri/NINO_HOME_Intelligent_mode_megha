#pragma once

#include <stdbool.h>

#include "esp_err.h"

/** Start NimBLE GATT Wi-Fi provisioning (ESP32-P4 + hosted C6 BT). */
esp_err_t wifi_prov_ble_start(void);

/** Call when STA gets or loses IP (updates status characteristic notify). */
void wifi_prov_ble_on_sta_ip_changed(bool connected);

/** 128-bit service UUID for Android scanner (same as firmware GATT service). */
#define WIFI_PROV_BLE_SVC_UUID "4facb001-5a2e-4b7c-9e1f-a8d3e6f20401"
#define WIFI_PROV_BLE_DEVICE_NAME "PROV_NINO"

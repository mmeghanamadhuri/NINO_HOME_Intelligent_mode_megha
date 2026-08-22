#pragma once

#include <stdbool.h>

#include "esp_err.h"

/** Start NimBLE GATT Wi-Fi provisioning (ESP32-P4 + hosted C6 BT). */
esp_err_t wifi_prov_ble_start(void);

/** Start BLE provisioning only when no STA credentials are saved in NVS. */
esp_err_t wifi_prov_ble_start_if_needed(void);

/**
 * Ensure BLE provisioning is active after credentials were erased:
 * start NimBLE if needed, reset status, and (re)start advertising.
 */
esp_err_t wifi_prov_ble_enable_provisioning(void);

/** Stop BLE advertising when leaving setup. Host stays up for a later re-enter. */
void wifi_prov_ble_stop_advertising(void);

/** Call when STA gets or loses IP (updates status characteristic notify). */
void wifi_prov_ble_on_sta_ip_changed(bool connected);

/** Returns the current BLE advertised device name. */
const char *wifi_prov_ble_device_name(void);

/** Updates the BLE advertised device name (applies immediately when possible). */
void wifi_prov_ble_set_device_name(const char *name);

/** 128-bit service UUID for Android scanner (same as firmware GATT service). */
#define WIFI_PROV_BLE_SVC_UUID "4facb001-5a2e-4b7c-9e1f-a8d3e6f20401"
#define WIFI_PROV_BLE_DEVICE_NAME_DEFAULT "NINO - HOME"
#define WIFI_PROV_BLE_DEVICE_NAME_MAX 32

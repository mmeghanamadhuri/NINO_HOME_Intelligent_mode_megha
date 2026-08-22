#pragma once

#include <stdbool.h>
#include <stddef.h>

#include "esp_err.h"
#include "esp_wifi_types.h"

#define WIFI_CONFIG_AP_SSID "ESP32_P4_CAM"
#define WIFI_CONFIG_AP_PASS "12345678"

#define WIFI_CONFIG_STA_SSID_MAX 32
#define WIFI_CONFIG_STA_PASS_MAX 64

esp_err_t wifi_config_set_sta_credentials(const char *ssid, const char *pass);
esp_err_t wifi_config_sta_connect(wifi_mode_t mode_to_save);
bool wifi_config_sta_connected(void);
void wifi_config_get_sta_ip(char *buf, size_t buf_size);
void wifi_config_get_ap_ip(char *buf, size_t buf_size);
int wifi_config_status_json(char *buf, size_t buf_sz);
bool wifi_config_is_provisioned(void);

/** Erase STA credentials from RAM + NVS, switch to AP, start BLE provisioning.
 *  Previous network is restored if the app never continues within 2 minutes. */
esp_err_t wifi_config_enter_setup_mode(void);

/** App/BLE/HTTP took the next setup step — cancel the 2-minute idle timeout. */
void wifi_config_note_setup_activity(void);

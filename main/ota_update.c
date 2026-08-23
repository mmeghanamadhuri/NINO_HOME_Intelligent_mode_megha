#include "ota_update.h"

#include "esp_app_desc.h"
#include "esp_http_client.h"
#include "esp_log.h"
#include "esp_ota_ops.h"
#include "esp_partition.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "nino_rgb_led.h"
#include <stdlib.h>
#include <string.h>

static const char *TAG = "OTA";
static bool s_ota_active;

bool ota_update_in_progress(void) { return s_ota_active; }

bool ota_update_capable(void) {
  const esp_partition_t *update = esp_ota_get_next_update_partition(NULL);
  return update != NULL;
}

static esp_err_t http_event_handler(esp_http_client_event_t *evt) {
  return ESP_OK;
}

static void ota_task(void *arg) {
  char *url = (char *)arg;
  s_ota_active = true;
  nino_rgb_led_show(NINO_RGB_SHOW_OTA);

  const esp_partition_t *update_part = esp_ota_get_next_update_partition(NULL);
  if (update_part == NULL) {
    ESP_LOGE(TAG, "No OTA partition — flash partitions_ota.csv once over USB");
    nino_rgb_led_show(NINO_RGB_SHOW_ERROR);
    free(url);
    s_ota_active = false;
    vTaskDelete(NULL);
    return;
  }

  esp_http_client_config_t http_cfg = {
      .url = url,
      .event_handler = http_event_handler,
      .timeout_ms = 60000,
      .keep_alive_enable = true,
  };

  esp_http_client_handle_t client = esp_http_client_init(&http_cfg);
  if (client == NULL) {
    ESP_LOGE(TAG, "HTTP client init failed");
    nino_rgb_led_show(NINO_RGB_SHOW_ERROR);
    free(url);
    s_ota_active = false;
    vTaskDelete(NULL);
    return;
  }

  esp_err_t err = esp_http_client_open(client, 0);
  if (err != ESP_OK) {
    ESP_LOGE(TAG, "HTTP open failed: %s", esp_err_to_name(err));
    esp_http_client_cleanup(client);
    nino_rgb_led_show(NINO_RGB_SHOW_ERROR);
    free(url);
    s_ota_active = false;
    vTaskDelete(NULL);
    return;
  }

  int content_length = esp_http_client_fetch_headers(client);
  ESP_LOGI(TAG, "OTA content-length=%d target=%s", content_length,
           update_part->label);

  esp_ota_handle_t ota_handle = 0;
  err = esp_ota_begin(update_part, OTA_WITH_SEQUENTIAL_WRITES, &ota_handle);
  if (err != ESP_OK) {
    ESP_LOGE(TAG, "esp_ota_begin failed: %s", esp_err_to_name(err));
    esp_http_client_close(client);
    esp_http_client_cleanup(client);
    nino_rgb_led_show(NINO_RGB_SHOW_ERROR);
    free(url);
    s_ota_active = false;
    vTaskDelete(NULL);
    return;
  }

  char buf[4096];
  int total = 0;
  while (true) {
    int read = esp_http_client_read(client, buf, sizeof(buf));
    if (read < 0) {
      ESP_LOGE(TAG, "HTTP read error");
      err = ESP_FAIL;
      break;
    }
    if (read == 0) {
      break;
    }
    err = esp_ota_write(ota_handle, buf, read);
    if (err != ESP_OK) {
      ESP_LOGE(TAG, "esp_ota_write failed: %s", esp_err_to_name(err));
      break;
    }
    total += read;
  }

  esp_http_client_close(client);
  esp_http_client_cleanup(client);

  if (err != ESP_OK) {
    esp_ota_abort(ota_handle);
    nino_rgb_led_show(NINO_RGB_SHOW_ERROR);
    free(url);
    s_ota_active = false;
    vTaskDelete(NULL);
    return;
  }

  err = esp_ota_end(ota_handle);
  if (err != ESP_OK) {
    ESP_LOGE(TAG, "esp_ota_end failed: %s", esp_err_to_name(err));
    nino_rgb_led_show(NINO_RGB_SHOW_ERROR);
    free(url);
    s_ota_active = false;
    vTaskDelete(NULL);
    return;
  }

  err = esp_ota_set_boot_partition(update_part);
  if (err != ESP_OK) {
    ESP_LOGE(TAG, "esp_ota_set_boot_partition failed: %s", esp_err_to_name(err));
    nino_rgb_led_show(NINO_RGB_SHOW_ERROR);
    free(url);
    s_ota_active = false;
    vTaskDelete(NULL);
    return;
  }

  ESP_LOGI(TAG, "OTA success (%d bytes) — rebooting", total);
  free(url);
  s_ota_active = false;
  vTaskDelete(NULL);
  esp_restart();
}

esp_err_t ota_update_start(const char *url) {
  if (!url || !url[0] || s_ota_active) {
    return ESP_ERR_INVALID_STATE;
  }
  if (!ota_update_capable()) {
    ESP_LOGE(TAG, "OTA not capable on this partition table");
    return ESP_ERR_NOT_SUPPORTED;
  }
  char *url_copy = strdup(url);
  if (!url_copy) {
    return ESP_ERR_NO_MEM;
  }
  BaseType_t ok =
      xTaskCreate(ota_task, "ota_update", 8192, url_copy, 5, NULL);
  if (ok != pdPASS) {
    free(url_copy);
    return ESP_ERR_NO_MEM;
  }
  return ESP_OK;
}

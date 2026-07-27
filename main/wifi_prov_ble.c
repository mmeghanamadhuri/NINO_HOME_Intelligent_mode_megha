/*
 * BLE GATT Wi-Fi provisioning for NiNO (ESP32-P4 host + C6 controller via ESP-Hosted).
 */
#include "wifi_prov_ble.h"

#include <assert.h>
#include <stdio.h>
#include <string.h>

#include "esp_err.h"
#include "esp_log.h"

/* NimBLE headers exist only when BT + NimBLE + Hosted BT are enabled in sdkconfig. */
#if CONFIG_BT_ENABLED && CONFIG_BT_NIMBLE_ENABLED && \
    CONFIG_ESP_HOSTED_ENABLE_BT_NIMBLE

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "esp_hosted.h"
#include "host/ble_gap.h"
#include "host/ble_gatt.h"
#include "host/ble_hs.h"
#include "host/ble_store.h"
#include "host/ble_uuid.h"
#include "host/util/util.h"
#include "nimble/nimble_port.h"
#include "nimble/nimble_port_freertos.h"
#include "services/gap/ble_svc_gap.h"
#include "services/gatt/ble_svc_gatt.h"
#include "wifi_config.h"

/* Provided by bt/nimble store/config (not in public headers). */
void ble_store_config_init(void);

static const char *TAG = "wifi_prov_ble";

#define PROV_CMD_APPLY 0x01
#define HOSTED_BT_SETUP_MAX_ATTEMPTS 8
#define HOSTED_BT_SETUP_RETRY_MS 1000

/* 4facb001-5a2e-4b7c-9e1f-a8d3e6f20401 */
#define WIFI_PROV_SVC_UUID128 \
  BLE_UUID128_INIT(0x01, 0x04, 0xf2, 0xe6, 0xd3, 0xa8, 0x1f, 0x9e, 0x7c, 0x4b, \
                   0x2e, 0x5a, 0x01, 0xb0, 0xac, 0x4f)
/* ...0201 SSID */
#define WIFI_PROV_CHR_SSID_UUID128 \
  BLE_UUID128_INIT(0x01, 0x04, 0xf2, 0xe6, 0xd3, 0xa8, 0x1f, 0x9e, 0x7c, 0x4b, \
                   0x2e, 0x5a, 0x02, 0xb0, 0xac, 0x4f)
/* ...0301 password */
#define WIFI_PROV_CHR_PASS_UUID128 \
  BLE_UUID128_INIT(0x01, 0x04, 0xf2, 0xe6, 0xd3, 0xa8, 0x1f, 0x9e, 0x7c, 0x4b, \
                   0x2e, 0x5a, 0x03, 0xb0, 0xac, 0x4f)
/* ...0401 command */
#define WIFI_PROV_CHR_CMD_UUID128 \
  BLE_UUID128_INIT(0x01, 0x04, 0xf2, 0xe6, 0xd3, 0xa8, 0x1f, 0x9e, 0x7c, 0x4b, \
                   0x2e, 0x5a, 0x04, 0xb0, 0xac, 0x4f)
/* ...0501 status */
#define WIFI_PROV_CHR_STATUS_UUID128 \
  BLE_UUID128_INIT(0x01, 0x04, 0xf2, 0xe6, 0xd3, 0xa8, 0x1f, 0x9e, 0x7c, 0x4b, \
                   0x2e, 0x5a, 0x05, 0xb0, 0xac, 0x4f)

static const ble_uuid128_t s_svc_uuid = WIFI_PROV_SVC_UUID128;
static const ble_uuid128_t s_chr_ssid_uuid = WIFI_PROV_CHR_SSID_UUID128;
static const ble_uuid128_t s_chr_pass_uuid = WIFI_PROV_CHR_PASS_UUID128;
static const ble_uuid128_t s_chr_cmd_uuid = WIFI_PROV_CHR_CMD_UUID128;
static const ble_uuid128_t s_chr_status_uuid = WIFI_PROV_CHR_STATUS_UUID128;

static char s_pending_ssid[WIFI_CONFIG_STA_SSID_MAX];
static char s_pending_pass[WIFI_CONFIG_STA_PASS_MAX];
static char s_status_json[96];
static uint16_t s_status_val_handle;
static uint8_t s_own_addr_type;
static bool s_ble_started;
static uint16_t s_conn_handle = BLE_HS_CONN_HANDLE_NONE;
static bool s_bt_ctrl_ready;
static char s_prov_device_name[WIFI_PROV_BLE_DEVICE_NAME_MAX + 1] =
    WIFI_PROV_BLE_DEVICE_NAME_DEFAULT;

static void prov_advertise(void);

static void set_prov_device_name(const char *name) {
  if (name == NULL || name[0] == '\0') {
    name = WIFI_PROV_BLE_DEVICE_NAME_DEFAULT;
  }
  size_t len = strnlen(name, WIFI_PROV_BLE_DEVICE_NAME_MAX);
  memcpy(s_prov_device_name, name, len);
  s_prov_device_name[len] = '\0';
}

static esp_err_t hosted_bt_setup_with_retry(void) {
  if (s_bt_ctrl_ready) {
    return ESP_OK;
  }
  for (int attempt = 0; attempt < HOSTED_BT_SETUP_MAX_ATTEMPTS; ++attempt) {
    /* BLE is only needed while the robot has no saved network. Do not keep
     * issuing long Hosted RPCs after provisioning has succeeded. */
    if (wifi_config_is_provisioned()) {
      ESP_LOGI(TAG, "Wi-Fi credentials received — stopping BLE controller setup");
      return ESP_ERR_INVALID_STATE;
    }
    if (attempt > 0) {
      vTaskDelay(pdMS_TO_TICKS(HOSTED_BT_SETUP_RETRY_MS));
    }
    esp_err_t err = esp_hosted_connect_to_slave();
    if (err != ESP_OK) {
      ESP_LOGW(TAG, "connect_to_slave (attempt %d): %s", attempt + 1,
               esp_err_to_name(err));
    }
    err = esp_hosted_bt_controller_init();
    if (err != ESP_OK) {
      ESP_LOGW(TAG, "bt_controller_init (attempt %d): %s", attempt + 1,
               esp_err_to_name(err));
      continue;
    }
    err = esp_hosted_bt_controller_enable();
    if (err != ESP_OK) {
      ESP_LOGW(TAG, "bt_controller_enable (attempt %d): %s", attempt + 1,
               esp_err_to_name(err));
      continue;
    }
    s_bt_ctrl_ready = true;
    ESP_LOGI(TAG, "C6 BLE controller ready");
    return ESP_OK;
  }
  ESP_LOGW(TAG,
           "BLE controller on C6 not ready — flash ESP-Hosted firmware to the "
           "C6 (see docs/WIFI_PROVISION.md). HTTP provisioning still works.");
  return ESP_FAIL;
}

static int gatt_prov_access(uint16_t conn_handle, uint16_t attr_handle,
                            struct ble_gatt_access_ctxt *ctxt, void *arg);

static const struct ble_gatt_svc_def s_gatt_svcs[] = {
    {
        .type = BLE_GATT_SVC_TYPE_PRIMARY,
        .uuid = &s_svc_uuid.u,
        .characteristics =
            (struct ble_gatt_chr_def[]){
                {
                    .uuid = &s_chr_ssid_uuid.u,
                    .access_cb = gatt_prov_access,
                    .flags = BLE_GATT_CHR_F_WRITE,
                },
                {
                    .uuid = &s_chr_pass_uuid.u,
                    .access_cb = gatt_prov_access,
                    .flags = BLE_GATT_CHR_F_WRITE,
                },
                {
                    .uuid = &s_chr_cmd_uuid.u,
                    .access_cb = gatt_prov_access,
                    .flags = BLE_GATT_CHR_F_WRITE,
                },
                {
                    .uuid = &s_chr_status_uuid.u,
                    .access_cb = gatt_prov_access,
                    .flags = BLE_GATT_CHR_F_READ | BLE_GATT_CHR_F_NOTIFY,
                    .val_handle = &s_status_val_handle,
                },
                {0},
            },
    },
    {0},
};

static int write_mbuf_to_buf(struct os_mbuf *om, void *dst, uint16_t max_len,
                             uint16_t *out_len) {
  uint16_t om_len = OS_MBUF_PKTLEN(om);
  if (om_len > max_len) {
    return BLE_ATT_ERR_INVALID_ATTR_VALUE_LEN;
  }
  int rc = ble_hs_mbuf_to_flat(om, dst, max_len, out_len);
  return (rc == 0) ? 0 : BLE_ATT_ERR_UNLIKELY;
}

static void prov_update_status_json(int state, const char *msg) {
  char ip[16] = "0.0.0.0";
  if (msg == NULL) {
    msg = "unknown";
  }
  wifi_config_get_sta_ip(ip, sizeof(ip));
  snprintf(s_status_json, sizeof(s_status_json),
           "{\"state\":%d,\"connected\":%s,\"ip\":\"%s\",\"msg\":\"%s\"}", state,
           wifi_config_sta_connected() ? "true" : "false", ip, msg);
}

static void prov_notify_status(void) {
  if (s_status_val_handle == 0 || s_conn_handle == BLE_HS_CONN_HANDLE_NONE) {
    return;
  }
  struct os_mbuf *om =
      ble_hs_mbuf_from_flat(s_status_json, strlen(s_status_json));
  if (om == NULL) {
    return;
  }
  ble_gatts_notify_custom(s_conn_handle, s_status_val_handle, om);
}

void wifi_prov_ble_on_sta_ip_changed(bool connected) {
  if (!s_ble_started) {
    return;
  }
  if (connected && s_bt_ctrl_ready) {
    prov_advertise();
  }
  prov_update_status_json(connected ? 2 : 1, connected ? "done" : "connecting");
  prov_notify_status();
}

const char *wifi_prov_ble_device_name(void) { return s_prov_device_name; }

void wifi_prov_ble_set_device_name(const char *name) {
  set_prov_device_name(name);
  if (!s_ble_started) {
    return;
  }
  ble_svc_gap_device_name_set(s_prov_device_name);
  if (s_bt_ctrl_ready) {
    prov_advertise();
  }
}

static void prov_apply_task(void *arg) {
  (void)arg;
  ESP_LOGI(TAG, "BLE apply received (ssid=%s, pass_len=%u)", s_pending_ssid,
           (unsigned)strlen(s_pending_pass));
  prov_update_status_json(1, "applying");
  prov_notify_status();

  esp_err_t err = wifi_config_set_sta_credentials(s_pending_ssid, s_pending_pass);
  if (err == ESP_OK) {
    err = wifi_config_sta_connect(WIFI_MODE_STA);
  }

  if (err != ESP_OK) {
    ESP_LOGW(TAG, "BLE apply failed before connect: %s", esp_err_to_name(err));
    prov_update_status_json(3, "failed");
    prov_notify_status();
    vTaskDelete(NULL);
    return;
  }

  for (int i = 0; i < 40; ++i) {
    vTaskDelay(pdMS_TO_TICKS(500));
    if (wifi_config_sta_connected()) {
      ESP_LOGI(TAG, "BLE apply success: STA connected");
      prov_update_status_json(2, "done");
      prov_notify_status();
      vTaskDelete(NULL);
      return;
    }
  }
  ESP_LOGW(TAG, "BLE apply timeout: STA did not connect in time");
  prov_update_status_json(3, "timeout");
  prov_notify_status();
  vTaskDelete(NULL);
}

static int gatt_prov_access(uint16_t conn_handle, uint16_t attr_handle,
                            struct ble_gatt_access_ctxt *ctxt, void *arg) {
  (void)conn_handle;
  (void)attr_handle;
  (void)arg;

  const ble_uuid_t *uuid = NULL;
  if (ctxt->op == BLE_GATT_ACCESS_OP_WRITE_CHR) {
    uuid = ctxt->chr->uuid;
  } else if (ctxt->op == BLE_GATT_ACCESS_OP_READ_CHR) {
    uuid = ctxt->chr->uuid;
  } else {
    return BLE_ATT_ERR_UNLIKELY;
  }

  if (ble_uuid_cmp(uuid, &s_chr_ssid_uuid.u) == 0 &&
      ctxt->op == BLE_GATT_ACCESS_OP_WRITE_CHR) {
    uint16_t len = 0;
    int rc = write_mbuf_to_buf(ctxt->om, s_pending_ssid,
                               sizeof(s_pending_ssid) - 1, &len);
    if (rc != 0) {
      return rc;
    }
    s_pending_ssid[len] = '\0';
    ESP_LOGI(TAG, "BLE SSID set (%u bytes)", (unsigned)len);
    prov_update_status_json(0, "ssid_received");
    prov_notify_status();
    return 0;
  }

  if (ble_uuid_cmp(uuid, &s_chr_pass_uuid.u) == 0 &&
      ctxt->op == BLE_GATT_ACCESS_OP_WRITE_CHR) {
    uint16_t len = 0;
    int rc = write_mbuf_to_buf(ctxt->om, s_pending_pass,
                               sizeof(s_pending_pass) - 1, &len);
    if (rc != 0) {
      return rc;
    }
    s_pending_pass[len] = '\0';
    ESP_LOGI(TAG, "BLE password set (%u bytes)", (unsigned)len);
    if (s_pending_ssid[0] != '\0') {
      prov_update_status_json(0, "creds_received");
      prov_notify_status();
    }
    return 0;
  }

  if (ble_uuid_cmp(uuid, &s_chr_cmd_uuid.u) == 0 &&
      ctxt->op == BLE_GATT_ACCESS_OP_WRITE_CHR) {
    uint8_t cmd = 0;
    uint16_t len = 0;
    int rc = write_mbuf_to_buf(ctxt->om, &cmd, sizeof(cmd), &len);
    if (rc != 0 || len < 1) {
      return BLE_ATT_ERR_INVALID_ATTR_VALUE_LEN;
    }
    if (cmd != PROV_CMD_APPLY) {
      return BLE_ATT_ERR_REQ_NOT_SUPPORTED;
    }
    if (s_pending_ssid[0] == '\0') {
      return BLE_ATT_ERR_INVALID_ATTR_VALUE_LEN;
    }
    ESP_LOGI(TAG, "BLE apply command accepted");
    prov_update_status_json(1, "apply_accepted");
    prov_notify_status();
    xTaskCreate(prov_apply_task, "ble_prov", 4096, NULL, 5, NULL);
    return 0;
  }

  if (ble_uuid_cmp(uuid, &s_chr_status_uuid.u) == 0 &&
      ctxt->op == BLE_GATT_ACCESS_OP_READ_CHR) {
    prov_update_status_json(wifi_config_sta_connected() ? 2 : 0,
                            wifi_config_sta_connected() ? "done" : "idle");
    int rc = os_mbuf_append(ctxt->om, s_status_json, strlen(s_status_json));
    return (rc == 0) ? 0 : BLE_ATT_ERR_INSUFFICIENT_RES;
  }

  return BLE_ATT_ERR_UNLIKELY;
}

static void gatt_register_cb(struct ble_gatt_register_ctxt *ctxt, void *arg) {
  (void)arg;
  (void)ctxt;
}

static int gap_event(struct ble_gap_event *event, void *arg) {
  (void)arg;
  switch (event->type) {
  case BLE_GAP_EVENT_CONNECT:
    ESP_LOGI(TAG, "BLE %s", event->connect.status == 0 ? "connected" : "failed");
    if (event->connect.status == 0) {
      s_conn_handle = event->connect.conn_handle;
    } else {
      prov_advertise();
    }
    return 0;
  case BLE_GAP_EVENT_DISCONNECT:
    ESP_LOGI(TAG, "BLE disconnected reason=%d", event->disconnect.reason);
    s_conn_handle = BLE_HS_CONN_HANDLE_NONE;
    prov_advertise();
    return 0;
  case BLE_GAP_EVENT_ADV_COMPLETE:
    prov_advertise();
    return 0;
  default:
    return 0;
  }
}

static void prov_advertise(void) {
  struct ble_hs_adv_fields adv_fields;
  struct ble_hs_adv_fields rsp_fields;
  struct ble_gap_adv_params adv_params;
  int rc;

  /* 31-byte ADV limit: keep name in ADV, 128-bit UUID in scan response (rc=4 EINVAL if combined). */
  memset(&adv_fields, 0, sizeof(adv_fields));
  adv_fields.flags = BLE_HS_ADV_F_DISC_GEN | BLE_HS_ADV_F_BREDR_UNSUP;
  adv_fields.name = (uint8_t *)s_prov_device_name;
  adv_fields.name_len = strlen(s_prov_device_name);
  adv_fields.name_is_complete = 1;

  rc = ble_gap_adv_set_fields(&adv_fields);
  if (rc != 0) {
    ESP_LOGE(TAG, "adv_set_fields rc=%d", rc);
    return;
  }

  memset(&rsp_fields, 0, sizeof(rsp_fields));
  rsp_fields.uuids128 = &s_svc_uuid;
  rsp_fields.num_uuids128 = 1;
  rsp_fields.uuids128_is_complete = 1;
  rc = ble_gap_adv_rsp_set_fields(&rsp_fields);
  if (rc != 0) {
    ESP_LOGE(TAG, "adv_rsp_set_fields rc=%d", rc);
    return;
  }

  memset(&adv_params, 0, sizeof(adv_params));
  adv_params.conn_mode = BLE_GAP_CONN_MODE_UND;
  adv_params.disc_mode = BLE_GAP_DISC_MODE_GEN;
  rc = ble_gap_adv_start(s_own_addr_type, NULL, BLE_HS_FOREVER, &adv_params,
                         gap_event, NULL);
  if (rc != 0) {
    ESP_LOGE(TAG, "adv_start rc=%d", rc);
  }
}

static void on_sync(void) {
  int rc = ble_hs_util_ensure_addr(0);
  if (rc != 0) {
    ESP_LOGE(TAG, "ensure_addr rc=%d", rc);
    return;
  }
  rc = ble_hs_id_infer_auto(0, &s_own_addr_type);
  if (rc != 0) {
    ESP_LOGE(TAG, "infer_addr rc=%d", rc);
    return;
  }

  ble_svc_gap_device_name_set(s_prov_device_name);
  prov_update_status_json(0, "idle");
  prov_advertise();
  ESP_LOGI(TAG, "BLE provisioning GATT ready (%s)", s_prov_device_name);
}

static void on_reset(int reason) {
  ESP_LOGW(TAG, "NimBLE reset reason=%d", reason);
}

static void host_task(void *param) {
  (void)param;
  nimble_port_run();
  nimble_port_freertos_deinit();
}

static int gatt_init(void) {
  int rc;
  ble_svc_gap_init();
  ble_svc_gatt_init();
  rc = ble_gatts_count_cfg(s_gatt_svcs);
  if (rc != 0) {
    return rc;
  }
  rc = ble_gatts_add_svcs(s_gatt_svcs);
  return rc;
}

esp_err_t wifi_prov_ble_start(void) {
  if (s_ble_started) {
    return ESP_OK;
  }

  s_pending_ssid[0] = '\0';
  s_pending_pass[0] = '\0';
  prov_update_status_json(0, "idle");

  /* Start the NimBLE host even when the controller setup is still settling.
   * ESP-Hosted can complete its controller handshake asynchronously. */
  (void)hosted_bt_setup_with_retry();

  esp_err_t err = nimble_port_init();
  if (err != ESP_OK) {
    ESP_LOGE(TAG, "nimble_port_init: %s", esp_err_to_name(err));
    return err;
  }

  ble_hs_cfg.reset_cb = on_reset;
  ble_hs_cfg.sync_cb = on_sync;
  ble_hs_cfg.gatts_register_cb = gatt_register_cb;
  ble_hs_cfg.store_status_cb = ble_store_util_status_rr;
  ble_hs_cfg.sm_io_cap = BLE_SM_IO_CAP_NO_IO;
  ble_hs_cfg.sm_bonding = 0;
  ble_hs_cfg.sm_sc = 0;

  ble_store_config_init();
  if (gatt_init() != 0) {
    ESP_LOGE(TAG, "GATT init failed");
    return ESP_FAIL;
  }

  nimble_port_freertos_init(host_task);
  s_ble_started = true;
  ESP_LOGI(TAG, "NimBLE host started");
  return ESP_OK;
}

esp_err_t wifi_prov_ble_start_if_needed(void) {
  if (wifi_config_is_provisioned()) {
    ESP_LOGI(TAG,
             "Wi-Fi credentials found in NVS — BLE provisioning not started");
    return ESP_OK;
  }
  ESP_LOGI(TAG, "No Wi-Fi credentials in NVS — starting BLE provisioning");
  return wifi_prov_ble_start();
}

#else /* !CONFIG_BT_NIMBLE_ENABLED || !CONFIG_ESP_HOSTED_ENABLE_BT_NIMBLE */

#include "wifi_config.h"

static char s_prov_device_name[WIFI_PROV_BLE_DEVICE_NAME_MAX + 1] =
    WIFI_PROV_BLE_DEVICE_NAME_DEFAULT;

esp_err_t wifi_prov_ble_start(void) {
  ESP_LOGW("wifi_prov_ble",
           "BLE provisioning off — enable BT + NimBLE + ESP-Hosted BT in "
           "sdkconfig (see sdkconfig.defaults.esp32p4), then idf.py fullclean "
           "build");
  return ESP_ERR_NOT_SUPPORTED;
}

esp_err_t wifi_prov_ble_start_if_needed(void) {
  if (wifi_config_is_provisioned()) {
    ESP_LOGI("wifi_prov_ble",
             "Wi-Fi credentials found in NVS — BLE provisioning not started");
    return ESP_OK;
  }
  ESP_LOGI("wifi_prov_ble", "No Wi-Fi credentials in NVS — BLE not available");
  return wifi_prov_ble_start();
}

void wifi_prov_ble_on_sta_ip_changed(bool connected) {
  (void)connected;
}

const char *wifi_prov_ble_device_name(void) { return s_prov_device_name; }

void wifi_prov_ble_set_device_name(const char *name) {
  if (name == NULL || name[0] == '\0') {
    name = WIFI_PROV_BLE_DEVICE_NAME_DEFAULT;
  }
  size_t len = strnlen(name, WIFI_PROV_BLE_DEVICE_NAME_MAX);
  memcpy(s_prov_device_name, name, len);
  s_prov_device_name[len] = '\0';
}

#endif

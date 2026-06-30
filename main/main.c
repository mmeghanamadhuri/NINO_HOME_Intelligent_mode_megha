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
#include "esp_http_server.h"
#include "esp_log.h"
#include "esp_netif.h"
#include "esp_timer.h"
#include "esp_wifi.h"
#include "lwip/inet.h"
#include "lwip/ip4_addr.h"
#include "lwip/sockets.h"
#include "nvs.h"
#include "nvs_flash.h"
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
#include "face_detect.hpp"
#include "face_tracker.h"
#include "nino_eye.h"
#include "ssd1351.h"
#include "servo_dxl.h"
#include "servo_motion.h"
#include "touch_sensor.h"
#include "voice_assist.h"
#include "voice_wake.h"
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

#define MULTICAST_ADDR "239.255.255.250"
#define BROADCAST_ADDR "255.255.255.255"
#define DISCOVERY_PORT 1900
#define MESSAGE_PORT 8888
#define DISCOVERY_MSG "discover"
#define DISCOVERY_BUF 192
#define MESSAGE_BUF 256

#define STA_RECONNECT_DELAY_MS 5000
static char s_sta_ssid[WIFI_CONFIG_STA_SSID_MAX] = "";
static char s_sta_pass[WIFI_CONFIG_STA_PASS_MAX] = "";

static wifi_mode_t s_wifi_mode = WIFI_MODE_AP;
static bool s_sta_connected = false;
static bool s_wifi_connected_chime_pending = false;
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
#define HTTP_STREAM_ROTATE_DEG 90
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
static char s_voice_ws_url[160];
static bool s_voice_wake_started;
static int64_t s_last_uvc_timeout_log_us;
static char s_device_name[WIFI_PROV_BLE_DEVICE_NAME_MAX + 1] =
    DEVICE_NAME_DEFAULT;

extern const uint8_t wifi_wav_start[] asm("_binary_WIFI_wav_start");
extern const uint8_t wifi_wav_end[] asm("_binary_WIFI_wav_end");

extern const uint8_t hello_home_wav_start[] asm("_binary_Hello_home_wav_start");
extern const uint8_t hello_home_wav_end[] asm("_binary_Hello_home_wav_end");

extern const uint8_t wifi_unable_wav_start[] asm("_binary_WIFI_UNABLE_wav_start");
extern const uint8_t wifi_unable_wav_end[] asm("_binary_WIFI_UNABLE_wav_end");

extern const uint8_t go_app_wav_start[] asm("_binary_GO_APP_wav_start");
extern const uint8_t go_app_wav_end[] asm("_binary_GO_APP_wav_end");

/* Set once WIFI-UNABLE.wav has been played for the current connect attempt so
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

static bool play_hello_home_clip(void) {
  const size_t wav_len = (size_t)(hello_home_wav_end - hello_home_wav_start);
  if (wav_len < 44) {
    ESP_LOGW(TAG, "Embedded Hello-home.wav missing or too small");
    return false;
  }

  esp_err_t err = nino_audio_queue_wav_copy(hello_home_wav_start, wav_len, false,
                                            NINO_AUDIO_SERVO_PRIORITY_NONE, false);
  if (err != ESP_OK) {
    ESP_LOGW(TAG, "Failed to queue Hello-home.wav: %s", esp_err_to_name(err));
    return false;
  }

  ESP_LOGI(TAG, "Queued Hello-home.wav (%u bytes) after boot",
           (unsigned)wav_len);
  return true;
}

static bool play_wifi_unable_clip(void) {
  const size_t wav_len = (size_t)(wifi_unable_wav_end - wifi_unable_wav_start);
  if (wav_len < 44) {
    ESP_LOGW(TAG, "Embedded WIFI-UNABLE.wav missing or too small");
    return false;
  }

  esp_err_t err = nino_audio_queue_wav_copy(wifi_unable_wav_start, wav_len, false,
                                            NINO_AUDIO_SERVO_PRIORITY_NONE, false);
  if (err != ESP_OK) {
    ESP_LOGW(TAG, "Failed to queue WIFI-UNABLE.wav: %s", esp_err_to_name(err));
    return false;
  }

  ESP_LOGI(TAG, "Queued WIFI-UNABLE.wav (%u bytes): STA connect failed",
           (unsigned)wav_len);
  return true;
}

static bool play_go_app_clip(void) {
  const size_t wav_len = (size_t)(go_app_wav_end - go_app_wav_start);
  if (wav_len < 44) {
    ESP_LOGW(TAG, "Embedded GO-APP.wav missing or too small");
    return false;
  }

  esp_err_t err = nino_audio_queue_wav_copy(go_app_wav_start, wav_len, false,
                                            NINO_AUDIO_SERVO_PRIORITY_NONE, false);
  if (err != ESP_OK) {
    ESP_LOGW(TAG, "Failed to queue GO-APP.wav: %s", esp_err_to_name(err));
    return false;
  }

  ESP_LOGI(TAG, "Queued GO-APP.wav (%u bytes): no saved Wi-Fi network",
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
 *  - No saved Wi-Fi network in NVS -> prompt the user to use the app (GO-APP).
 *  - Provisioned and connected -> greet with Hello-home after the WIFI.wav clip.
 *    Falls back to greeting anyway if Wi-Fi never connects within the timeout,
 *    unless we already played the "unable to connect" prompt. */
static void hello_home_task(void *arg) {
  (void)arg;
  if (s_sta_ssid[0] == '\0') {
    play_go_app_clip();
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
  vTaskDelete(NULL);
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

  err = mdns_hostname_set(s_device_name);
  if (err != ESP_OK) {
    ESP_LOGW(TAG, "mDNS hostname set failed: %s", esp_err_to_name(err));
    mdns_free();
    return;
  }

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
      {"ble_name", s_device_name},
      {"transport", "http"},
  };
  err = mdns_service_txt_set(MDNS_SERVICE_TYPE, MDNS_SERVICE_PROTO, txt,
                             sizeof(txt) / sizeof(txt[0]));
  if (err != ESP_OK) {
    ESP_LOGW(TAG, "mDNS TXT set failed: %s", esp_err_to_name(err));
    mdns_free();
    return;
  }

  s_mdns_started = true;
  ESP_LOGI(TAG, "mDNS ready: %s.local service %s.%s port %d", s_device_name,
           MDNS_SERVICE_TYPE, MDNS_SERVICE_PROTO, HTTP_SERVER_PORT);
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

static void voice_wake_start_once(void) {
  if (s_voice_wake_started) {
    return;
  }
  s_voice_wake_started = true;
  nino_voice_wake_init();
  if (nino_voice_wake_hw_ready()) {
    nino_voice_wake_set_enabled(true);
    if (s_voice_ws_url[0] == '\0') {
      ESP_LOGW(TAG,
               "Voice PC URL not set — serial: voice connect <YOUR_PC_LAN_IP> 8000 "
               "(not the ESP camera IP)");
    } else {
      ESP_LOGI(TAG, "Voice assistant URL: %s", s_voice_ws_url);
    }
  } else {
    nino_voice_wake_set_enabled(false);
    ESP_LOGE(TAG, "Wake word not started — check srmodels partition / flash size");
  }
}

static void delayed_voice_wake_task(void *arg) {
  (void)arg;
  vTaskDelay(pdMS_TO_TICKS(5000));
  voice_wake_start_once();
  vTaskDelete(NULL);
}

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
  return ESP_OK;
}

bool wifi_config_sta_connected(void) { return s_sta_connected; }

bool wifi_config_is_provisioned(void) { return s_sta_ssid[0] != '\0'; }

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
static void servo_cli_register(void);
static void track_cli_register(void);
static void speaker_cli_register(void);
static void hstop_cli_register(void);

static int cmd_eye(int argc, char **argv) {
  if (argc >= 2 && nino_eye_apply_command(argv[1])) {
    printf("eye -> state %d\n", (int)nino_eye_get_state());
    return 0;
  }
  printf("Usage: eye <idle|happy|tired|thinking|curious|sad|surprised|listening|recalling>"
         "   (current state: %d)\n",
         (int)nino_eye_get_state());
  return 0;
}

static void eye_cli_register(void) {
  const esp_console_cmd_t eye_cmd = {
      .command = "eye",
      .help = "Set NINO eye state: eye <idle|happy|tired|thinking|curious|sad|surprised|listening|recalling>",
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
  voice_cli_register();
  servo_cli_register();
  track_cli_register();
  speaker_cli_register();
  eye_cli_register();
  hstop_cli_register();

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
    s_wifi_connected_chime_pending = true;
    wifi_prov_ble_on_sta_ip_changed(false);
    mdns_stop_service();
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
    ESP_LOGI(TAG, "STA: Got IP " IPSTR, IP2STR(&event->ip_info.ip));
    mdns_start_service();
    wifi_prov_ble_on_sta_ip_changed(true);
    if (s_wifi_connected_chime_pending) {
      if (play_wifi_connected_clip()) {
        s_wifi_connected_chime_pending = false;
      }
    }
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
    esp_wifi_connect();
  }

  wifi_save_to_nvs(mode);
  const char *mode_str = (mode == WIFI_MODE_AP)    ? "AP"
                         : (mode == WIFI_MODE_STA) ? "STA"
                                                   : "AP+STA";
  ESP_LOGI(TAG, "WiFi mode switched to %s", mode_str);
  return ESP_OK;
}

static void wifi_init_all(void) {
  ESP_ERROR_CHECK(esp_netif_init());
  ESP_ERROR_CHECK(esp_event_loop_create_default());
  esp_netif_create_default_wifi_ap();
  esp_netif_create_default_wifi_sta();

  wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
  ESP_ERROR_CHECK(esp_wifi_init(&cfg));

  s_wifi_connected_chime_pending = true;

  ESP_ERROR_CHECK(esp_event_handler_instance_register(
      WIFI_EVENT, ESP_EVENT_ANY_ID, &wifi_event_handler, NULL, NULL));
  ESP_ERROR_CHECK(esp_event_handler_instance_register(
      IP_EVENT, IP_EVENT_STA_GOT_IP, &wifi_event_handler, NULL, NULL));

  wifi_mode_t saved_mode = wifi_load_from_nvs();
  ESP_ERROR_CHECK(wifi_switch_mode(saved_mode));
}

static bool is_discovery_request(const char *msg, size_t len) {
  if (len < strlen(DISCOVERY_MSG))
    return false;
  if (len >= DISCOVERY_BUF)
    return false;
  return (strncmp(msg, DISCOVERY_MSG, strlen(DISCOVERY_MSG)) == 0);
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

          if (strcmp(ap_ip, "0.0.0.0") != 0 && strcmp(sta_ip, "0.0.0.0") != 0) {
            rlen = snprintf(response, sizeof(response),
                            "hi\nmac=%s\nname=%s\n%s:%d\n%s:%d", mac_str,
                            device_name, ap_ip, MESSAGE_PORT, sta_ip,
                            MESSAGE_PORT);
          } else if (strcmp(ap_ip, "0.0.0.0") != 0) {
            rlen = snprintf(response, sizeof(response),
                            "hi\nmac=%s\nname=%s\n%s:%d", mac_str, device_name,
                            ap_ip, MESSAGE_PORT);
          } else {
            rlen = snprintf(response, sizeof(response),
                            "hi\nmac=%s\nname=%s\n%s:%d", mac_str, device_name,
                            sta_ip, MESSAGE_PORT);
          }
          if (rlen > 0 && rlen < (int)sizeof(response)) {
            sendto(sock, response, (size_t)rlen, 0, (struct sockaddr *)&raddr,
                   raddr_len);
            ESP_LOGI(TAG, "Discovery: responded to %s",
                     inet_ntoa(raddr.sin_addr));
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

  /* Optional emotion tag from server (e.g. happy/sad/surprised). */
  nino_eye_state_t eye_state = NINO_EYE_STATE_COUNT;
  char eye_hdr[24] = {0};
  if (httpd_req_get_hdr_value_str(req, "X-Nino-Eye-Expression", eye_hdr,
                                  sizeof(eye_hdr)) == ESP_OK) {
    eye_state = nino_eye_state_from_name(eye_hdr);
    if (eye_state < NINO_EYE_STATE_COUNT) {
      ESP_LOGI(TAG, "HTTP /play_wav eye_expression=%s -> state %d", eye_hdr,
               (int)eye_state);
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

static int app_status_json(char *buf, size_t buf_sz) {
  char sta_ip[16] = "0.0.0.0";
  wifi_config_get_sta_ip(sta_ip, sizeof(sta_ip));
  const char *fw_version = PROJECT_VER;

  return snprintf(
      buf, buf_sz,
      "{\"ok\":true,\"device_name\":\"%s\",\"wifi_ssid\":\"%s\","
      "\"volume\":%d,\"firmware\":\"%s\",\"sta_connected\":%s,"
      "\"ip\":\"%s\",\"mdns_host\":\"%s.local\"}",
      s_device_name, s_sta_ssid, nino_audio_get_volume_percent(), fw_version,
      s_sta_connected ? "true" : "false", sta_ip, s_device_name);
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

static esp_err_t status_handler(httpd_req_t *req) {
  if (req->method != HTTP_GET) {
    httpd_resp_set_status(req, "405 Method Not Allowed");
    httpd_resp_set_type(req, "application/json");
    return httpd_resp_send(req, "{\"ok\":false,\"error\":\"GET only\"}",
                           HTTPD_RESP_USE_STRLEN);
  }

  char body[320];
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
  char body[320];
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
}

static int cmd_voice(int argc, char **argv) {
  if (argc >= 2 && strcmp(argv[1], "connect") == 0) {
    if (argc < 3) {
      printf("Usage: voice connect <IPv4> [port]   (default port 8000)\n"
             "  Saves ws://<ip>:<port>/voice-query for \"Hi ESP\" wake flow\n");
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
    nino_voice_wake_set_enabled(s_voice_ws_url[0] != '\0');
    printf("Voice assistant: %s\n", s_voice_ws_url);
    return 0;
  }
  if (argc >= 2 && strcmp(argv[1], "url") == 0) {
    if (argc < 3) {
      printf("voice url: \"%s\"\n", s_voice_ws_url);
      return 0;
    }
    strncpy(s_voice_ws_url, argv[2], sizeof(s_voice_ws_url) - 1);
    s_voice_ws_url[sizeof(s_voice_ws_url) - 1] = '\0';
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
    nino_voice_wake_set_enabled(s_voice_ws_url[0] != '\0');
    return 0;
  }
  if (argc >= 2 && strcmp(argv[1], "status") == 0) {
    printf("voice wake hw: %s\n", nino_voice_wake_hw_ready() ? "ready" : "not loaded");
    printf("voice wake: %s\n", nino_voice_wake_is_enabled() ? "on" : "off");
    printf("voice url: \"%s\"\n", s_voice_ws_url[0] ? s_voice_ws_url : "(not set)");
    printf("Tip: voice connect must use your PC LAN IP (where python app.py runs), not 192.168.x.x of the board.\n");
    return 0;
  }
  if (argc >= 2 && strcmp(argv[1], "wake") == 0) {
    if (argc < 3) {
      printf("voice wake: %s\n", nino_voice_wake_is_enabled() ? "on" : "off");
      return 0;
    }
    if (strcmp(argv[2], "on") == 0) {
      nino_voice_wake_set_enabled(true);
      return 0;
    }
    if (strcmp(argv[2], "off") == 0) {
      nino_voice_wake_set_enabled(false);
      return 0;
    }
    printf("Usage: voice wake [on|off]\n");
    return 1;
  }
  printf("Usage: voice connect <ip> [port] | voice url [<ws-uri>] | voice wake [on|off] | voice status\n");
  return 0;
}

static void voice_cli_register(void) {
  const esp_console_cmd_t cmd = {
      .command = "voice",
      .help = "voice connect <PC_IP> [port] | voice status | voice wake [on|off]",
      .hint = NULL,
      .func = &cmd_voice,
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

static void servo_cli_register(void) {
  const esp_console_cmd_t cmd = {
      .command = "360",
      .help = "ID2 full rotation: home to 512 if needed, then 512->0->1023->512",
      .hint = NULL,
      .func = &cmd_servo_360,
      .argtable = NULL,
  };
  ESP_ERROR_CHECK(esp_console_cmd_register(&cmd));
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
    printf("Pan tracking ON (servo ID 2)\n");
    return 0;
  }

  if (strcmp(argv[1], "off") == 0) {
    nino_face_tracker_set_enabled(false);
    printf("Pan tracking OFF\n");
    return 0;
  }

  if (strcmp(argv[1], "status") == 0) {
    nino_face_tracker_status_t status = {};
    nino_face_tracker_get_status(&status);
    printf("track: %s\n", status.enabled ? "ON" : "OFF");
    printf("detector: %s\n", status.detector_ready ? "ready" : "not ready");
    printf("pan goal: %d\n", status.pan_goal);
    printf("last frame seq: %lu\n", (unsigned long)status.last_frame_sequence);
    printf("face: %s\n", status.face_found ? "found" : "not found");
    if (status.face_found && status.last_frame_w > 0) {
      printf("face cx/frame_w: %d/%d\n", status.last_face_cx, status.last_frame_w);
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
      .help = "track on | off | status  (pan-only face tracking on servo ID 2)",
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

  printf("Usage: speaker volume [0-100]\n");
  return 0;
}

static void speaker_cli_register(void) {
  const esp_console_cmd_t cmd = {
      .command = "speaker",
      .help = "speaker volume [0-100]",
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
  config.max_uri_handlers = 20;
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
  ESP_ERROR_CHECK(httpd_register_uri_handler(s_http_server, &servo_360_uri));
  ESP_ERROR_CHECK(httpd_register_uri_handler(s_http_server, &eye_expression_uri));
  ESP_ERROR_CHECK(
      httpd_register_uri_handler(s_http_server, &speaker_volume_get_uri));
  ESP_ERROR_CHECK(
      httpd_register_uri_handler(s_http_server, &speaker_volume_post_uri));
  ESP_ERROR_CHECK(httpd_register_uri_handler(s_http_server, &volume_get_uri));
  ESP_ERROR_CHECK(httpd_register_uri_handler(s_http_server, &volume_post_uri));
  ESP_ERROR_CHECK(httpd_register_uri_handler(s_http_server, &status_uri));
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
  /* Wake word starts from delayed_voice_wake_task — avoids racing UVC USB DMA alloc. */
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

  /* Eye OLEDs come up first so the robot shows its idle face during boot. */
  if (ssd1351_init() == ESP_OK) {
    nino_eye_begin(); /* defaults to NINO_EYE_IDLE */
  } else {
    ESP_LOGW(TAG, "SSD1351 eye displays init failed; running without eyes");
  }

  ESP_ERROR_CHECK(nino_voice_assist_init_mutex());
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

  wifi_init_all();

  if (nino_audio_init() != ESP_OK) {
    ESP_LOGW(TAG,
             "Speaker (BSP audio) init failed; POST /play_wav may not work");
  }
  (void)nino_audio_load_saved_volume();
  ESP_ERROR_CHECK(nino_audio_queue_start());
  if (s_sta_connected && s_wifi_connected_chime_pending) {
    if (play_wifi_connected_clip()) {
      s_wifi_connected_chime_pending = false;
    }
  }
  if (nino_touch_sensor_start() != ESP_OK) {
    ESP_LOGW(TAG, "QT2120 touch sensor task not started");
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
      .skip_phy_setup = false,
      .intr_flags = ESP_INTR_FLAG_LOWMED,
  };
  ESP_ERROR_CHECK(usb_host_install(&usb_host_config));

  /* Lib task must run before clients enumerate hub downstream devices (camera + U2D2). */
  BaseType_t ok = xTaskCreatePinnedToCore(
      usb_lib_task, "usb_lib", USB_LIB_TASK_STACK_SIZE, NULL,
      USB_LIB_TASK_PRIORITY, NULL, APP_CORE_USB);
  assert(ok == pdPASS);
  vTaskDelay(pdMS_TO_TICKS(300));

  if (nino_servo_dxl_start() != ESP_OK) {
    ESP_LOGW(TAG, "Dynamixel servo task not started (connect U2D2 on J18 USB hub)");
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

  esp_err_t ble_err = wifi_prov_ble_start();
  if (ble_err != ESP_OK && ble_err != ESP_ERR_NOT_SUPPORTED) {
    ESP_LOGW(TAG, "BLE Wi-Fi provisioning not started: %s",
             esp_err_to_name(ble_err));
  }

  /* If no camera plugs in, still start wake after USB/SDIO settle (HP WDT on boot). */
  (void)xTaskCreatePinnedToCore(delayed_voice_wake_task, "wake_delay", 4096, NULL, 3, NULL,
                                APP_CORE_NET);

  ESP_LOGI(TAG, "J18: powered USB hub -> UVC camera + FTDI U2D2 (Dynamixel)");
  ESP_LOGI(
      TAG,
      "Open / in a browser on your camera's IP address (check 'wifi status')");

  /* Boot sequence complete: greet with Hello-home after the Wi-Fi clip. */
  xTaskCreatePinnedToCore(hello_home_task, "hello_home", 4096, NULL, 4, NULL,
                          APP_CORE_NET);
}

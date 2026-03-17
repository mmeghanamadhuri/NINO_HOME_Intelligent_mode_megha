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
#include "esp_wifi.h"
#include "nvs_flash.h"
#include "usb/usb_host.h"
#include "usb/uvc_host.h"

#define WIFI_AP_SSID                     "ESP32_P4_CAM"
#define WIFI_AP_PASS                     "12345678"
#define WIFI_AP_MAX_CONN                 4

#define USB_LIB_TASK_STACK_SIZE          4096
#define USB_LIB_TASK_PRIORITY            20
#define UVC_DRIVER_TASK_STACK_SIZE       4096
#define UVC_DRIVER_TASK_PRIORITY         21
#define UVC_STREAM_TASK_STACK_SIZE       8192
#define UVC_STREAM_TASK_PRIORITY         19

#define UVC_TARGET_WIDTH                 640
#define UVC_TARGET_HEIGHT                480
#define UVC_TARGET_FPS                   30.0f
#define UVC_FRAME_QUEUE_LEN              3
#define UVC_FRAME_BUFFERS                3
#define UVC_URB_COUNT                    8
#define UVC_URB_SIZE                     (16 * 1024)
#define UVC_FRAME_SIZE_BYTES             (64 * 1024)
#define UVC_OPEN_TIMEOUT_MS              5000

#define HTTP_STREAM_BOUNDARY             "frame"
#define HTTP_SERVER_PORT                 80
#define HTTP_STREAM_POLL_MS              25

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
static QueueHandle_t s_frame_queue;
static volatile bool s_device_connected;
static volatile bool s_stream_task_created;
static selected_stream_t s_selected_stream;
static uvc_host_frame_info_t *s_frame_info_list;
static size_t s_frame_info_count;
static SemaphoreHandle_t s_frame_mutex;
static latest_frame_t s_latest_frame;
static httpd_handle_t s_http_server;
static esp_netif_t *s_ap_netif;
static esp_console_repl_t *s_repl;

static const char *INDEX_HTML =
    "<!DOCTYPE html>"
    "<html><head><meta charset=\"utf-8\">"
    "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
    "<title>ESP32-P4 Camera</title>"
    "<style>"
    "body{font-family:system-ui,sans-serif;margin:0;background:#101820;color:#f5f7fa;}"
    "main{max-width:900px;margin:0 auto;padding:24px;}"
    "h1{margin:0 0 8px;font-size:2rem;}"
    "p{color:#b8c2cc;}"
    ".card{background:#17232e;border-radius:16px;padding:16px;box-shadow:0 16px 40px rgba(0,0,0,.25);}"
    "img{display:block;width:100%;height:auto;border-radius:12px;background:#000;}"
    "code{background:#0d141b;padding:2px 6px;border-radius:6px;}"
    "</style></head>"
    "<body><main><h1>ESP32-P4 USB Camera</h1>"
    "<p>Live MJPEG stream from the UVC webcam.</p>"
    "<div class=\"card\"><img src=\"/stream\" alt=\"camera stream\"></div>"
    "<p>Snapshot endpoint: <code>/snapshot.jpg</code></p>"
    "</main></body></html>";

static int cmd_cpu_dump(int argc, char **argv)
{
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
        const char state =
            (task->eCurrentState == eRunning) ? 'R' :
            (task->eCurrentState == eReady) ? 'Y' :
            (task->eCurrentState == eBlocked) ? 'B' :
            (task->eCurrentState == eSuspended) ? 'S' :
            (task->eCurrentState == eDeleted) ? 'D' : '?';
        unsigned long runtime = (unsigned long)task->ulRunTimeCounter;
        unsigned long pct = (total_runtime > 0U) ? (runtime * 100UL) / total_runtime : 0UL;

        printf("%-20s %4d %4u %5c %5u %10lu %4lu%%\n",
               task->pcTaskName,
               (int)task->xCoreID,
               (unsigned)task->uxCurrentPriority,
               state,
               (unsigned)task->usStackHighWaterMark,
               runtime,
               pct);
    }

    printf("Total runtime ticks: %lu\n", (unsigned long)total_runtime);
    free(task_list);
    return 0;
}

static void console_init(void)
{
    esp_console_repl_config_t repl_config = ESP_CONSOLE_REPL_CONFIG_DEFAULT();
    repl_config.prompt = "usb_cam> ";

    esp_console_register_help_command();

    const esp_console_cmd_t cpu_dump_cmd = {
        .command = "cpu_dump",
        .help = "Show current FreeRTOS runtime CPU stats",
        .hint = NULL,
        .func = &cmd_cpu_dump,
        .argtable = NULL,
    };
    ESP_ERROR_CHECK(esp_console_cmd_register(&cpu_dump_cmd));

#if defined(CONFIG_ESP_CONSOLE_UART_DEFAULT) || defined(CONFIG_ESP_CONSOLE_UART_CUSTOM)
    esp_console_dev_uart_config_t uart_config = ESP_CONSOLE_DEV_UART_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_console_new_repl_uart(&uart_config, &repl_config, &s_repl));
#elif defined(CONFIG_ESP_CONSOLE_USB_SERIAL_JTAG)
    esp_console_dev_usb_serial_jtag_config_t usbjtag_config = ESP_CONSOLE_DEV_USB_SERIAL_JTAG_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_console_new_repl_usb_serial_jtag(&usbjtag_config, &repl_config, &s_repl));
#else
    esp_console_dev_uart_config_t uart_config = ESP_CONSOLE_DEV_UART_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_console_new_repl_uart(&uart_config, &repl_config, &s_repl));
#endif

    ESP_ERROR_CHECK(esp_console_start_repl(s_repl));
}

static const char *format_to_str(enum uvc_host_stream_format format)
{
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

static float frame_interval_to_fps(uint32_t frame_interval)
{
    return (frame_interval != 0U) ? (10000000.0f / (float)frame_interval) : 0.0f;
}

static void free_frame_info_list(void)
{
    free(s_frame_info_list);
    s_frame_info_list = NULL;
    s_frame_info_count = 0;
}

static esp_err_t latest_frame_reserve(size_t required)
{
    if (required <= s_latest_frame.capacity) {
        return ESP_OK;
    }

    uint8_t *new_buf = heap_caps_realloc(s_latest_frame.data,
                                         required,
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

static void latest_frame_store(const uvc_host_frame_t *frame)
{
    if (frame == NULL || frame->data == NULL || frame->data_len == 0) {
        return;
    }

    if (xSemaphoreTake(s_frame_mutex, portMAX_DELAY) != pdTRUE) {
        return;
    }

    if (latest_frame_reserve(frame->data_len) != ESP_OK) {
        xSemaphoreGive(s_frame_mutex);
        ESP_LOGE(TAG, "Failed to grow latest frame buffer to %u bytes", (unsigned)frame->data_len);
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
}

static bool latest_frame_copy(uint8_t *dst,
                              size_t dst_capacity,
                              size_t *out_len,
                              uint32_t *out_sequence)
{
    bool ok = false;

    if (xSemaphoreTake(s_frame_mutex, portMAX_DELAY) != pdTRUE) {
        return false;
    }

    if (s_latest_frame.ready && s_latest_frame.format.format == UVC_VS_FORMAT_MJPEG &&
        s_latest_frame.len <= dst_capacity) {
        memcpy(dst, s_latest_frame.data, s_latest_frame.len);
        *out_len = s_latest_frame.len;
        *out_sequence = s_latest_frame.sequence;
        ok = true;
    }

    xSemaphoreGive(s_frame_mutex);
    return ok;
}

static void wifi_event_handler(void *arg,
                               esp_event_base_t event_base,
                               int32_t event_id,
                               void *event_data)
{
    (void)arg;
    (void)event_data;

    if (event_base == WIFI_EVENT && event_id == WIFI_EVENT_AP_START) {
        ESP_LOGI(TAG, "WiFi AP started: SSID=%s", WIFI_AP_SSID);
    } else if (event_base == WIFI_EVENT && event_id == WIFI_EVENT_AP_STACONNECTED) {
        wifi_event_ap_staconnected_t *event = (wifi_event_ap_staconnected_t *)event_data;
        ESP_LOGI(TAG, "Station joined AP, aid=%d", event->aid);
    } else if (event_base == WIFI_EVENT && event_id == WIFI_EVENT_AP_STADISCONNECTED) {
        wifi_event_ap_stadisconnected_t *event = (wifi_event_ap_stadisconnected_t *)event_data;
        ESP_LOGI(TAG, "Station left AP, aid=%d", event->aid);
    }
}

static void start_wifi_ap(void)
{
    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    s_ap_netif = esp_netif_create_default_wifi_ap();
    assert(s_ap_netif != NULL);

    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&cfg));
    ESP_ERROR_CHECK(esp_event_handler_register(WIFI_EVENT, ESP_EVENT_ANY_ID, &wifi_event_handler, NULL));

    wifi_config_t wifi_config = { 0 };
    memcpy(wifi_config.ap.ssid, WIFI_AP_SSID, sizeof(WIFI_AP_SSID) - 1);
    memcpy(wifi_config.ap.password, WIFI_AP_PASS, sizeof(WIFI_AP_PASS) - 1);
    wifi_config.ap.ssid_len = strlen(WIFI_AP_SSID);
    wifi_config.ap.max_connection = WIFI_AP_MAX_CONN;
    wifi_config.ap.authmode = WIFI_AUTH_WPA2_PSK;

    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_AP));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_AP, &wifi_config));
    ESP_ERROR_CHECK(esp_wifi_set_ps(WIFI_PS_NONE));
    ESP_ERROR_CHECK(esp_wifi_start());

    esp_netif_ip_info_t ip_info = { 0 };
    ESP_ERROR_CHECK(esp_netif_get_ip_info(s_ap_netif, &ip_info));
    ESP_LOGI(TAG, "Connect browser to http://" IPSTR "/", IP2STR(&ip_info.ip));
}

static esp_err_t index_handler(httpd_req_t *req)
{
    httpd_resp_set_type(req, "text/html; charset=utf-8");
    return httpd_resp_send(req, INDEX_HTML, HTTPD_RESP_USE_STRLEN);
}

static esp_err_t snapshot_handler(httpd_req_t *req)
{
    uint8_t *buffer = heap_caps_malloc(UVC_FRAME_SIZE_BYTES, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    size_t frame_len = 0;
    uint32_t frame_seq = 0;
    esp_err_t err;

    if (buffer == NULL) {
        return httpd_resp_send_500(req);
    }

    if (!latest_frame_copy(buffer, UVC_FRAME_SIZE_BYTES, &frame_len, &frame_seq)) {
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

static esp_err_t stream_handler(httpd_req_t *req)
{
    static const char *stream_type = "multipart/x-mixed-replace;boundary=" HTTP_STREAM_BOUNDARY;
    uint8_t *buffer = heap_caps_malloc(UVC_FRAME_SIZE_BYTES, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
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

        if (!latest_frame_copy(buffer, UVC_FRAME_SIZE_BYTES, &frame_len, &frame_sequence) ||
            frame_sequence == last_sequence) {
            vTaskDelay(pdMS_TO_TICKS(HTTP_STREAM_POLL_MS));
            continue;
        }

        last_sequence = frame_sequence;

        char part_header[96];
        int header_len = snprintf(part_header,
                                  sizeof(part_header),
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

static void start_http_server(void)
{
    httpd_config_t config = HTTPD_DEFAULT_CONFIG();
    config.server_port = HTTP_SERVER_PORT;
    config.stack_size = 8192;
    config.max_uri_handlers = 8;
    config.recv_wait_timeout = 10;
    config.send_wait_timeout = 10;

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
    const httpd_uri_t snapshot_uri = {
        .uri = "/snapshot.jpg",
        .method = HTTP_GET,
        .handler = snapshot_handler,
        .user_ctx = NULL,
    };

    ESP_ERROR_CHECK(httpd_register_uri_handler(s_http_server, &index_uri));
    ESP_ERROR_CHECK(httpd_register_uri_handler(s_http_server, &stream_uri));
    ESP_ERROR_CHECK(httpd_register_uri_handler(s_http_server, &snapshot_uri));
}

static void usb_lib_task(void *arg)
{
    (void)arg;

    while (true) {
        uint32_t event_flags = 0;
        usb_host_lib_handle_events(portMAX_DELAY, &event_flags);

        if (event_flags & USB_HOST_LIB_EVENT_FLAGS_NO_CLIENTS) {
            usb_host_device_free_all();
        }
        if (event_flags & USB_HOST_LIB_EVENT_FLAGS_ALL_FREE) {
            ESP_LOGI(TAG, "USB host reports all devices freed");
        }
    }
}

static void stream_event_callback(const uvc_host_stream_event_data_t *event, void *user_ctx)
{
    (void)user_ctx;

    switch (event->type) {
    case UVC_HOST_TRANSFER_ERROR:
        ESP_LOGE(TAG, "USB transfer error: %s", esp_err_to_name(event->transfer_error.error));
        break;
    case UVC_HOST_DEVICE_DISCONNECTED:
        ESP_LOGW(TAG, "UVC device disconnected");
        s_device_connected = false;
        ESP_ERROR_CHECK(uvc_host_stream_close(event->device_disconnected.stream_hdl));
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

static bool frame_callback(const uvc_host_frame_t *frame, void *user_ctx)
{
    QueueHandle_t frame_queue = (QueueHandle_t)user_ctx;
    BaseType_t sent = xQueueSendToBack(frame_queue, &frame, 0);
    if (sent != pdPASS) {
        return true;
    }
    return false;
}

static bool select_stream_format(const uvc_host_frame_info_t *frame_list,
                                 size_t frame_count,
                                 uvc_host_stream_format_t *selected_format)
{
    const uvc_host_frame_info_t *best = NULL;
    const uvc_host_frame_info_t *fallback_mjpeg = NULL;

    for (size_t i = 0; i < frame_count; ++i) {
        const uvc_host_frame_info_t *candidate = &frame_list[i];
        float fps = frame_interval_to_fps(candidate->default_interval);

        ESP_LOGI(TAG, "Camera mode %u: %s %ux%u @ %.1f fps",
                 (unsigned)i,
                 format_to_str(candidate->format),
                 candidate->h_res,
                 candidate->v_res,
                 fps);

        if (candidate->format != UVC_VS_FORMAT_MJPEG) {
            continue;
        }

        if (fallback_mjpeg == NULL) {
            fallback_mjpeg = candidate;
        }

        if (candidate->h_res == UVC_TARGET_WIDTH && candidate->v_res == UVC_TARGET_HEIGHT) {
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
            selected_format->fps = (UVC_TARGET_FPS < default_fps) ? UVC_TARGET_FPS : default_fps;
        } else {
            selected_format->fps = default_fps;
        }
    }
    selected_format->format = best->format;
    return true;
}

static void uvc_stream_task(void *arg)
{
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
            .usb = {
                .dev_addr = s_selected_stream.dev_addr,
                .vid = UVC_HOST_ANY_VID,
                .pid = UVC_HOST_ANY_PID,
                .uvc_stream_index = s_selected_stream.stream_index,
            },
            .vs_format = s_selected_stream.format,
            .advanced = {
                .number_of_frame_buffers = UVC_FRAME_BUFFERS,
                .frame_size = UVC_FRAME_SIZE_BYTES,
                .frame_heap_caps = MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT,
                .number_of_urbs = UVC_URB_COUNT,
                .urb_size = UVC_URB_SIZE,
                .user_frame_buffers = NULL,
            },
        };

        ESP_LOGI(TAG, "Opening camera addr=%u stream=%u format=%s %ux%u @ %.1f fps, frame_size=%u, urbs=%u x %u",
                 stream_config.usb.dev_addr,
                 stream_config.usb.uvc_stream_index,
                 format_to_str(stream_config.vs_format.format),
                 stream_config.vs_format.h_res,
                 stream_config.vs_format.v_res,
                 stream_config.vs_format.fps,
                 (unsigned)stream_config.advanced.frame_size,
                 (unsigned)stream_config.advanced.number_of_urbs,
                 (unsigned)stream_config.advanced.urb_size);

        esp_err_t err = uvc_host_stream_open(&stream_config, pdMS_TO_TICKS(UVC_OPEN_TIMEOUT_MS), &stream);
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
                ESP_LOGW(TAG, "Timed out waiting for a UVC frame");
                continue;
            }

            latest_frame_store(frame);

            if ((s_latest_frame.sequence % 30U) == 1U) {
                ESP_LOGI(TAG, "Frame %lu: %ux%u %s len=%u",
                         (unsigned long)s_latest_frame.sequence,
                         frame->vs_format.h_res,
                         frame->vs_format.v_res,
                         format_to_str(frame->vs_format.format),
                         (unsigned)frame->data_len);
            }

            ESP_ERROR_CHECK(uvc_host_frame_return(stream, frame));
        }

        ESP_LOGI(TAG, "Stream loop exiting");
        vTaskDelay(pdMS_TO_TICKS(500));
    }
}

static void uvc_driver_event_callback(const uvc_host_driver_event_data_t *event, void *user_ctx)
{
    (void)user_ctx;

    if (event->type != UVC_HOST_DRIVER_EVENT_DEVICE_CONNECTED) {
        return;
    }

    if (s_device_connected) {
        ESP_LOGW(TAG, "Ignoring additional UVC device on addr=%u", event->device_connected.dev_addr);
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

    ESP_ERROR_CHECK(uvc_host_get_frame_list(event->device_connected.dev_addr,
                                            event->device_connected.uvc_stream_index,
                                            (uvc_host_frame_info_t (*)[])s_frame_info_list,
                                            &s_frame_info_count));

    s_selected_stream.dev_addr = event->device_connected.dev_addr;
    s_selected_stream.stream_index = event->device_connected.uvc_stream_index;
    if (!select_stream_format(s_frame_info_list, s_frame_info_count, &s_selected_stream.format)) {
        ESP_LOGE(TAG, "No MJPEG format available for HTTP streaming");
        free_frame_info_list();
        return;
    }

    ESP_LOGI(TAG, "Selected format: %s %ux%u @ %.1f fps",
             format_to_str(s_selected_stream.format.format),
             s_selected_stream.format.h_res,
             s_selected_stream.format.v_res,
             s_selected_stream.format.fps);

    s_device_connected = true;
    if (!s_stream_task_created) {
        BaseType_t ok = xTaskCreatePinnedToCore(uvc_stream_task,
                                                "uvc_stream",
                                                UVC_STREAM_TASK_STACK_SIZE,
                                                NULL,
                                                UVC_STREAM_TASK_PRIORITY,
                                                &s_stream_task_handle,
                                                tskNO_AFFINITY);
        assert(ok == pdPASS);
        s_stream_task_created = true;
    }
}

void app_main(void)
{
    esp_log_level_set("esp_driver_usb", ESP_LOG_WARN);
    esp_log_level_set("uvc", ESP_LOG_WARN);
    
    esp_err_t err = nvs_flash_init();
    if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        err = nvs_flash_init();
    }
    ESP_ERROR_CHECK(err);

    s_frame_mutex = xSemaphoreCreateMutex();
    assert(s_frame_mutex != NULL);
    s_frame_queue = xQueueCreate(UVC_FRAME_QUEUE_LEN, sizeof(uvc_host_frame_t *));
    assert(s_frame_queue != NULL);

    start_wifi_ap();
    start_http_server();
    console_init();

    ESP_LOGI(TAG, "Installing USB host stack");
    const usb_host_config_t usb_host_config = {
        .skip_phy_setup = false,
        .intr_flags = ESP_INTR_FLAG_LOWMED,
    };
    ESP_ERROR_CHECK(usb_host_install(&usb_host_config));

    BaseType_t ok = xTaskCreatePinnedToCore(usb_lib_task,
                                            "usb_lib",
                                            USB_LIB_TASK_STACK_SIZE,
                                            NULL,
                                            USB_LIB_TASK_PRIORITY,
                                            NULL,
                                            tskNO_AFFINITY);
    assert(ok == pdPASS);

    ESP_LOGI(TAG, "Installing UVC host driver");
    const uvc_host_driver_config_t uvc_driver_config = {
        .driver_task_stack_size = UVC_DRIVER_TASK_STACK_SIZE,
        .driver_task_priority = UVC_DRIVER_TASK_PRIORITY,
        .xCoreID = tskNO_AFFINITY,
        .create_background_task = true,
        .event_cb = uvc_driver_event_callback,
        .user_ctx = NULL,
    };
    ESP_ERROR_CHECK(uvc_host_install(&uvc_driver_config));

    ESP_LOGI(TAG, "Waiting for a UVC camera on J18");
    ESP_LOGI(TAG, "Open / in a browser after joining SSID %s", WIFI_AP_SSID);
}

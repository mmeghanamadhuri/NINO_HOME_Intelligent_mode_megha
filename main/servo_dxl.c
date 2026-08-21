#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>

#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "esp_check.h"
#include "esp_err.h"
#include "esp_log.h"
#include "sdkconfig.h"
#include "usb/usb_helpers.h"
#include "usb/usb_host.h"

#include "battery_endurance.h"
#include "servo_dxl.h"
#include "servo_motion.h"
#include "servo_recplay.h"

#define USB_CLIENT_TASK_STACK_SIZE          8192
#define USB_TASK_PRIORITY                   18

#define FTDI_VID                            0x0403
#define ROBOTIS_VID                         0x16d0
#define ROBOTIS_U2D2_PID                    0x06a7
#define FTDI_DEFAULT_INDEX                  0x0001
#define FTDI_RX_HEADER_SIZE                 2
/** FT232 bulk OUT: byte0 = 0x01 | ((payload_len & 0x3F) << 2) per ftdi_sio. */
#define FTDI_TX_HEADER_SIZE                 1
#define FTDI_TX_MAX_PAYLOAD                 63
#define FTDI_BAUD_BASE                      3000000UL

#define FTDI_SIO_RESET                      0x00
#define FTDI_SIO_MODEM_CTRL                 0x01
#define FTDI_SIO_SET_FLOW_CTRL              0x02
#define FTDI_SIO_SET_BAUD_RATE              0x03
#define FTDI_SIO_SET_DATA                   0x04
#define FTDI_SIO_SET_LATENCY_TIMER          0x09

#define FTDI_SIO_RESET_SIO                  0x0000
#define FTDI_SIO_MODEM_DTR                  0x0001
#define FTDI_SIO_MODEM_RTS                  0x0002
#define FTDI_SIO_MODEM_DTR_ENABLE           0x0100
#define FTDI_SIO_MODEM_RTS_ENABLE           0x0200
#define FTDI_SIO_SET_DATA_8N1               0x0008

#define USB_CLASS_CDC_COMM                  0x02
#ifndef USB_CLASS_VIDEO
#define USB_CLASS_VIDEO                     0x0E
#endif
#define CDC_ACM_SET_LINE_CODING             0x20
#define CDC_ACM_SET_CONTROL_LINE_STATE      0x22
#define CDC_ACM_CONTROL_LINE_DTR            0x0001
#define CDC_ACM_CONTROL_LINE_RTS            0x0002

#define DXL_HEADER_0                        0xFF
#define DXL_HEADER_1                        0xFF

#define DXL_INST_PING                       0x01
#define DXL_INST_READ                       0x02
#define DXL_INST_WRITE                      0x03

#define DXL_PRIMARY_ID                      1
#define DXL_SECONDARY_ID                    2
#define DXL_SERVO_COUNT                     2
#define DXL_DEFAULT_BAUDRATE                1000000
#define DXL_TORQUE_ENABLE_ADDR              24
#define DXL_MOVING_SPEED_ADDR               32
#define DXL_GOAL_POSITION_ADDR              30
#define DXL_PRESENT_POSITION_ADDR           36
#define DXL_CW_ANGLE_LIMIT_ADDR             6
#define DXL_CCW_ANGLE_LIMIT_ADDR            8
#define DXL_TORQUE_ON                       1
#define DXL_TORQUE_OFF                      0
#define DXL_GOAL_MIN                        0
#define DXL_GOAL_MAX                        1023
#define DXL_CENTER_POSITION                 512
#define DXL_WHEEL_SPEED_MIN                 -1023
#define DXL_WHEEL_SPEED_MAX                 1023
#define DXL_POSITION_SPEED_MIN              1
#define DXL_POSITION_SPEED_MAX              1023
#define DXL_DEFAULT_POSITION_SPEED          22
#define DXL_AX_JOINT_CCW_LIMIT              1023

#define DXL_MAX_PARAMS                      64
#define DXL_MAX_PACKET_SIZE                 96
#define DXL_RX_ACCUM_SIZE                   256
#define DXL_APP_POLL_INTERVAL_MS            100
#define DXL_FAST_POLL_INTERVAL_MS           30
#define DXL_ATTACH_RETRY_MS                   750
#define DXL_HUB_SETTLE_ATTEMPTS               80
#define DXL_PING_FAIL_RECONNECT               8
#define DXL_BOOT_HEALTH_SPEED                 28
#define DXL_BOOT_MOVE_TIMEOUT_MS              8000
#define DXL_BOOT_BUS_SETTLE_MS                200
#define DXL_USB_ADDR_LIST_MAX                 16
#define DXL_POSITION_TOLERANCE                15
#define DXL_MOVE_SEGMENT_TIMEOUT_MS           60000
#define DXL_SPIN360_TASK_STACK                4096
#define DXL_SPIN360_TASK_PRIO                 4
#define DXL_TRACK_HON_TASK_STACK              4096
#define DXL_TRACK_HON_TASK_PRIO               4
#define DXL_TRACK_HON_HOLD_MS                 500
#define DXL_BROADCAST_ID                      0xFE
#define DXL_MAX_SCAN_IDS                      32
#define DXL_SCAN_COLLECT_MS                   200

typedef enum {
    DXL_SYNC_IDLE = 0,
    DXL_SYNC_READ_PENDING,
    DXL_SYNC_READ_WAIT_RSP,
    DXL_SYNC_PING_WAIT_RSP,
    DXL_SYNC_SCAN_WAIT_RSP,
} dxl_sync_state_t;

typedef struct {
    volatile dxl_sync_state_t state;
    uint8_t id;
    uint8_t addr;
    uint8_t length;
    uint16_t value;
    esp_err_t result;
} dxl_sync_request_t;

typedef enum {
    DEVICE_ACTION_NONE = 0,
    DEVICE_ACTION_OPEN = 1 << 0,
    DEVICE_ACTION_CLOSE = 1 << 1,
    DEVICE_ACTION_SCAN = 1 << 2,
} device_action_t;

typedef struct {
    volatile bool done;
    esp_err_t result;
} transfer_wait_t;

typedef struct {
    uint8_t id;
    uint8_t error;
    uint16_t param_len;
    uint8_t params[DXL_MAX_PARAMS];
} dynamixel_status_packet_t;

typedef struct {
    usb_host_client_handle_t client_hdl;
    usb_device_handle_t dev_hdl;
    uint8_t dev_addr;
    uint16_t vid;
    uint16_t pid;
    bool is_ftdi;

    uint8_t interface_number;
    uint8_t interface_alt;
    uint8_t ep_in;
    uint8_t ep_out;
    uint16_t ep_mps_in;
    uint16_t ep_mps_out;
    uint8_t interface_class;
    uint8_t interface_subclass;
    uint8_t interface_protocol;
    uint8_t control_interface_number;

    bool device_ready;
    bool device_gone;

    usb_transfer_t *ctrl_xfer;
    usb_transfer_t *bulk_in_xfer;
    usb_transfer_t *bulk_out_xfer;

    uint8_t rx_accum[DXL_RX_ACCUM_SIZE];
    size_t rx_accum_len;

    volatile uint32_t actions;
} ftdi_device_t;

typedef enum {
    SERVO_MODE_POSITION = 0,
} servo_mode_t;

static const char *TAG = "nino_servo";
static const uint8_t s_servo_ids[] = {
    DXL_PRIMARY_ID,
    DXL_SECONDARY_ID,
};
static ftdi_device_t s_ftdi = {0};
static uint32_t s_usb_bus_sig = 0;
static TickType_t s_usb_bus_log_tick = 0;
static bool s_torque_enabled = false; /* joint-mode initialized / bus usable */
static bool s_servo_torque_on[DXL_SERVO_COUNT] = {false, false};
static volatile bool s_torque_update_pending[DXL_SERVO_COUNT] = {false, false};
static volatile bool s_requested_torque_on[DXL_SERVO_COUNT] = {true, true};
static volatile bool s_goal_update_pending[DXL_SERVO_COUNT] = {false};
static volatile int s_requested_goal[DXL_SERVO_COUNT] = {
    DXL_CENTER_POSITION,
    DXL_CENTER_POSITION,
};
static volatile int s_requested_position_speed = DXL_DEFAULT_POSITION_SPEED;
static int s_active_position_speed = DXL_DEFAULT_POSITION_SPEED;
static volatile bool s_position_speed_pending = false;
static int s_ping_fail_streak = 0;
static bool s_usb_bus_settle = false;
static bool s_servo_online[DXL_SERVO_COUNT] = {false, false};
static bool s_servo_move_ok[DXL_SERVO_COUNT] = {false, false};
static volatile bool s_boot_nod_started = false;
static bool s_servo_started = false;
static SemaphoreHandle_t s_goal_mutex;
static dxl_sync_request_t s_sync = {0};
static SemaphoreHandle_t s_sync_mutex;
static SemaphoreHandle_t s_read_done_sem;
static TaskHandle_t s_spin360_task;
static TaskHandle_t s_track_hon_task;
static volatile bool s_track_hon_stop_requested;
static uint8_t s_scan_ids[DXL_MAX_SCAN_IDS];
static size_t s_scan_count;

static void usb_client_task(void *arg);

static void client_event_cb(const usb_host_client_event_msg_t *event_msg, void *arg);
static void ctrl_transfer_cb(usb_transfer_t *transfer);
static void bulk_out_transfer_cb(usb_transfer_t *transfer);
static void bulk_in_transfer_cb(usb_transfer_t *transfer);

static esp_err_t ftdi_open_device(ftdi_device_t *dev);
static void ftdi_close_device(ftdi_device_t *dev);
static esp_err_t ftdi_configure_device(ftdi_device_t *dev, uint32_t baudrate);
static esp_err_t cdc_acm_configure_device(ftdi_device_t *dev, uint32_t baudrate);
static esp_err_t ftdi_start_rx(ftdi_device_t *dev);
static esp_err_t ftdi_uart_write(ftdi_device_t *dev, const uint8_t *data, size_t len);

static size_t dynamixel_build_instruction_packet(uint8_t id, uint8_t instruction, const uint8_t *params, size_t params_len, uint8_t *packet, size_t packet_size);
static void dynamixel_rx_consume(ftdi_device_t *dev, const uint8_t *data, size_t len);
static bool dynamixel_try_parse_packet(ftdi_device_t *dev);
static void dynamixel_handle_status_packet(const dynamixel_status_packet_t *packet);
static esp_err_t dynamixel_send_ping(ftdi_device_t *dev, uint8_t id);
static esp_err_t dynamixel_send_read(ftdi_device_t *dev, uint8_t id, uint8_t addr, uint8_t len);
static esp_err_t dynamixel_send_write8(ftdi_device_t *dev, uint8_t id, uint8_t addr, uint8_t value);
static esp_err_t dynamixel_send_write16(ftdi_device_t *dev, uint8_t id, uint8_t addr, uint16_t value);
static esp_err_t dynamixel_send_write8_all(ftdi_device_t *dev, uint8_t addr, uint8_t value);
static esp_err_t dynamixel_send_write16_all(ftdi_device_t *dev, uint8_t addr, uint16_t value);
static esp_err_t dynamixel_set_joint_mode(ftdi_device_t *dev);
static void dynamixel_queue_goal_all(int goal);
static int clamp_goal(int goal);
static int servo_index_for_id(uint8_t id);
static esp_err_t dynamixel_read_word_blocking(uint8_t id, uint8_t addr, uint16_t *out, TickType_t timeout);
static esp_err_t dynamixel_ping_blocking(uint8_t id, TickType_t timeout);
static esp_err_t dynamixel_scan_chain_blocking(uint8_t *ids, size_t max_ids, size_t *out_count);
static void scan_add_id(uint8_t id);
static void scan_sort_ids(uint8_t *ids, size_t count);
static bool dynamixel_wait_servo_at(uint8_t id, int target, TickType_t timeout_ms);
static bool track_hon_wait_target_or_stop(uint8_t id, int target, TickType_t timeout_ms);
static void spin360_task(void *arg);
static void track_hon_task(void *arg);
static bool usb_is_obvious_non_u2d2(uint16_t vid, uint16_t pid, uint8_t dev_class);
static bool usb_is_robotis_u2d2(uint16_t vid, uint16_t pid);
static bool usb_is_u2d2_candidate(uint16_t vid, uint16_t pid);
static bool usb_iface_is_cdc_acm(const ftdi_device_t *dev);
static bool usb_device_uses_ftdi_sio(const ftdi_device_t *dev);
static const char *usb_speed_str(usb_speed_t speed);

static uint8_t dynamixel_v1_checksum(const uint8_t *data, size_t len)
{
    uint32_t sum = 0;

    for (size_t i = 0; i < len; i++) {
        sum += data[i];
    }

    return (uint8_t)(~sum & 0xFF);
}

static const char *usb_speed_str(usb_speed_t speed)
{
    switch (speed) {
    case USB_SPEED_HIGH:
        return "HS";
    case USB_SPEED_FULL:
        return "FS";
    case USB_SPEED_LOW:
        return "LS";
    default:
        return "?";
    }
}

static bool usb_is_obvious_non_u2d2(uint16_t vid, uint16_t pid, uint8_t dev_class)
{
    (void)pid;
    /* Never claim the hub or UVC camera — that hides the other downstream device. */
    if (dev_class == USB_CLASS_HUB || dev_class == USB_CLASS_VIDEO) {
        return true;
    }
    /* USB hub bridge chips and common UVC camera vendors — not the Dynamixel adapter. */
    if (vid == 0x046d || vid == 0x03eb || vid == 0x1a40 || vid == 0x05e3 ||
        vid == 0x2109 || vid == 0x0bda || vid == 0x1a86 || vid == 0x0424 ||
        vid == 0x174c || vid == 0x2357 || vid == 0x214b || vid == 0x14cd ||
        vid == 0x05ac || vid == 0x0a12 || vid == 0x1d6b || vid == 0x8087) {
        return true;
    }
    return false;
}

static bool usb_is_robotis_u2d2(uint16_t vid, uint16_t pid)
{
    return vid == ROBOTIS_VID && pid == ROBOTIS_U2D2_PID;
}

static bool usb_is_u2d2_candidate(uint16_t vid, uint16_t pid)
{
    if (vid == FTDI_VID) {
        return true;
    }
    /* USB-C and Micro-B U2D2: FT232HL with ROBOTIS EEPROM 16d0:06a7. */
    return usb_is_robotis_u2d2(vid, pid);
}

static bool usb_iface_is_cdc_acm(const ftdi_device_t *dev)
{
    return dev->interface_class == USB_CLASS_CDC_DATA &&
           dev->control_interface_number != 0xFF;
}

/** True when the chip speaks FTDI SIO (not native CDC ACM). */
static bool usb_device_uses_ftdi_sio(const ftdi_device_t *dev)
{
    if (dev->vid == FTDI_VID) {
        return true;
    }
    /* New U2D2 keeps FTDI protocol; only the VID/PID in EEPROM changed. */
    if (usb_is_robotis_u2d2(dev->vid, dev->pid) && !usb_iface_is_cdc_acm(dev)) {
        return true;
    }
    return false;
}

static int clamp_goal(int goal)
{
    if (goal < DXL_GOAL_MIN) {
        return DXL_GOAL_MIN;
    }
    if (goal > DXL_GOAL_MAX) {
        return DXL_GOAL_MAX;
    }
    return goal;
}

static void dynamixel_queue_goal_all(int goal)
{
    goal = clamp_goal(goal);
    if (s_goal_mutex != NULL) {
        xSemaphoreTake(s_goal_mutex, portMAX_DELAY);
    }
    for (size_t i = 0; i < DXL_SERVO_COUNT; i++) {
        s_requested_goal[i] = goal;
        s_goal_update_pending[i] = true;
    }
    if (s_goal_mutex != NULL) {
        xSemaphoreGive(s_goal_mutex);
    }
}

bool nino_servo_dxl_is_ready(void)
{
    return s_torque_enabled && s_ftdi.device_ready && !s_ftdi.device_gone;
}

bool nino_servo_dxl_id_is_online(uint8_t id)
{
    const int idx = servo_index_for_id(id);
    if (idx < 0) {
        return false;
    }
    return s_servo_online[idx];
}

bool nino_servo_dxl_id_moved_ok(uint8_t id)
{
    const int idx = servo_index_for_id(id);
    if (idx < 0) {
        return false;
    }
    return s_servo_move_ok[idx];
}

static const char *servo_axis_name(uint8_t id)
{
    return (id == DXL_PRIMARY_ID) ? "tilt" : "pan";
}

bool nino_servo_dxl_bus_open(void)
{
    return s_ftdi.device_ready && !s_ftdi.device_gone;
}

bool nino_servo_dxl_spin_is_active(void)
{
    return s_spin360_task != NULL;
}

bool nino_servo_dxl_track_hon_is_active(void)
{
    return s_track_hon_task != NULL;
}

void nino_servo_dxl_go_neutral(void)
{
    /* Audio-playback cleanup and head-motion stop call this; while a 360 spin
     * is running it must not yank ID2 back to center mid-rotation (the spin
     * ends at neutral anyway). Same for the GPIO48 hardware test sweep. */
    if (nino_servo_dxl_spin_is_active() || nino_servo_dxl_track_hon_is_active() ||
        nino_servo_recplay_is_busy() || nino_battery_endurance_owns_actuators()) {
        return;
    }
    dynamixel_queue_goal_all(DXL_CENTER_POSITION);
}

void nino_servo_dxl_set_pan_tilt(int pan_goal, int tilt_goal)
{
    /* Head-motion poses must not override spin / hand-teach. Play mode is allowed. */
    if (nino_servo_dxl_spin_is_active() || nino_servo_dxl_track_hon_is_active()) {
        return;
    }
    if (nino_servo_recplay_mode() == NINO_SERVO_MODE_RECORD) {
        return;
    }
    if (s_goal_mutex != NULL) {
        xSemaphoreTake(s_goal_mutex, portMAX_DELAY);
    }
    /* Head wiring: ID1 = tilt (pitch), ID2 = pan (yaw). */
    s_requested_goal[0] = clamp_goal(tilt_goal);
    s_requested_goal[1] = clamp_goal(pan_goal);
    s_goal_update_pending[0] = true;
    s_goal_update_pending[1] = true;
    if (s_goal_mutex != NULL) {
        xSemaphoreGive(s_goal_mutex);
    }
}

void nino_servo_dxl_set_position_speed(int speed)
{
    if (speed < DXL_POSITION_SPEED_MIN) {
        speed = DXL_POSITION_SPEED_MIN;
    } else if (speed > DXL_POSITION_SPEED_MAX) {
        speed = DXL_POSITION_SPEED_MAX;
    }

    if (s_goal_mutex != NULL) {
        xSemaphoreTake(s_goal_mutex, portMAX_DELAY);
    }
    s_requested_position_speed = speed;
    s_position_speed_pending = true;
    if (s_goal_mutex != NULL) {
        xSemaphoreGive(s_goal_mutex);
    }
}

void nino_servo_dxl_set_servo_goal(uint8_t id, int goal)
{
    const int idx = servo_index_for_id(id);
    if (idx < 0) {
        return;
    }

    goal = clamp_goal(goal);
    if (s_goal_mutex != NULL) {
        xSemaphoreTake(s_goal_mutex, portMAX_DELAY);
    }
    s_requested_goal[idx] = goal;
    s_goal_update_pending[idx] = true;
    if (s_goal_mutex != NULL) {
        xSemaphoreGive(s_goal_mutex);
    }
}

esp_err_t nino_servo_dxl_get_present_position(uint8_t id, int *position)
{
    if (position == NULL) {
        return ESP_ERR_INVALID_ARG;
    }

    uint16_t raw = 0;
    esp_err_t err = dynamixel_read_word_blocking(id, DXL_PRESENT_POSITION_ADDR, &raw,
                                                 pdMS_TO_TICKS(2000));
    if (err != ESP_OK) {
        return err;
    }

    *position = (int)raw;
    return ESP_OK;
}

bool nino_servo_dxl_torque_is_on(uint8_t id)
{
    const int idx = servo_index_for_id(id);
    if (idx < 0) {
        return false;
    }
    return s_servo_torque_on[idx];
}

esp_err_t nino_servo_dxl_set_torque(uint8_t id, bool enable)
{
    const int idx = servo_index_for_id(id);
    if (idx < 0) {
        return ESP_ERR_INVALID_ARG;
    }
    if (!s_ftdi.device_ready || s_ftdi.device_gone || !s_torque_enabled) {
        return ESP_ERR_INVALID_STATE;
    }

    if (s_goal_mutex != NULL) {
        xSemaphoreTake(s_goal_mutex, portMAX_DELAY);
    }
    s_requested_torque_on[idx] = enable;
    s_torque_update_pending[idx] = true;
    if (s_goal_mutex != NULL) {
        xSemaphoreGive(s_goal_mutex);
    }

    /* Wait briefly for the USB worker to apply the write. */
    const TickType_t deadline = xTaskGetTickCount() + pdMS_TO_TICKS(1500);
    while (xTaskGetTickCount() < deadline) {
        if (!s_torque_update_pending[idx] && s_servo_torque_on[idx] == enable) {
            return ESP_OK;
        }
        vTaskDelay(pdMS_TO_TICKS(20));
    }
    return s_servo_torque_on[idx] == enable ? ESP_OK : ESP_ERR_TIMEOUT;
}

esp_err_t nino_servo_dxl_scan_chain(uint8_t *ids, size_t max_ids, size_t *out_count)
{
    return dynamixel_scan_chain_blocking(ids, max_ids, out_count);
}

esp_err_t nino_servo_dxl_spin_360(void)
{
    if (s_spin360_task != NULL || s_track_hon_task != NULL || nino_servo_recplay_is_busy()) {
        return ESP_ERR_INVALID_STATE;
    }
    if (!nino_servo_dxl_is_ready()) {
        return ESP_ERR_INVALID_STATE;
    }

    BaseType_t ok = xTaskCreate(spin360_task, "servo_360", DXL_SPIN360_TASK_STACK, NULL,
                                DXL_SPIN360_TASK_PRIO, &s_spin360_task);
    if (ok != pdPASS) {
        s_spin360_task = NULL;
        return ESP_ERR_NO_MEM;
    }

    return ESP_OK;
}

esp_err_t nino_servo_dxl_track_hon(void)
{
    if (s_track_hon_task != NULL || s_spin360_task != NULL || nino_servo_recplay_is_busy()) {
        return ESP_ERR_INVALID_STATE;
    }
    if (!nino_servo_dxl_is_ready()) {
        return ESP_ERR_INVALID_STATE;
    }

    /* Freeze any current LRUD/NOD motion while track-hon path is active. */
    nino_servo_motion_stop();
    s_track_hon_stop_requested = false;

    BaseType_t ok = xTaskCreate(track_hon_task, "servo_hon", DXL_TRACK_HON_TASK_STACK, NULL,
                                DXL_TRACK_HON_TASK_PRIO, &s_track_hon_task);
    if (ok != pdPASS) {
        s_track_hon_task = NULL;
        return ESP_ERR_NO_MEM;
    }

    return ESP_OK;
}

esp_err_t nino_servo_dxl_track_hon_stop(void)
{
    if (s_track_hon_task == NULL) {
        return ESP_ERR_INVALID_STATE;
    }
    s_track_hon_stop_requested = true;
    return ESP_OK;
}

static int servo_index_for_id(uint8_t id)
{
    for (size_t i = 0; i < DXL_SERVO_COUNT; i++) {
        if (s_servo_ids[i] == id) {
            return (int)i;
        }
    }
    return -1;
}

static int position_delta(int current, int target)
{
    int delta = current - target;
    if (delta < 0) {
        delta = -delta;
    }
    return delta;
}

static bool dynamixel_wait_servo_at(uint8_t id, int target, TickType_t timeout_ms)
{
    const TickType_t start = xTaskGetTickCount();

    while ((xTaskGetTickCount() - start) < timeout_ms) {
        int pos = 0;
        if (nino_servo_dxl_get_present_position(id, &pos) == ESP_OK) {
            if (position_delta(pos, target) <= DXL_POSITION_TOLERANCE) {
                return true;
            }
        }
        vTaskDelay(pdMS_TO_TICKS(80));
    }

    return false;
}

static bool track_hon_wait_target_or_stop(uint8_t id, int target, TickType_t timeout_ms)
{
    const TickType_t start = xTaskGetTickCount();

    while ((xTaskGetTickCount() - start) < timeout_ms) {
        if (s_track_hon_stop_requested) {
            return false;
        }
        int pos = 0;
        if (nino_servo_dxl_get_present_position(id, &pos) == ESP_OK) {
            if (position_delta(pos, target) <= DXL_POSITION_TOLERANCE) {
                return true;
            }
        }
        vTaskDelay(pdMS_TO_TICKS(80));
    }

    return false;
}

static void spin360_task(void *arg)
{
    (void)arg;
    const uint8_t servo_id = DXL_SECONDARY_ID;
    static const int waypoints[] = {
        DXL_CENTER_POSITION,
        DXL_GOAL_MIN,
        DXL_GOAL_MAX,
        DXL_CENTER_POSITION,
    };

    ESP_LOGI(TAG, "ID%u 360 spin start — neutral=%d, path 512→0→1023→512",
             servo_id, DXL_CENTER_POSITION);

    if (!nino_servo_dxl_is_ready()) {
        ESP_LOGW(TAG, "ID%u 360 spin aborted — servos not ready", servo_id);
        goto done;
    }

    int pos = 0;
    if (nino_servo_dxl_get_present_position(servo_id, &pos) == ESP_OK) {
        if (position_delta(pos, DXL_CENTER_POSITION) > DXL_POSITION_TOLERANCE) {
            ESP_LOGI(TAG, "ID%u not at neutral (%d) — moving to %d", servo_id, pos,
                     DXL_CENTER_POSITION);
            nino_servo_dxl_set_servo_goal(servo_id, DXL_CENTER_POSITION);
            if (!dynamixel_wait_servo_at(servo_id, DXL_CENTER_POSITION,
                                         pdMS_TO_TICKS(DXL_MOVE_SEGMENT_TIMEOUT_MS))) {
                ESP_LOGW(TAG, "ID%u failed to reach neutral before 360 spin", servo_id);
                goto done;
            }
        }
    } else {
        ESP_LOGW(TAG, "ID%u present position read failed — homing to %d", servo_id,
                 DXL_CENTER_POSITION);
        nino_servo_dxl_set_servo_goal(servo_id, DXL_CENTER_POSITION);
        vTaskDelay(pdMS_TO_TICKS(1500));
    }

    for (size_t i = 1; i < sizeof(waypoints) / sizeof(waypoints[0]); i++) {
        const int goal = waypoints[i];
        ESP_LOGI(TAG, "ID%u moving to %d", servo_id, goal);
        nino_servo_dxl_set_servo_goal(servo_id, goal);
        if (!dynamixel_wait_servo_at(servo_id, goal, pdMS_TO_TICKS(DXL_MOVE_SEGMENT_TIMEOUT_MS))) {
            ESP_LOGW(TAG, "ID%u timed out reaching %d during 360 spin", servo_id, goal);
            break;
        }
    }

    ESP_LOGI(TAG, "ID%u 360 spin finished", servo_id);

done:
    s_spin360_task = NULL;
    vTaskDelete(NULL);
}

static void track_hon_task(void *arg)
{
    (void)arg;
    const uint8_t servo_id = DXL_SECONDARY_ID;
    static const int waypoints[] = {512, 212, 512, 800, 512};

    ESP_LOGI(TAG, "ID%u track hon start — path 512->212->512->800->512", servo_id);

    if (!nino_servo_dxl_is_ready()) {
        ESP_LOGW(TAG, "ID%u track hon aborted — servos not ready", servo_id);
        goto done;
    }

    while (!s_track_hon_stop_requested) {
        for (size_t i = 0; i < sizeof(waypoints) / sizeof(waypoints[0]); i++) {
            if (s_track_hon_stop_requested) {
                break;
            }
            const int goal = waypoints[i];
            ESP_LOGI(TAG, "ID%u moving to %d", servo_id, goal);
            nino_servo_dxl_set_servo_goal(servo_id, goal);
            if (!track_hon_wait_target_or_stop(servo_id, goal,
                                               pdMS_TO_TICKS(DXL_MOVE_SEGMENT_TIMEOUT_MS))) {
                if (s_track_hon_stop_requested) {
                    break;
                }
                ESP_LOGW(TAG, "ID%u timed out reaching %d during track hon", servo_id, goal);
                goto done;
            }

            const TickType_t hold_start = xTaskGetTickCount();
            while (!s_track_hon_stop_requested &&
                   (xTaskGetTickCount() - hold_start) < pdMS_TO_TICKS(DXL_TRACK_HON_HOLD_MS)) {
                vTaskDelay(pdMS_TO_TICKS(20));
            }
        }
    }

    if (s_track_hon_stop_requested) {
        ESP_LOGI(TAG, "ID%u track hon stop requested", servo_id);
        vTaskDelay(pdMS_TO_TICKS(2000));
        nino_servo_dxl_set_servo_goal(servo_id, DXL_CENTER_POSITION);
        if (!dynamixel_wait_servo_at(servo_id, DXL_CENTER_POSITION,
                                     pdMS_TO_TICKS(DXL_MOVE_SEGMENT_TIMEOUT_MS))) {
            ESP_LOGW(TAG, "ID%u neutral return timeout after hstop", servo_id);
        }
        ESP_LOGI(TAG, "ID%u track hon stopped and returned to %d", servo_id,
                 DXL_CENTER_POSITION);
    }

done:
    s_track_hon_stop_requested = false;
    s_track_hon_task = NULL;
    vTaskDelete(NULL);
}

static void scan_add_id(uint8_t id)
{
    if (id == 0 || id == DXL_BROADCAST_ID) {
        return;
    }

    for (size_t i = 0; i < s_scan_count; i++) {
        if (s_scan_ids[i] == id) {
            return;
        }
    }

    if (s_scan_count < DXL_MAX_SCAN_IDS) {
        s_scan_ids[s_scan_count++] = id;
    }
}

static void scan_sort_ids(uint8_t *ids, size_t count)
{
    for (size_t i = 0; i + 1 < count; i++) {
        for (size_t j = i + 1; j < count; j++) {
            if (ids[j] < ids[i]) {
                const uint8_t tmp = ids[i];
                ids[i] = ids[j];
                ids[j] = tmp;
            }
        }
    }
}

static esp_err_t dynamixel_scan_chain_blocking(uint8_t *ids, size_t max_ids, size_t *out_count)
{
    if (ids == NULL || out_count == NULL || max_ids == 0) {
        return ESP_ERR_INVALID_ARG;
    }
    if (s_sync_mutex == NULL) {
        return ESP_ERR_INVALID_STATE;
    }
    if (!s_ftdi.device_ready || s_ftdi.client_hdl == NULL) {
        *out_count = 0;
        return ESP_ERR_INVALID_STATE;
    }

    *out_count = 0;

    xSemaphoreTake(s_sync_mutex, portMAX_DELAY);
    if (s_sync.state != DXL_SYNC_IDLE) {
        xSemaphoreGive(s_sync_mutex);
        return ESP_ERR_INVALID_STATE;
    }
    s_sync.state = DXL_SYNC_SCAN_WAIT_RSP;
    s_scan_count = 0;
    xSemaphoreGive(s_sync_mutex);

    esp_err_t err = dynamixel_send_ping(&s_ftdi, DXL_BROADCAST_ID);
    if (err != ESP_OK) {
        xSemaphoreTake(s_sync_mutex, portMAX_DELAY);
        s_sync.state = DXL_SYNC_IDLE;
        xSemaphoreGive(s_sync_mutex);
        return err;
    }

    const TickType_t deadline = xTaskGetTickCount() + pdMS_TO_TICKS(DXL_SCAN_COLLECT_MS);
    while (xTaskGetTickCount() < deadline) {
        if (s_ftdi.device_gone) {
            break;
        }
        (void)usb_host_client_handle_events(s_ftdi.client_hdl, pdMS_TO_TICKS(10));
    }

    xSemaphoreTake(s_sync_mutex, portMAX_DELAY);
    s_sync.state = DXL_SYNC_IDLE;
    const size_t found = s_scan_count;
    const size_t copy_count = (found < max_ids) ? found : max_ids;
    for (size_t i = 0; i < copy_count; i++) {
        ids[i] = s_scan_ids[i];
    }
    xSemaphoreGive(s_sync_mutex);

    scan_sort_ids(ids, copy_count);
    *out_count = copy_count;
    return (copy_count > 0) ? ESP_OK : ESP_ERR_NOT_FOUND;
}

static esp_err_t dynamixel_ping_blocking(uint8_t id, TickType_t timeout)
{
    if (s_sync_mutex == NULL || s_read_done_sem == NULL) {
        return ESP_ERR_INVALID_STATE;
    }
    if (!s_ftdi.device_ready || s_ftdi.client_hdl == NULL) {
        return ESP_ERR_INVALID_STATE;
    }

    (void)xSemaphoreTake(s_read_done_sem, 0);

    xSemaphoreTake(s_sync_mutex, portMAX_DELAY);
    s_sync.state = DXL_SYNC_PING_WAIT_RSP;
    s_sync.id = id;
    s_sync.result = ESP_FAIL;
    xSemaphoreGive(s_sync_mutex);

    esp_err_t err = dynamixel_send_ping(&s_ftdi, id);
    if (err != ESP_OK) {
        xSemaphoreTake(s_sync_mutex, portMAX_DELAY);
        s_sync.state = DXL_SYNC_IDLE;
        xSemaphoreGive(s_sync_mutex);
        return err;
    }

    const TickType_t start = xTaskGetTickCount();
    while ((xTaskGetTickCount() - start) < timeout) {
        if (xSemaphoreTake(s_read_done_sem, pdMS_TO_TICKS(20)) == pdTRUE) {
            xSemaphoreTake(s_sync_mutex, portMAX_DELAY);
            const esp_err_t rsp = s_sync.result;
            s_sync.state = DXL_SYNC_IDLE;
            xSemaphoreGive(s_sync_mutex);
            return rsp;
        }
        if (s_ftdi.device_gone) {
            break;
        }
        (void)usb_host_client_handle_events(s_ftdi.client_hdl, pdMS_TO_TICKS(20));
    }

    xSemaphoreTake(s_sync_mutex, portMAX_DELAY);
    s_sync.state = DXL_SYNC_IDLE;
    xSemaphoreGive(s_sync_mutex);
    return ESP_ERR_TIMEOUT;
}

static esp_err_t dynamixel_read_word_blocking(uint8_t id, uint8_t addr, uint16_t *out,
                                              TickType_t timeout)
{
    if (out == NULL || s_sync_mutex == NULL || s_read_done_sem == NULL) {
        return ESP_ERR_INVALID_STATE;
    }
    /* Present-position reads must work with torque off (hand teach / record). */
    if (!s_ftdi.device_ready || s_ftdi.device_gone) {
        return ESP_ERR_INVALID_STATE;
    }

    (void)xSemaphoreTake(s_read_done_sem, 0);

    xSemaphoreTake(s_sync_mutex, portMAX_DELAY);
    s_sync.state = DXL_SYNC_READ_PENDING;
    s_sync.id = id;
    s_sync.addr = addr;
    s_sync.length = 2;
    s_sync.result = ESP_FAIL;
    xSemaphoreGive(s_sync_mutex);

    if (xSemaphoreTake(s_read_done_sem, timeout) != pdTRUE) {
        xSemaphoreTake(s_sync_mutex, portMAX_DELAY);
        s_sync.state = DXL_SYNC_IDLE;
        xSemaphoreGive(s_sync_mutex);
        return ESP_ERR_TIMEOUT;
    }

    xSemaphoreTake(s_sync_mutex, portMAX_DELAY);
    const esp_err_t err = s_sync.result;
    if (err == ESP_OK) {
        *out = s_sync.value;
    }
    s_sync.state = DXL_SYNC_IDLE;
    xSemaphoreGive(s_sync_mutex);
    return err;
}

static uint16_t ftdi_encode_baudrate(uint32_t baudrate)
{
    static const uint16_t frac_code[8] = {0, 3, 2, 4, 1, 5, 6, 7};

    if (baudrate == 0) {
        baudrate = DXL_DEFAULT_BAUDRATE;
    }

    uint32_t divisor_x8 = (uint32_t)(((uint64_t)FTDI_BAUD_BASE * 8ULL + (baudrate / 2ULL)) / baudrate);
    if (divisor_x8 == 0) {
        divisor_x8 = 1;
    }

    return (uint16_t)((divisor_x8 >> 3) | (frac_code[divisor_x8 & 0x7] << 14));
}

static esp_err_t wait_for_transfer(ftdi_device_t *dev, transfer_wait_t *waiter, TickType_t timeout_ticks)
{
    TickType_t start = xTaskGetTickCount();

    while (!waiter->done) {
        TickType_t now = xTaskGetTickCount();
        if ((now - start) >= timeout_ticks) {
            return ESP_ERR_TIMEOUT;
        }

        esp_err_t err = usb_host_client_handle_events(dev->client_hdl, pdMS_TO_TICKS(50));
        if (err != ESP_OK && err != ESP_ERR_TIMEOUT) {
            return err;
        }
        if (dev->device_gone) {
            return ESP_ERR_INVALID_STATE;
        }
    }

    return waiter->result;
}

static esp_err_t ftdi_control_transfer(ftdi_device_t *dev,
                                       uint8_t bm_request_type,
                                       uint8_t b_request,
                                       uint16_t w_value,
                                       uint16_t w_index,
                                       const void *payload,
                                       uint16_t payload_len)
{
    if (dev->ctrl_xfer == NULL) {
        return ESP_ERR_INVALID_STATE;
    }

    usb_setup_packet_t *setup = (usb_setup_packet_t *)dev->ctrl_xfer->data_buffer;
    memset(dev->ctrl_xfer->data_buffer, 0, dev->ctrl_xfer->data_buffer_size);

    setup->bmRequestType = bm_request_type;
    setup->bRequest = b_request;
    setup->wValue = w_value;
    setup->wIndex = w_index;
    setup->wLength = payload_len;

    if (payload_len > 0 && payload != NULL) {
        memcpy(dev->ctrl_xfer->data_buffer + USB_SETUP_PACKET_SIZE, payload, payload_len);
    }

    transfer_wait_t waiter = {
        .done = false,
        .result = ESP_FAIL,
    };

    dev->ctrl_xfer->device_handle = dev->dev_hdl;
    dev->ctrl_xfer->bEndpointAddress = 0;
    dev->ctrl_xfer->num_bytes = USB_SETUP_PACKET_SIZE + payload_len;
    dev->ctrl_xfer->callback = ctrl_transfer_cb;
    dev->ctrl_xfer->context = &waiter;
    dev->ctrl_xfer->timeout_ms = 1000;

    ESP_RETURN_ON_ERROR(usb_host_transfer_submit_control(dev->client_hdl, dev->ctrl_xfer), TAG, "control submit failed");
    return wait_for_transfer(dev, &waiter, pdMS_TO_TICKS(1000));
}

static esp_err_t ftdi_configure_device(ftdi_device_t *dev, uint32_t baudrate)
{
    const uint8_t request_type = USB_BM_REQUEST_TYPE_DIR_OUT |
                                 USB_BM_REQUEST_TYPE_TYPE_VENDOR |
                                 USB_BM_REQUEST_TYPE_RECIP_DEVICE;

    ESP_RETURN_ON_ERROR(ftdi_control_transfer(dev, request_type, FTDI_SIO_RESET, FTDI_SIO_RESET_SIO, FTDI_DEFAULT_INDEX, NULL, 0), TAG, "FTDI reset failed");
    ESP_RETURN_ON_ERROR(ftdi_control_transfer(dev, request_type, FTDI_SIO_SET_LATENCY_TIMER, 1, FTDI_DEFAULT_INDEX, NULL, 0), TAG, "FTDI latency failed");
    ESP_RETURN_ON_ERROR(ftdi_control_transfer(dev, request_type, FTDI_SIO_SET_DATA, FTDI_SIO_SET_DATA_8N1, FTDI_DEFAULT_INDEX, NULL, 0), TAG, "FTDI line setup failed");
    ESP_RETURN_ON_ERROR(ftdi_control_transfer(dev, request_type, FTDI_SIO_MODEM_CTRL,
                                              FTDI_SIO_MODEM_DTR | FTDI_SIO_MODEM_RTS |
                                              FTDI_SIO_MODEM_DTR_ENABLE | FTDI_SIO_MODEM_RTS_ENABLE,
                                              FTDI_DEFAULT_INDEX, NULL, 0), TAG, "FTDI modem ctrl failed");
    ESP_RETURN_ON_ERROR(ftdi_control_transfer(dev, request_type, FTDI_SIO_SET_FLOW_CTRL, 0, FTDI_DEFAULT_INDEX, NULL, 0), TAG, "FTDI flow ctrl failed");
    ESP_RETURN_ON_ERROR(ftdi_control_transfer(dev, request_type, FTDI_SIO_SET_BAUD_RATE, ftdi_encode_baudrate(baudrate), FTDI_DEFAULT_INDEX, NULL, 0), TAG, "FTDI baudrate failed");

    ESP_LOGI(TAG, "FTDI configured at %lu baud", (unsigned long)baudrate);
    return ESP_OK;
}

static esp_err_t cdc_acm_configure_device(ftdi_device_t *dev, uint32_t baudrate)
{
    struct {
        uint32_t dw_dte_rate;
        uint8_t b_char_format;
        uint8_t b_parity_type;
        uint8_t b_data_bits;
    } line_coding = {
        .dw_dte_rate = baudrate,
        .b_char_format = 0,
        .b_parity_type = 0,
        .b_data_bits = 8,
    };

    const uint8_t request_type = USB_BM_REQUEST_TYPE_DIR_OUT |
                                 USB_BM_REQUEST_TYPE_TYPE_CLASS |
                                 USB_BM_REQUEST_TYPE_RECIP_INTERFACE;

    ESP_RETURN_ON_ERROR(ftdi_control_transfer(dev,
                                              request_type,
                                              CDC_ACM_SET_LINE_CODING,
                                              0,
                                              dev->control_interface_number,
                                              &line_coding,
                                              sizeof(line_coding)),
                        TAG,
                        "CDC SET_LINE_CODING failed");

    esp_err_t line_state_err = ftdi_control_transfer(dev,
                                                     request_type,
                                                     CDC_ACM_SET_CONTROL_LINE_STATE,
                                                     CDC_ACM_CONTROL_LINE_DTR | CDC_ACM_CONTROL_LINE_RTS,
                                                     dev->control_interface_number,
                                                     NULL,
                                                     0);
    if (line_state_err != ESP_OK) {
        ESP_LOGW(TAG,
                 "CDC SET_CONTROL_LINE_STATE was rejected (%s), continuing anyway",
                 esp_err_to_name(line_state_err));
    }

    ESP_LOGI(TAG, "CDC ACM configured at %lu baud on control interface %u", (unsigned long)baudrate, dev->control_interface_number);
    return ESP_OK;
}

static bool ftdi_find_bulk_interface(ftdi_device_t *dev, const usb_config_desc_t *config_desc)
{
    dev->control_interface_number = 0xFF;

    for (uint8_t intf_num = 0; intf_num < config_desc->bNumInterfaces; intf_num++) {
        int intf_offset = 0;
        const usb_intf_desc_t *intf_desc = usb_parse_interface_descriptor(config_desc, intf_num, 0, &intf_offset);
        if (intf_desc == NULL) {
            continue;
        }

        if (intf_desc->bInterfaceClass == USB_CLASS_CDC_COMM && dev->control_interface_number == 0xFF) {
            dev->control_interface_number = intf_desc->bInterfaceNumber;
        }

        uint8_t ep_in = 0;
        uint8_t ep_out = 0;
        uint16_t ep_mps_in = 0;
        uint16_t ep_mps_out = 0;

        for (int ep_index = 0; ep_index < intf_desc->bNumEndpoints; ep_index++) {
            int ep_offset = intf_offset;
            const usb_ep_desc_t *ep_desc = usb_parse_endpoint_descriptor_by_index(intf_desc, ep_index, config_desc->wTotalLength, &ep_offset);
            if (ep_desc == NULL) {
                continue;
            }

            if ((ep_desc->bmAttributes & USB_BM_ATTRIBUTES_XFERTYPE_MASK) != USB_BM_ATTRIBUTES_XFER_BULK) {
                continue;
            }

            if (USB_EP_DESC_GET_EP_DIR(ep_desc)) {
                ep_in = ep_desc->bEndpointAddress;
                ep_mps_in = USB_EP_DESC_GET_MPS(ep_desc);
            } else {
                ep_out = ep_desc->bEndpointAddress;
                ep_mps_out = USB_EP_DESC_GET_MPS(ep_desc);
            }
        }

        if (ep_in != 0 && ep_out != 0) {
            dev->interface_number = intf_desc->bInterfaceNumber;
            dev->interface_alt = intf_desc->bAlternateSetting;
            dev->ep_in = ep_in;
            dev->ep_out = ep_out;
            dev->ep_mps_in = ep_mps_in;
            dev->ep_mps_out = ep_mps_out;
            dev->interface_class = intf_desc->bInterfaceClass;
            dev->interface_subclass = intf_desc->bInterfaceSubClass;
            dev->interface_protocol = intf_desc->bInterfaceProtocol;
            return true;
        }
    }

    return false;
}

static esp_err_t ftdi_open_device(ftdi_device_t *dev)
{
    const usb_device_desc_t *dev_desc = NULL;
    const usb_config_desc_t *config_desc = NULL;
    bool claimed_ctrl = false;
    bool claimed_data = false;
    esp_err_t err;

    err = usb_host_device_open(dev->client_hdl, dev->dev_addr, &dev->dev_hdl);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "device open failed: %s", esp_err_to_name(err));
        return err;
    }
    err = usb_host_get_device_descriptor(dev->dev_hdl, &dev_desc);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "descriptor read failed: %s", esp_err_to_name(err));
        goto open_fail;
    }

    dev->vid = dev_desc->idVendor;
    dev->pid = dev_desc->idProduct;
    dev->is_ftdi = false;

    if (usb_is_obvious_non_u2d2(dev->vid, dev->pid, dev_desc->bDeviceClass)) {
        ESP_LOGD(TAG, "addr=%u vid=%04x pid=%04x class=0x%02x — hub/camera, skip",
                 dev->dev_addr, dev->vid, dev->pid, dev_desc->bDeviceClass);
        usb_host_device_close(dev->client_hdl, dev->dev_hdl);
        dev->dev_hdl = NULL;
        dev->dev_addr = 0;
        return ESP_ERR_NOT_SUPPORTED;
    }

    if (!usb_is_u2d2_candidate(dev->vid, dev->pid)) {
        ESP_LOGD(TAG, "addr=%u vid=%04x pid=%04x class=0x%02x — not a known U2D2 VID",
                 dev->dev_addr, dev->vid, dev->pid, dev_desc->bDeviceClass);
        usb_host_device_close(dev->client_hdl, dev->dev_hdl);
        dev->dev_hdl = NULL;
        dev->dev_addr = 0;
        return ESP_ERR_NOT_SUPPORTED;
    }

    err = usb_host_get_active_config_descriptor(dev->dev_hdl, &config_desc);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "config descriptor read failed: %s", esp_err_to_name(err));
        goto open_fail;
    }
    if (!ftdi_find_bulk_interface(dev, config_desc)) {
        ESP_LOGW(TAG, "Device %04x:%04x has no bulk IN/OUT pair we can use", dev->vid, dev->pid);
        err = ESP_ERR_NOT_FOUND;
        goto open_fail;
    }

    /* USB-C U2D2 is FT232HL with ROBOTIS 16d0:06a7 — same FTDI SIO as 0403:6014. */
    dev->is_ftdi = usb_device_uses_ftdi_sio(dev);

    usb_device_info_t info = {0};
    const bool have_info = (usb_host_device_info(dev->dev_hdl, &info) == ESP_OK);
    const bool on_hub = have_info && (info.parent.dev_hdl != NULL);
    ESP_LOGI(TAG,
             "U2D2 candidate %04x:%04x class=0x%02x proto=%s speed=%s parent=%s mps_in=%u mps_out=%u",
             dev->vid, dev->pid, dev->interface_class,
             dev->is_ftdi ? "FTDI-SIO" : "CDC",
             have_info ? usb_speed_str(info.speed) : "?",
             on_hub ? "hub" : "root",
             (unsigned)dev->ep_mps_in, (unsigned)dev->ep_mps_out);
    if (on_hub && have_info && info.speed != USB_SPEED_HIGH) {
        ESP_LOGE(TAG,
                 "U2D2 is %s behind a High-Speed hub — ESP-IDF has no Transaction Translator. "
                 "Use a USB 2.0 HS hub that keeps FT232H at High Speed.",
                 usb_speed_str(info.speed));
    }

    if (!dev->is_ftdi && usb_iface_is_cdc_acm(dev)) {
        err = usb_host_interface_claim(dev->client_hdl, dev->dev_hdl, dev->control_interface_number, 0);
        if (err != ESP_OK) {
            ESP_LOGE(TAG, "CDC control claim failed: %s", esp_err_to_name(err));
            goto open_fail;
        }
        claimed_ctrl = true;
    }
    err = usb_host_interface_claim(dev->client_hdl, dev->dev_hdl, dev->interface_number, dev->interface_alt);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "interface claim failed: %s (mps_in=%u mps_out=%u)",
                 esp_err_to_name(err), (unsigned)dev->ep_mps_in, (unsigned)dev->ep_mps_out);
        goto open_fail;
    }
    claimed_data = true;
    err = usb_host_transfer_alloc(USB_SETUP_PACKET_SIZE, 0, &dev->ctrl_xfer);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "control alloc failed: %s", esp_err_to_name(err));
        goto open_fail;
    }
    err = usb_host_transfer_alloc(dev->ep_mps_in, 0, &dev->bulk_in_xfer);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "bulk IN alloc failed: %s", esp_err_to_name(err));
        goto open_fail;
    }
    err = usb_host_transfer_alloc(dev->ep_mps_out + FTDI_TX_HEADER_SIZE, 0, &dev->bulk_out_xfer);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "bulk OUT alloc failed: %s", esp_err_to_name(err));
        goto open_fail;
    }

    dev->bulk_in_xfer->device_handle = dev->dev_hdl;
    dev->bulk_in_xfer->bEndpointAddress = dev->ep_in;
    dev->bulk_in_xfer->callback = bulk_in_transfer_cb;
    dev->bulk_in_xfer->context = dev;

    dev->bulk_out_xfer->device_handle = dev->dev_hdl;
    dev->bulk_out_xfer->bEndpointAddress = dev->ep_out;

    if (dev->is_ftdi) {
        err = ftdi_configure_device(dev, DXL_DEFAULT_BAUDRATE);
        if (err != ESP_OK) {
            ESP_LOGE(TAG, "FTDI configure failed: %s", esp_err_to_name(err));
            goto open_fail;
        }
    } else if (usb_iface_is_cdc_acm(dev)) {
        err = cdc_acm_configure_device(dev, DXL_DEFAULT_BAUDRATE);
        if (err != ESP_OK) {
            ESP_LOGE(TAG, "CDC configure failed: %s", esp_err_to_name(err));
            goto open_fail;
        }
    } else {
        ESP_LOGW(TAG, "U2D2 candidate %04x:%04x class=0x%02x has no usable CDC/FTDI serial path",
                 dev->vid, dev->pid, dev->interface_class);
        err = ESP_ERR_NOT_SUPPORTED;
        goto open_fail;
    }

    dev->device_ready = true;
    dev->device_gone = false;
    dev->rx_accum_len = 0;

    ESP_LOGI(TAG,
             "USB serial candidate ready: addr=%u vid=%04x pid=%04x proto=%s intf=%u class=0x%02x subclass=0x%02x protocol=0x%02x ep_in=0x%02x ep_out=0x%02x",
             dev->dev_addr,
             dev->vid,
             dev->pid,
             dev->is_ftdi ? "FTDI-SIO" : "CDC-ACM",
             dev->interface_number,
             dev->interface_class,
             dev->interface_subclass,
             dev->interface_protocol,
             dev->ep_in,
             dev->ep_out);
    return ESP_OK;

open_fail:
    if (dev->bulk_in_xfer != NULL) {
        usb_host_transfer_free(dev->bulk_in_xfer);
        dev->bulk_in_xfer = NULL;
    }
    if (dev->bulk_out_xfer != NULL) {
        usb_host_transfer_free(dev->bulk_out_xfer);
        dev->bulk_out_xfer = NULL;
    }
    if (dev->ctrl_xfer != NULL) {
        usb_host_transfer_free(dev->ctrl_xfer);
        dev->ctrl_xfer = NULL;
    }
    if (dev->dev_hdl != NULL) {
        if (claimed_ctrl) {
            usb_host_interface_release(dev->client_hdl, dev->dev_hdl, dev->control_interface_number);
        }
        if (claimed_data) {
            usb_host_interface_release(dev->client_hdl, dev->dev_hdl, dev->interface_number);
        }
        usb_host_device_close(dev->client_hdl, dev->dev_hdl);
        dev->dev_hdl = NULL;
    }
    dev->dev_addr = 0;
    return err;
}

static void ftdi_close_device(ftdi_device_t *dev)
{
    dev->device_ready = false;
    dev->device_gone = true;

    if (dev->bulk_in_xfer != NULL) {
        usb_host_transfer_free(dev->bulk_in_xfer);
        dev->bulk_in_xfer = NULL;
    }
    if (dev->bulk_out_xfer != NULL) {
        usb_host_transfer_free(dev->bulk_out_xfer);
        dev->bulk_out_xfer = NULL;
    }
    if (dev->ctrl_xfer != NULL) {
        usb_host_transfer_free(dev->ctrl_xfer);
        dev->ctrl_xfer = NULL;
    }
    if (dev->dev_hdl != NULL) {
        if (dev->control_interface_number != 0xFF) {
            usb_host_interface_release(dev->client_hdl, dev->dev_hdl, dev->control_interface_number);
        }
        usb_host_interface_release(dev->client_hdl, dev->dev_hdl, dev->interface_number);
        usb_host_device_close(dev->client_hdl, dev->dev_hdl);
    }

    dev->dev_hdl = NULL;
    dev->dev_addr = 0;
    dev->vid = 0;
    dev->pid = 0;
    dev->is_ftdi = false;
    dev->interface_number = 0;
    dev->interface_alt = 0;
    dev->ep_in = 0;
    dev->ep_out = 0;
    dev->ep_mps_in = 0;
    dev->ep_mps_out = 0;
    dev->interface_class = 0;
    dev->interface_subclass = 0;
    dev->interface_protocol = 0;
    dev->control_interface_number = 0xFF;
    dev->rx_accum_len = 0;

    ESP_LOGI(TAG, "FTDI/U2D2 closed");
}

static esp_err_t ftdi_start_rx(ftdi_device_t *dev)
{
    if (!dev->device_ready || dev->bulk_in_xfer == NULL) {
        return ESP_ERR_INVALID_STATE;
    }

    dev->bulk_in_xfer->num_bytes = usb_round_up_to_mps(dev->ep_mps_in, dev->ep_mps_in);
    dev->bulk_in_xfer->timeout_ms = 0;
    dev->bulk_in_xfer->callback = bulk_in_transfer_cb;
    dev->bulk_in_xfer->context = dev;

    ESP_RETURN_ON_ERROR(usb_host_transfer_submit(dev->bulk_in_xfer), TAG, "bulk IN submit failed");
    return ESP_OK;
}

static esp_err_t ftdi_uart_write(ftdi_device_t *dev, const uint8_t *data, size_t len)
{
    if (!dev->device_ready || dev->bulk_out_xfer == NULL) {
        return ESP_ERR_INVALID_STATE;
    }
    if (len == 0) {
        return ESP_ERR_INVALID_ARG;
    }
    if (dev->is_ftdi && len > FTDI_TX_MAX_PAYLOAD) {
        return ESP_ERR_INVALID_SIZE;
    }
    const size_t tx_len = dev->is_ftdi ? (len + FTDI_TX_HEADER_SIZE) : len;
    if (tx_len > (size_t)dev->bulk_out_xfer->data_buffer_size) {
        return ESP_ERR_INVALID_SIZE;
    }

    if (dev->is_ftdi) {
        dev->bulk_out_xfer->data_buffer[0] = (uint8_t)(0x01 | ((len & 0x3F) << 2));
        memcpy(dev->bulk_out_xfer->data_buffer + FTDI_TX_HEADER_SIZE, data, len);
    } else {
        memcpy(dev->bulk_out_xfer->data_buffer, data, len);
    }

    transfer_wait_t waiter = {
        .done = false,
        .result = ESP_FAIL,
    };

    dev->bulk_out_xfer->num_bytes = (int)tx_len;
    dev->bulk_out_xfer->actual_num_bytes = 0;
    dev->bulk_out_xfer->flags = 0;
    dev->bulk_out_xfer->timeout_ms = 1000;
    dev->bulk_out_xfer->callback = bulk_out_transfer_cb;
    dev->bulk_out_xfer->context = &waiter;

    ESP_RETURN_ON_ERROR(usb_host_transfer_submit(dev->bulk_out_xfer), TAG, "bulk OUT submit failed");
    return wait_for_transfer(dev, &waiter, pdMS_TO_TICKS(1000));
}

static size_t dynamixel_build_instruction_packet(uint8_t id,
                                                 uint8_t instruction,
                                                 const uint8_t *params,
                                                 size_t params_len,
                                                 uint8_t *packet,
                                                 size_t packet_size)
{
    const size_t total_len = 6 + params_len;
    const uint8_t dxl_len = (uint8_t)(params_len + 2);

    if (packet_size < total_len || params_len > DXL_MAX_PARAMS) {
        return 0;
    }

    packet[0] = DXL_HEADER_0;
    packet[1] = DXL_HEADER_1;
    packet[2] = id;
    packet[3] = dxl_len;
    packet[4] = instruction;
    if (params_len > 0 && params != NULL) {
        memcpy(&packet[5], params, params_len);
    }

    packet[5 + params_len] = dynamixel_v1_checksum(&packet[2], 3 + params_len);
    return total_len;
}

static esp_err_t dynamixel_send_ping(ftdi_device_t *dev, uint8_t id)
{
    uint8_t packet[DXL_MAX_PACKET_SIZE];
    size_t packet_len = dynamixel_build_instruction_packet(id, DXL_INST_PING, NULL, 0, packet, sizeof(packet));
    if (packet_len == 0) {
        return ESP_ERR_INVALID_SIZE;
    }

    ESP_LOGD(TAG, "Sending PING to Dynamixel ID %u", id);
    return ftdi_uart_write(dev, packet, packet_len);
}

static esp_err_t dynamixel_send_read(ftdi_device_t *dev, uint8_t id, uint8_t addr, uint8_t len)
{
    uint8_t params[2] = {
        addr,
        len,
    };
    uint8_t packet[DXL_MAX_PACKET_SIZE];
    size_t packet_len =
        dynamixel_build_instruction_packet(id, DXL_INST_READ, params, sizeof(params), packet, sizeof(packet));
    if (packet_len == 0) {
        return ESP_ERR_INVALID_SIZE;
    }

    ESP_LOGD(TAG, "Reading %u bytes from ID %u addr=%u", len, id, addr);
    return ftdi_uart_write(dev, packet, packet_len);
}

static esp_err_t dynamixel_send_write8(ftdi_device_t *dev, uint8_t id, uint8_t addr, uint8_t value)
{
    uint8_t params[2] = {
        addr,
        value,
    };
    uint8_t packet[DXL_MAX_PACKET_SIZE];
    size_t packet_len = dynamixel_build_instruction_packet(id, DXL_INST_WRITE, params, sizeof(params), packet, sizeof(packet));
    if (packet_len == 0) {
        return ESP_ERR_INVALID_SIZE;
    }

    ESP_LOGI(TAG, "Writing 8-bit value %u to ID %u addr=%u", value, id, addr);
    return ftdi_uart_write(dev, packet, packet_len);
}

static esp_err_t dynamixel_send_write16(ftdi_device_t *dev, uint8_t id, uint8_t addr, uint16_t value)
{
    uint8_t params[3] = {
        addr,
        (uint8_t)(value & 0xFF),
        (uint8_t)((value >> 8) & 0xFF),
    };
    uint8_t packet[DXL_MAX_PACKET_SIZE];
    size_t packet_len = dynamixel_build_instruction_packet(id, DXL_INST_WRITE, params, sizeof(params), packet, sizeof(packet));
    if (packet_len == 0) {
        return ESP_ERR_INVALID_SIZE;
    }

    ESP_LOGI(TAG, "Writing 16-bit value %u to ID %u addr=%u", value, id, addr);
    return ftdi_uart_write(dev, packet, packet_len);
}

static esp_err_t dynamixel_send_write8_all(ftdi_device_t *dev, uint8_t addr, uint8_t value)
{
    esp_err_t first_err = ESP_OK;

    for (size_t i = 0; i < DXL_SERVO_COUNT; i++) {
        esp_err_t err = dynamixel_send_write8(dev, s_servo_ids[i], addr, value);
        if (err != ESP_OK && first_err == ESP_OK) {
            first_err = err;
        }
    }

    return first_err;
}

static esp_err_t dynamixel_send_write16_all(ftdi_device_t *dev, uint8_t addr, uint16_t value)
{
    esp_err_t first_err = ESP_OK;

    for (size_t i = 0; i < DXL_SERVO_COUNT; i++) {
        esp_err_t err = dynamixel_send_write16(dev, s_servo_ids[i], addr, value);
        if (err != ESP_OK && first_err == ESP_OK) {
            first_err = err;
        }
    }

    return first_err;
}

static esp_err_t dynamixel_set_joint_mode(ftdi_device_t *dev)
{
    esp_err_t err = dynamixel_send_write8_all(dev, DXL_TORQUE_ENABLE_ADDR, DXL_TORQUE_OFF);
    if (err != ESP_OK) {
        return err;
    }

    err = dynamixel_send_write16_all(dev, DXL_CW_ANGLE_LIMIT_ADDR, 0);
    if (err != ESP_OK) {
        return err;
    }

    err = dynamixel_send_write16_all(dev, DXL_CCW_ANGLE_LIMIT_ADDR, DXL_AX_JOINT_CCW_LIMIT);
    if (err != ESP_OK) {
        return err;
    }

    err = dynamixel_send_write8_all(dev, DXL_TORQUE_ENABLE_ADDR, DXL_TORQUE_ON);
    if (err == ESP_OK) {
        s_torque_enabled = true;
        for (size_t i = 0; i < DXL_SERVO_COUNT; i++) {
            s_servo_torque_on[i] = true;
            s_requested_torque_on[i] = true;
            s_torque_update_pending[i] = false;
        }
        ESP_LOGI(TAG, "Dynamixel joint (position) mode enabled");
    }

    return err;
}

static void dynamixel_handle_status_packet(const dynamixel_status_packet_t *packet)
{
    if (packet == NULL || s_sync_mutex == NULL || s_read_done_sem == NULL) {
        return;
    }

    if (xSemaphoreTake(s_sync_mutex, pdMS_TO_TICKS(100)) != pdTRUE) {
        return;
    }

    if (s_sync.state == DXL_SYNC_SCAN_WAIT_RSP) {
        if (packet->error == 0) {
            scan_add_id(packet->id);
        }
        xSemaphoreGive(s_sync_mutex);
        return;
    }

    if ((s_sync.state == DXL_SYNC_READ_WAIT_RSP || s_sync.state == DXL_SYNC_PING_WAIT_RSP) &&
        packet->id == s_sync.id) {
        if (packet->error != 0) {
            s_sync.result = ESP_FAIL;
        } else if (s_sync.state == DXL_SYNC_PING_WAIT_RSP) {
            s_sync.result = ESP_OK;
        } else if (packet->param_len >= s_sync.length) {
            s_sync.value = packet->params[0];
            if (s_sync.length >= 2) {
                s_sync.value |= (uint16_t)packet->params[1] << 8;
            }
            s_sync.result = ESP_OK;
        } else {
            s_sync.result = ESP_ERR_INVALID_RESPONSE;
        }
        s_sync.state = DXL_SYNC_IDLE;
        xSemaphoreGive(s_sync_mutex);
        xSemaphoreGive(s_read_done_sem);
        return;
    }

    xSemaphoreGive(s_sync_mutex);
}

static bool dynamixel_try_parse_packet(ftdi_device_t *dev)
{
    uint8_t *buf = dev->rx_accum;
    size_t len = dev->rx_accum_len;

    while (len >= 2) {
        if (buf[0] == DXL_HEADER_0 && buf[1] == DXL_HEADER_1) {
            break;
        }
        memmove(buf, buf + 1, len - 1);
        len--;
    }

    dev->rx_accum_len = len;
    if (len < 6) {
        return false;
    }

    uint8_t declared_len = buf[3];
    size_t total_packet_len = (size_t)declared_len + 4;
    if (total_packet_len > len) {
        return false;
    }

    uint8_t rx_checksum = buf[total_packet_len - 1];
    uint8_t calc_checksum = dynamixel_v1_checksum(&buf[2], total_packet_len - 3);
    if (rx_checksum != calc_checksum) {
        ESP_LOGW(TAG, "Dropping packet with checksum mismatch");
        memmove(buf, buf + 1, len - 1);
        dev->rx_accum_len = len - 1;
        return true;
    }

    dynamixel_status_packet_t status = {
        .id = buf[2],
        .error = buf[4],
        .param_len = (uint16_t)(declared_len - 2),
    };

    if (status.param_len > DXL_MAX_PARAMS) {
        status.param_len = DXL_MAX_PARAMS;
    }
    if (status.param_len > 0) {
        memcpy(status.params, &buf[5], status.param_len);
    }
    dynamixel_handle_status_packet(&status);

    memmove(buf, buf + total_packet_len, len - total_packet_len);
    dev->rx_accum_len = len - total_packet_len;
    return true;
}

static void dynamixel_rx_consume(ftdi_device_t *dev, const uint8_t *data, size_t len)
{
    if (len == 0) {
        return;
    }

    if ((dev->rx_accum_len + len) > sizeof(dev->rx_accum)) {
        ESP_LOGW(TAG, "RX buffer overflow, resetting parser");
        dev->rx_accum_len = 0;
    }

    memcpy(&dev->rx_accum[dev->rx_accum_len], data, len);
    dev->rx_accum_len += len;

    while (dynamixel_try_parse_packet(dev)) {
    }
}

static void ctrl_transfer_cb(usb_transfer_t *transfer)
{
    transfer_wait_t *waiter = (transfer_wait_t *)transfer->context;
    waiter->result = (transfer->status == USB_TRANSFER_STATUS_COMPLETED) ? ESP_OK : ESP_FAIL;
    waiter->done = true;
}

static void bulk_out_transfer_cb(usb_transfer_t *transfer)
{
    transfer_wait_t *waiter = (transfer_wait_t *)transfer->context;
    waiter->result = (transfer->status == USB_TRANSFER_STATUS_COMPLETED) ? ESP_OK : ESP_FAIL;
    waiter->done = true;
}

static void bulk_in_transfer_cb(usb_transfer_t *transfer)
{
    ftdi_device_t *dev = (ftdi_device_t *)transfer->context;

    if (transfer->status == USB_TRANSFER_STATUS_COMPLETED) {
        size_t payload_offset = dev->is_ftdi ? FTDI_RX_HEADER_SIZE : 0;
        if ((size_t)transfer->actual_num_bytes > payload_offset) {
            dynamixel_rx_consume(dev,
                                 transfer->data_buffer + payload_offset,
                                 (size_t)transfer->actual_num_bytes - payload_offset);
        }
    } else if (transfer->status == USB_TRANSFER_STATUS_NO_DEVICE) {
        dev->device_gone = true;
        return;
    } else {
        ESP_LOGW(TAG, "bulk IN transfer status=%d", transfer->status);
    }

    if (dev->device_ready && !dev->device_gone) {
        transfer->num_bytes = usb_round_up_to_mps(dev->ep_mps_in, dev->ep_mps_in);
        transfer->actual_num_bytes = 0;
        esp_err_t err = usb_host_transfer_submit(transfer);
        if (err != ESP_OK) {
            ESP_LOGW(TAG, "bulk IN resubmit failed: %s", esp_err_to_name(err));
        }
    }
}

typedef struct {
    uint16_t vid;
    uint16_t pid;
    uint8_t dev_class;
    usb_speed_t speed;
    bool on_hub;
    uint8_t parent_port;
} servo_usb_peek_t;

static uint8_t s_usb_skip_addr[DXL_USB_ADDR_LIST_MAX];
static uint8_t s_usb_skip_n;

static bool servo_usb_addr_skipped(uint8_t addr)
{
    for (uint8_t i = 0; i < s_usb_skip_n; i++) {
        if (s_usb_skip_addr[i] == addr) {
            return true;
        }
    }
    return false;
}

static void servo_usb_skip_add(uint8_t addr)
{
    if (addr == 0 || servo_usb_addr_skipped(addr) || s_usb_skip_n >= DXL_USB_ADDR_LIST_MAX) {
        return;
    }
    s_usb_skip_addr[s_usb_skip_n++] = addr;
}

static void servo_usb_skip_retain(const uint8_t *alive, int n_alive)
{
    uint8_t kept[DXL_USB_ADDR_LIST_MAX];
    uint8_t n_kept = 0;
    for (uint8_t i = 0; i < s_usb_skip_n; i++) {
        for (int j = 0; j < n_alive; j++) {
            if (alive[j] == s_usb_skip_addr[i]) {
                kept[n_kept++] = s_usb_skip_addr[i];
                break;
            }
        }
    }
    memcpy(s_usb_skip_addr, kept, n_kept);
    s_usb_skip_n = n_kept;
}

static esp_err_t servo_peek_usb_dev(ftdi_device_t *dev, uint8_t addr, servo_usb_peek_t *out)
{
    usb_device_handle_t hdl = NULL;
    memset(out, 0, sizeof(*out));
    esp_err_t err = usb_host_device_open(dev->client_hdl, addr, &hdl);
    if (err != ESP_OK) {
        return err;
    }
    const usb_device_desc_t *desc = NULL;
    err = usb_host_get_device_descriptor(hdl, &desc);
    if (err == ESP_OK) {
        out->vid = desc->idVendor;
        out->pid = desc->idProduct;
        out->dev_class = desc->bDeviceClass;
        usb_device_info_t info = {0};
        if (usb_host_device_info(hdl, &info) == ESP_OK) {
            out->speed = info.speed;
            out->on_hub = (info.parent.dev_hdl != NULL);
            out->parent_port = info.parent.port_num;
        } else {
            out->speed = USB_SPEED_FULL;
            out->on_hub = false;
        }
    }
    usb_host_device_close(dev->client_hdl, hdl);
    return err;
}

/** Scan the USB tree and open U2D2 (FTDI 0403:* or ROBOTIS 16d0:06a7 FT232H). */
static void servo_try_attach_ftdi(ftdi_device_t *dev)
{
    if (dev->client_hdl == NULL || dev->device_ready) {
        return;
    }

    uint8_t addr_list[DXL_USB_ADDR_LIST_MAX];
    uint8_t try_order[DXL_USB_ADDR_LIST_MAX];
    uint16_t try_vid[DXL_USB_ADDR_LIST_MAX];
    int num_dev = 0;
    int num_try = 0;
    int hub_count = 0;
    int behind_hub_count = 0;
    if (usb_host_device_addr_list_fill((int)sizeof(addr_list), addr_list, &num_dev) != ESP_OK ||
        num_dev <= 0) {
        return;
    }

    servo_usb_skip_retain(addr_list, num_dev);

    uint32_t bus_sig = (uint32_t)num_dev;
    const TickType_t now = xTaskGetTickCount();
    const bool log_bus = (bus_sig != s_usb_bus_sig) ||
                         (s_usb_bus_log_tick == 0) ||
                         ((now - s_usb_bus_log_tick) >= pdMS_TO_TICKS(10000));

    if (log_bus) {
        ESP_LOGI(TAG, "USB bus scan: %d device(s)", num_dev);
        s_usb_bus_sig = bus_sig;
        s_usb_bus_log_tick = now;
    }

    for (int i = 0; i < num_dev; i++) {
        if (servo_usb_addr_skipped(addr_list[i])) {
            continue;
        }
        servo_usb_peek_t peek = {0};
        (void)usb_host_client_handle_events(dev->client_hdl, pdMS_TO_TICKS(20));
        esp_err_t peek_err = servo_peek_usb_dev(dev, addr_list[i], &peek);
        if (peek_err != ESP_OK) {
            /* Camera/hub already opened by another client — never reopen it. */
            if (peek_err == ESP_ERR_INVALID_STATE) {
                servo_usb_skip_add(addr_list[i]);
            } else if (log_bus) {
                ESP_LOGW(TAG, "  addr=%u — descriptor peek failed (%s)",
                         addr_list[i], esp_err_to_name(peek_err));
            }
            continue;
        }
        if (peek.dev_class == USB_CLASS_HUB) {
            hub_count++;
        }
        if (peek.on_hub) {
            behind_hub_count++;
        }
        const bool skip = usb_is_obvious_non_u2d2(peek.vid, peek.pid, peek.dev_class);
        const bool candidate = !skip && usb_is_u2d2_candidate(peek.vid, peek.pid);
        if (log_bus) {
            ESP_LOGI(TAG,
                     "  addr=%u %04x:%04x class=%02x speed=%s loc=%s port=%u %s",
                     addr_list[i], peek.vid, peek.pid, peek.dev_class,
                     usb_speed_str(peek.speed),
                     peek.on_hub ? "behind-hub" : "root",
                     (unsigned)peek.parent_port,
                     candidate ? "U2D2-candidate" : (skip ? "skip-hub/camera" : "skip"));
        }
        if (!candidate) {
            servo_usb_skip_add(addr_list[i]);
            continue;
        }
        if (peek.on_hub && peek.speed != USB_SPEED_HIGH) {
            ESP_LOGW(TAG,
                     "U2D2 at addr %u is %s on a hub — ESP-IDF cannot talk FS/LS through an HS hub (no TT)",
                     addr_list[i], usb_speed_str(peek.speed));
        }
        try_order[num_try] = addr_list[i];
        try_vid[num_try] = peek.vid;
        num_try++;
    }

    if (log_bus && num_try == 0) {
        if (num_dev == hub_count) {
            ESP_LOGW(TAG,
                     "Only hub(s) on the bus — U2D2 is still hidden downstream (wait for hub ports)");
        } else if (behind_hub_count == 0) {
            ESP_LOGW(TAG,
                     "No U2D2 candidate yet and no hub children — adapter may still be behind the hub");
        } else {
            ESP_LOGW(TAG,
                     "Hub children visible but none are FTDI 0403 or ROBOTIS 16d0:06a7");
        }
    }

    /* Prefer ROBOTIS U2D2 on the hub over any other serial adapter. */
    for (int pass = 0; pass < 2 && !dev->device_ready; pass++) {
        for (int i = 0; i < num_try; i++) {
            const bool robotis = (try_vid[i] == ROBOTIS_VID);
            if (pass == 0 && !robotis) {
                continue;
            }
            if (pass == 1 && robotis) {
                continue;
            }
            if (dev->device_ready) {
                return;
            }
            (void)usb_host_client_handle_events(dev->client_hdl, pdMS_TO_TICKS(50));
            dev->dev_addr = try_order[i];
            esp_err_t err = ftdi_open_device(dev);
            if (err == ESP_OK) {
                goto attached;
            }
            if (err == ESP_ERR_NOT_SUPPORTED || err == ESP_ERR_INVALID_STATE) {
                servo_usb_skip_add(try_order[i]);
            }
            dev->dev_addr = 0;
            vTaskDelay(pdMS_TO_TICKS(40));
        }
    }
    return;

attached:
    s_ping_fail_streak = 0;
    s_usb_bus_settle = true;
    s_torque_enabled = false;
    s_position_speed_pending = true;
    dynamixel_queue_goal_all(DXL_CENTER_POSITION);
    if (ftdi_start_rx(dev) != ESP_OK) {
        ESP_LOGW(TAG, "bulk IN start failed after U2D2 open");
    }
    ESP_LOGI(TAG, "U2D2 attached addr=%u vid=%04x pid=%04x (homing to %d)", dev->dev_addr, dev->vid,
             dev->pid, DXL_CENTER_POSITION);
}

static void client_event_cb(const usb_host_client_event_msg_t *event_msg, void *arg)
{
    ftdi_device_t *dev = (ftdi_device_t *)arg;

    switch (event_msg->event) {
    case USB_HOST_CLIENT_EVENT_NEW_DEV:
        if (dev->device_ready) {
            break;
        }
        /* Hub enumerates first; camera and U2D2 appear later as children.
         * Do not open this address — it is usually the hub itself. */
        ESP_LOGI(TAG, "USB device at address %u — scan bus for U2D2 behind hub",
                 event_msg->new_dev.address);
        dev->actions |= DEVICE_ACTION_SCAN;
        break;
    case USB_HOST_CLIENT_EVENT_DEV_GONE:
        /* IDF 5.5: dev_gone carries dev_hdl (only for devices this client opened). */
        if (dev->device_ready && dev->dev_hdl != NULL &&
            event_msg->dev_gone.dev_hdl == dev->dev_hdl) {
            dev->device_gone = true;
            dev->actions |= DEVICE_ACTION_CLOSE;
            ESP_LOGW(TAG, "U2D2 removed from hub (addr %u)", dev->dev_addr);
        }
        break;
    default:
        break;
    }
}

static void usb_client_task(void *arg)
{
    (void)arg;
    usb_host_client_config_t client_config = {
        .is_synchronous = false,
        .max_num_event_msg = 16,
        .async = {
            .client_event_callback = client_event_cb,
            .callback_arg = &s_ftdi,
        },
    };

    ESP_ERROR_CHECK(usb_host_client_register(&client_config, &s_ftdi.client_hdl));
#if CONFIG_USB_HOST_HUBS_SUPPORTED
    ESP_LOGI(TAG, "Servo USB client ready — hub support ON, will walk hub children (camera + U2D2)");
#else
    ESP_LOGE(TAG, "CONFIG_USB_HOST_HUBS_SUPPORTED is off — U2D2 behind an external hub will never enumerate");
#endif

    TickType_t last_poll_tick = 0;
    TickType_t last_attach_scan_tick = 0;

    /* Hub downstream devices enumerate after the hub — scan until U2D2 appears. */
    for (int attempt = 0; attempt < DXL_HUB_SETTLE_ATTEMPTS && !s_ftdi.device_ready; attempt++) {
        (void)usb_host_client_handle_events(s_ftdi.client_hdl, pdMS_TO_TICKS(50));
        servo_try_attach_ftdi(&s_ftdi);
        vTaskDelay(pdMS_TO_TICKS(150));
    }
    last_attach_scan_tick = xTaskGetTickCount();
    if (!s_ftdi.device_ready) {
        ESP_LOGW(TAG, "U2D2 not found yet — keep scanning (hub+camera on HOST?)");
    }

    while (1) {
        if (s_ftdi.actions & DEVICE_ACTION_SCAN) {
            s_ftdi.actions &= ~DEVICE_ACTION_SCAN;
            if (!s_ftdi.device_ready) {
                servo_try_attach_ftdi(&s_ftdi);
                last_attach_scan_tick = xTaskGetTickCount();
            }
        }

        if (s_ftdi.actions & DEVICE_ACTION_OPEN) {
            s_ftdi.actions &= ~DEVICE_ACTION_OPEN;
            /* Never open the NEW_DEV address blindly — it is usually the hub or camera. */
            if (!s_ftdi.device_ready) {
                servo_try_attach_ftdi(&s_ftdi);
                last_attach_scan_tick = xTaskGetTickCount();
            }
        }

        if (s_ftdi.actions & DEVICE_ACTION_CLOSE) {
            s_ftdi.actions &= ~DEVICE_ACTION_CLOSE;
            if (s_ftdi.dev_hdl != NULL) {
                ftdi_close_device(&s_ftdi);
                s_torque_enabled = false;
                for (size_t i = 0; i < DXL_SERVO_COUNT; i++) {
                    s_servo_torque_on[i] = false;
                    s_torque_update_pending[i] = false;
                    s_servo_online[i] = false;
                    s_servo_move_ok[i] = false;
                }
                s_ftdi.dev_addr = 0;
            }
            s_ping_fail_streak = 0;
            s_boot_nod_started = false;
            last_attach_scan_tick = 0;
        }

        if (!s_ftdi.device_ready) {
            const TickType_t now = xTaskGetTickCount();
            if ((now - last_attach_scan_tick) >= pdMS_TO_TICKS(DXL_ATTACH_RETRY_MS)) {
                servo_try_attach_ftdi(&s_ftdi);
                last_attach_scan_tick = now;
            }
        }

        if (s_ftdi.device_ready && !s_ftdi.device_gone) {
            TickType_t now = xTaskGetTickCount();
            bool has_goal_update_pending = false;
            bool has_torque_update_pending = false;
            for (size_t i = 0; i < DXL_SERVO_COUNT; i++) {
                if (s_goal_update_pending[i]) {
                    has_goal_update_pending = true;
                }
                if (s_torque_update_pending[i]) {
                    has_torque_update_pending = true;
                }
            }
            bool has_sync_read_pending = false;
            if (s_sync_mutex != NULL && xSemaphoreTake(s_sync_mutex, 0) == pdTRUE) {
                has_sync_read_pending = (s_sync.state == DXL_SYNC_READ_PENDING);
                xSemaphoreGive(s_sync_mutex);
            }
            const TickType_t poll_ms =
                (has_goal_update_pending || has_sync_read_pending || has_torque_update_pending)
                    ? pdMS_TO_TICKS(DXL_FAST_POLL_INTERVAL_MS)
                    : pdMS_TO_TICKS(DXL_APP_POLL_INTERVAL_MS);
            if ((now - last_poll_tick) >= poll_ms) {
                esp_err_t err = ESP_OK;

                if (s_sync_mutex != NULL && xSemaphoreTake(s_sync_mutex, 0) == pdTRUE) {
                    if (s_sync.state == DXL_SYNC_READ_PENDING) {
                        err = dynamixel_send_read(&s_ftdi, s_sync.id, s_sync.addr, s_sync.length);
                        if (err == ESP_OK) {
                            s_sync.state = DXL_SYNC_READ_WAIT_RSP;
                        } else {
                            s_sync.result = err;
                            s_sync.state = DXL_SYNC_IDLE;
                            xSemaphoreGive(s_read_done_sem);
                        }
                    }
                    xSemaphoreGive(s_sync_mutex);
                }

                if (!s_torque_enabled) {
                    bool sync_busy = false;
                    if (s_sync_mutex != NULL && xSemaphoreTake(s_sync_mutex, 0) == pdTRUE) {
                        sync_busy = (s_sync.state != DXL_SYNC_IDLE &&
                                     s_sync.state != DXL_SYNC_READ_PENDING);
                        xSemaphoreGive(s_sync_mutex);
                    }
                    if (!sync_busy && s_usb_bus_settle) {
                        s_usb_bus_settle = false;
                        vTaskDelay(pdMS_TO_TICKS(DXL_BOOT_BUS_SETTLE_MS));
                    }
                    bool ping_ok[DXL_SERVO_COUNT] = {false, false};
                    if (!sync_busy) {
                        for (size_t i = 0; i < DXL_SERVO_COUNT; i++) {
                            const uint8_t id = s_servo_ids[i];
                            const esp_err_t ping_err =
                                dynamixel_ping_blocking(id, pdMS_TO_TICKS(400));
                            ping_ok[i] = (ping_err == ESP_OK);
                            s_servo_online[i] = ping_ok[i];
                            if (ping_ok[i]) {
                                ESP_LOGI(TAG, "PING ID%u (%s) OK", id, servo_axis_name(id));
                            } else {
                                ESP_LOGW(TAG, "PING ID%u (%s) failed: %s", id,
                                         servo_axis_name(id), esp_err_to_name(ping_err));
                            }
                            if (i + 1 < DXL_SERVO_COUNT) {
                                vTaskDelay(pdMS_TO_TICKS(30));
                            }
                        }
                    }
                    const bool any_online = ping_ok[0] || ping_ok[1];
                    if (!sync_busy && !any_online) {
                        s_ping_fail_streak++;
                        if (s_ping_fail_streak == 1 || (s_ping_fail_streak % 4) == 0) {
                            ESP_LOGW(TAG,
                                     "No Dynamixel reply to PING ID%u/%u (%d) — check servo "
                                     "power, TTL wiring, IDs 1&2, 1 Mbps baud, U2D2 on J18",
                                     DXL_PRIMARY_ID, DXL_SECONDARY_ID, s_ping_fail_streak);
                        }
                    } else if (!sync_busy && any_online) {
                        s_ping_fail_streak = 0;
                        if (!ping_ok[0] || !ping_ok[1]) {
                            ESP_LOGW(TAG,
                                     "Bring-up: ID1 tilt %s, ID2 pan %s — continuing with "
                                     "servos that answered",
                                     ping_ok[0] ? "OK" : "MISSING",
                                     ping_ok[1] ? "OK" : "MISSING");
                        } else {
                            ESP_LOGI(TAG, "Dynamixel ID1 (tilt) and ID2 (pan) both answered PING");
                        }
                        err = dynamixel_set_joint_mode(&s_ftdi);
                        if (err == ESP_OK) {
                            s_position_speed_pending = true;
                            dynamixel_queue_goal_all(DXL_CENTER_POSITION);
                            nino_servo_dxl_boot_nod();
                        }
                    }
                } else if (has_torque_update_pending) {
                    for (size_t i = 0; i < DXL_SERVO_COUNT; i++) {
                        if (!s_torque_update_pending[i]) {
                            continue;
                        }
                        const bool want_on = s_requested_torque_on[i];
                        s_torque_update_pending[i] = false;
                        esp_err_t t_err = dynamixel_send_write8(
                            &s_ftdi, s_servo_ids[i], DXL_TORQUE_ENABLE_ADDR,
                            want_on ? DXL_TORQUE_ON : DXL_TORQUE_OFF);
                        if (t_err == ESP_OK) {
                            s_servo_torque_on[i] = want_on;
                            ESP_LOGI(TAG, "Torque ID%u %s", s_servo_ids[i],
                                     want_on ? "ON" : "OFF");
                        } else {
                            ESP_LOGW(TAG, "Torque ID%u write failed: %s", s_servo_ids[i],
                                     esp_err_to_name(t_err));
                            if (err == ESP_OK) {
                                err = t_err;
                            }
                        }
                    }
                } else if (s_position_speed_pending) {
                    int speed = s_requested_position_speed;
                    s_position_speed_pending = false;
                    err = dynamixel_send_write16_all(&s_ftdi, DXL_MOVING_SPEED_ADDR, (uint16_t)speed);
                    if (err != ESP_OK) {
                        ESP_LOGW(TAG, "Position speed write failed: %s", esp_err_to_name(err));
                    } else {
                        s_active_position_speed = speed;
                        ESP_LOGI(TAG, "Joint-mode moving speed set to %d", speed);
                    }
                } else if (has_goal_update_pending) {
                    if (s_active_position_speed != s_requested_position_speed) {
                        err = dynamixel_send_write16_all(&s_ftdi, DXL_MOVING_SPEED_ADDR,
                                                         (uint16_t)s_requested_position_speed);
                        if (err == ESP_OK) {
                            s_active_position_speed = s_requested_position_speed;
                        }
                    }
                    for (size_t i = 0; i < DXL_SERVO_COUNT; i++) {
                        if (!s_goal_update_pending[i]) {
                            continue;
                        }
                        int goal = s_requested_goal[i];
                        s_goal_update_pending[i] = false;
                        if (!s_servo_torque_on[i]) {
                            continue;
                        }
                        esp_err_t goal_err =
                            dynamixel_send_write16(&s_ftdi, s_servo_ids[i], DXL_GOAL_POSITION_ADDR, (uint16_t)goal);
                        if (goal_err != ESP_OK && err == ESP_OK) {
                            err = goal_err;
                        }
                    }
                    if (err != ESP_OK) {
                        ESP_LOGW(TAG, "GOAL write failed: %s", esp_err_to_name(err));
                    }
                }

                last_poll_tick = now;
            }
        }

        esp_err_t err = usb_host_client_handle_events(s_ftdi.client_hdl, pdMS_TO_TICKS(50));
        if (err != ESP_OK && err != ESP_ERR_TIMEOUT) {
            ESP_LOGW(TAG, "usb_host_client_handle_events: %s", esp_err_to_name(err));
        }
    }
}

static bool boot_health_move_axis(uint8_t id, int target, const char *label)
{
    int before = 0;
    if (nino_servo_dxl_get_present_position(id, &before) != ESP_OK) {
        ESP_LOGW(TAG, "Boot health ID%u (%s) FAIL — no present-position reply", id, label);
        return false;
    }
    ESP_LOGI(TAG, "Boot health ID%u (%s) present=%d — moving to %d", id, label, before, target);
    nino_servo_dxl_set_servo_goal(id, target);
    if (!dynamixel_wait_servo_at(id, target, pdMS_TO_TICKS(DXL_BOOT_MOVE_TIMEOUT_MS))) {
        int now_pos = before;
        (void)nino_servo_dxl_get_present_position(id, &now_pos);
        ESP_LOGW(TAG, "Boot health ID%u (%s) FAIL — did not reach %d (now %d)", id, label, target,
                 now_pos);
        return false;
    }
    ESP_LOGI(TAG, "Boot health ID%u (%s) OK — reached %d", id, label, target);
    vTaskDelay(pdMS_TO_TICKS(200));
    return true;
}

static void boot_nod_task(void *arg)
{
    (void)arg;
    for (int i = 0; i < 80 && !nino_servo_dxl_is_ready(); i++) {
        vTaskDelay(pdMS_TO_TICKS(40));
    }
    if (!nino_servo_dxl_is_ready()) {
        ESP_LOGW(TAG, "Boot health skipped — joint mode never came up (U2D2/J18/IDs/baud?)");
        vTaskDelete(NULL);
        return;
    }

    ESP_LOGI(TAG, "Boot health: ID1 tilt online=%d, ID2 pan online=%d",
             s_servo_online[0] ? 1 : 0, s_servo_online[1] ? 1 : 0);

    nino_servo_dxl_set_position_speed(DXL_BOOT_HEALTH_SPEED);
    vTaskDelay(pdMS_TO_TICKS(120));

    if (s_servo_online[0]) {
        const bool up = boot_health_move_axis(DXL_PRIMARY_ID, NINO_SERVO_TILT_UP, "tilt");
        const bool down = boot_health_move_axis(DXL_PRIMARY_ID, NINO_SERVO_TILT_DOWN, "tilt");
        s_servo_move_ok[0] = up && down;
        (void)boot_health_move_axis(DXL_PRIMARY_ID, DXL_CENTER_POSITION, "tilt");
    } else {
        ESP_LOGW(TAG, "Boot health: skipping tilt (ID1 did not PING)");
    }

    if (s_servo_online[1]) {
        const bool left = boot_health_move_axis(DXL_SECONDARY_ID, NINO_SERVO_PAN_LEFT, "pan");
        const bool right = boot_health_move_axis(DXL_SECONDARY_ID, NINO_SERVO_PAN_RIGHT, "pan");
        s_servo_move_ok[1] = left && right;
        (void)boot_health_move_axis(DXL_SECONDARY_ID, DXL_CENTER_POSITION, "pan");
    } else {
        ESP_LOGW(TAG, "Boot health: skipping pan (ID2 did not PING)");
    }

    nino_servo_dxl_set_position_speed(DXL_DEFAULT_POSITION_SPEED);
    nino_servo_dxl_go_neutral();

    ESP_LOGI(TAG, "Boot health summary: tilt=%s pan=%s",
             s_servo_move_ok[0] ? "PASS" : (s_servo_online[0] ? "NO-MOVE" : "NO-PING"),
             s_servo_move_ok[1] ? "PASS" : (s_servo_online[1] ? "NO-MOVE" : "NO-PING"));
    vTaskDelete(NULL);
}

void nino_servo_dxl_boot_nod(void)
{
    if (s_boot_nod_started) {
        return;
    }
    s_boot_nod_started = true;
    if (xTaskCreate(boot_nod_task, "dxl_boot_nod", 4096, NULL, 4, NULL) != pdPASS) {
        s_boot_nod_started = false;
        ESP_LOGW(TAG, "Boot health task not started");
    }
}

esp_err_t nino_servo_dxl_start(void)
{
    if (s_servo_started) {
        return ESP_OK;
    }
    if (s_goal_mutex == NULL) {
        s_goal_mutex = xSemaphoreCreateMutex();
        if (s_goal_mutex == NULL) {
            return ESP_ERR_NO_MEM;
        }
    }
    if (s_sync_mutex == NULL) {
        s_sync_mutex = xSemaphoreCreateMutex();
        if (s_sync_mutex == NULL) {
            return ESP_ERR_NO_MEM;
        }
    }
    if (s_read_done_sem == NULL) {
        s_read_done_sem = xSemaphoreCreateBinary();
        if (s_read_done_sem == NULL) {
            return ESP_ERR_NO_MEM;
        }
    }

    BaseType_t ok = xTaskCreate(usb_client_task, "servo_usb", USB_CLIENT_TASK_STACK_SIZE, NULL,
                                USB_TASK_PRIORITY, NULL);
    if (ok != pdPASS) {
        return ESP_ERR_NO_MEM;
    }

    s_servo_started = true;
    ESP_LOGI(TAG, "Dynamixel servo task started (IDs %u,%u joint mode, neutral=%d)",
             DXL_PRIMARY_ID, DXL_SECONDARY_ID, DXL_CENTER_POSITION);
    return ESP_OK;
}

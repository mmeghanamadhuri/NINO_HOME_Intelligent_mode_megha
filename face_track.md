# Main.cpp

// ESP32-P4 USB face-tracking system.
//
// Bring up the USB host (camera + U2D2), move both Dynamixel AX-18 servos to
// neutral (512) on boot, stream from the camera, run on-device face detection
// (ESP-DL HumanFaceDetect) on each frame, and drive a pan/tilt centering loop.
// Tracking is toggled from the UART console with "track on" / "track off".
//
// Bring-up ORDER matters when the camera and U2D2 share one (often marginal,
// full-speed) USB hub: the camera's heavy UVC enumeration + isochronous stream
// can saturate the hub's control endpoint and make it STALL while it is still
// trying to enable the U2D2's downstream port. So we bring up the U2D2 FIRST,
// wait for it to open, and only THEN start the camera. If the U2D2 never shows
// up we start the camera anyway so the video pipeline is never blocked.

#include "app_config.h"

extern "C" {
#include "usb_host.h"
#include "u2d2_serial.h"
#include "dynamixel.h"
#include "uvc_cam.h"
}

#include "face_detect.hpp"
#include "tracker.h"
#include "track_console.h"

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "nvs_flash.h"
#include "esp_log.h"

static const char *TAG = "app";

// Drive both servos to the neutral (center) position.
static void servos_go_neutral(void)
{
    dxl_set_torque(DXL_ID_PAN, true);
    dxl_set_torque(DXL_ID_TILT, true);
    dxl_set_moving_speed(DXL_ID_PAN, DXL_MOVING_SPEED);
    dxl_set_moving_speed(DXL_ID_TILT, DXL_MOVING_SPEED);
    dxl_set_goal_position(DXL_ID_PAN, DXL_NEUTRAL);
    dxl_set_goal_position(DXL_ID_TILT, DXL_NEUTRAL);
    ESP_LOGI(TAG, "servos -> neutral (%d)", DXL_NEUTRAL);
}

// Background task: open the U2D2, verify the servos, move to neutral.
// Retries quietly so it does not spam the log or block other subsystems.
static void u2d2_task(void *arg)
{
    ESP_ERROR_CHECK(u2d2_install());
    ESP_LOGI(TAG, "U2D2 bring-up: looking for CDC-ACM device %04X:%04X...",
             U2D2_CDC_VID, U2D2_CDC_PID);

    int attempt = 0;
    while (!u2d2_is_open()) {
        esp_err_t err = u2d2_open(DXL_BAUD, 1000);
        if (err != ESP_OK) {
            // Log only occasionally to avoid flooding the console.
            if (attempt++ % 10 == 0) {
                ESP_LOGW(TAG, "U2D2 not found yet (%s). Check: data cable, USB host (Type-A) "
                              "port, and hub power. Retrying...",
                         esp_err_to_name(err));
            }
            vTaskDelay(pdMS_TO_TICKS(2000));
        }
    }
    ESP_LOGI(TAG, "U2D2 connected.");

    const uint8_t ids[2] = { DXL_ID_PAN, DXL_ID_TILT };
    for (int i = 0; i < 2; i++) {
        if (dxl_ping(ids[i]) == ESP_OK) {
            ESP_LOGI(TAG, "AX servo id %d is responding", ids[i]);
        } else {
            ESP_LOGW(TAG, "AX servo id %d did NOT respond "
                          "(check 12V servo power, wiring, ID, and baud=%d)",
                     ids[i], DXL_BAUD);
        }
    }

    servos_go_neutral();
    ESP_LOGI(TAG, "U2D2 bring-up complete.");
    vTaskDelete(NULL);
}

// Background task: install the UVC driver, wait for the camera, start streaming.
static void camera_task(void *arg)
{
    ESP_ERROR_CHECK(uvc_cam_install());
    ESP_LOGI(TAG, "camera bring-up: waiting for a USB camera...");

    while (true) {
        esp_err_t err = uvc_cam_start(CAM_WIDTH, CAM_HEIGHT, CAM_FPS, 10000);
        if (err == ESP_OK) {
            ESP_LOGI(TAG, "camera streaming.");
            break;
        }
        ESP_LOGW(TAG, "camera start failed (%s), retrying...", esp_err_to_name(err));
        vTaskDelay(pdMS_TO_TICKS(2000));
    }
    vTaskDelete(NULL);
}

// Tracking pipeline: pull each available frame, run face detection on it, and
// feed the result to the tracker (which only moves the servos when 'track on').
static void tracking_task(void *arg)
{
    ESP_LOGI(TAG, "tracking pipeline started (%dx%d). Type 'track on' to follow a face.",
             CAM_WIDTH, CAM_HEIGHT);

    uint32_t processed = 0;
    while (true) {
        uvc_host_frame_t *frame = uvc_cam_acquire(1000);
        if (!frame) {
            continue;   // no frame this cycle
        }

        face_t face;
        esp_err_t err = face_detect_process(frame->data, frame->data_len, &face);
        uvc_cam_release(frame);
        if (err != ESP_OK) {
            continue;
        }

        // Center against the ACTUAL decoded frame size (face.frame_w/h), not the
        // requested camera resolution - the decoder may return a different size.
        tracker_update(face.found, face.cx, face.cy, face.frame_w, face.frame_h);

        // Periodic status so the console is not flooded.
        if ((processed++ % 10) == 0) {
            int pan = 0, tilt = 0;
            tracker_get_state(&pan, &tilt);
            if (face.found) {
                ESP_LOGI(TAG, "face (%d,%d)/%dx%d err(%+d,%+d) -> servo pan=%d tilt=%d score=%.2f [%s]",
                         face.cx, face.cy, face.frame_w, face.frame_h,
                         face.cx - face.frame_w / 2, face.cy - face.frame_h / 2,
                         pan, tilt, face.score, tracker_enabled() ? "ON" : "OFF");
            } else {
                ESP_LOGI(TAG, "no face -> servo pan=%d tilt=%d [%s]",
                         pan, tilt, tracker_enabled() ? "ON" : "OFF");
            }
        }
    }
}

extern "C" void app_main(void)
{
    ESP_ERROR_CHECK(nvs_flash_init());

    ESP_LOGI(TAG, "==============================================");
    ESP_LOGI(TAG, " ESP32-P4 face tracker");
    ESP_LOGI(TAG, "==============================================");

    ESP_ERROR_CHECK(app_usb_host_init());

    // Bring up the U2D2 FIRST and let it finish enumerating on the shared hub
    // before the camera floods the bus. This keeps the serial adapter and the
    // camera enumeration separate (mirrors the known-good board bring-up).
    xTaskCreatePinnedToCore(u2d2_task, "u2d2_init", 4096, NULL, 4, NULL, 0);

    ESP_LOGI(TAG, "Waiting up to %d ms for the U2D2 before starting the camera...",
             U2D2_BRINGUP_WAIT_MS);
    int waited = 0;
    while (!u2d2_is_open() && waited < U2D2_BRINGUP_WAIT_MS) {
        vTaskDelay(pdMS_TO_TICKS(100));
        waited += 100;
    }
    if (u2d2_is_open()) {
        ESP_LOGI(TAG, "U2D2 ready; starting the camera.");
    } else {
        ESP_LOGW(TAG, "U2D2 not ready in %d ms - starting the camera anyway "
                      "(U2D2 task keeps retrying in the background).",
                 U2D2_BRINGUP_WAIT_MS);
    }

    // Phase B: tracker (servos), face detector (ESP-DL), and the console.
    tracker_init();
    esp_err_t fd = face_detect_init();
    if (fd != ESP_OK) {
        ESP_LOGE(TAG, "face detector failed to load (%s); tracking will be inactive",
                 esp_err_to_name(fd));
    }
    track_console_start();

    // Start the camera, then the detect->track pipeline that consumes its frames.
    xTaskCreatePinnedToCore(camera_task, "cam_init", 4096, NULL, 4, NULL, 0);
    if (fd == ESP_OK) {
        // Face detection needs a large stack (model + JPEG decode buffers).
        xTaskCreatePinnedToCore(tracking_task, "tracking", 12 * 1024, NULL, 5, NULL, 1);
    }

    ESP_LOGI(TAG, "Phase B ready: tracking DISABLED (use 'track on' / 'track off').");

    // Heartbeat: report camera throughput so we can confirm the video pipeline.
    uint32_t last = 0;
    while (true) {
        vTaskDelay(pdMS_TO_TICKS(2000));
        uint32_t now = uvc_cam_frame_count();
        if (uvc_cam_streaming() || now != last) {
            ESP_LOGI(TAG, "camera frames: %u total (%.1f fps)",
                     (unsigned)now, (now - last) / 2.0f);
        }
        last = now;
    }
}


# app_config.h

#pragma once

// ---------------------------------------------------------------------------
// Dynamixel AX-18 (Protocol 1.0) configuration
// ---------------------------------------------------------------------------
#define DXL_ID_PAN        2        // servo ID 2 = pan  (left/right)
#define DXL_ID_TILT       1        // servo ID 1 = tilt (up/down)
#define DXL_NEUTRAL       512      // center position (0..1023, ~150 deg)
#define DXL_POS_MIN       0        // full positional range lower bound
#define DXL_POS_MAX       1023     // full positional range upper bound
#define DXL_BAUD          1000000  // 10 lakh bps (AX-18 factory default)
#define DXL_MOVING_SPEED  60      // 0..1023 moving speed (0 = max). Higher = faster response
                                   // for both face tracking and the boot move to 512.

// ---------------------------------------------------------------------------
// U2D2 USB adapter
// This unit enumerates as a USB CDC-ACM device (VID 0x16D0, PID 0x06A7), so it
// is opened with the CDC-ACM host driver and the baud is set via the standard
// CDC SET_LINE_CODING (dwDTERate = baud directly - no FTDI divisor).
// ---------------------------------------------------------------------------
#define U2D2_CDC_VID      0x16D0   // CDC-ACM U2D2 vendor ID
#define U2D2_CDC_PID      0x06A7   // CDC-ACM U2D2 product ID

// How long app_main waits for the U2D2 to open before starting the camera, so
// the serial adapter can enumerate on the shared hub without competing with the
// camera's UVC enumeration. Camera starts anyway after this timeout.
#define U2D2_BRINGUP_WAIT_MS  10000

// ---------------------------------------------------------------------------
// USB camera (UVC)
// 320x240 keeps every MJPEG frame within the full-speed isochronous budget of
// the shared USB 1.1 hub, so the stream holds a steady 15 fps (no "missed EoF"
// drops). For 640x480 @ 15 fps the camera needs a high-speed path - a powered
// USB 2.0 hub.
// ---------------------------------------------------------------------------
#define CAM_WIDTH         640
#define CAM_HEIGHT        480
#define CAM_FPS           15.0f

// ---------------------------------------------------------------------------
// Tracking control (Phase B)
// Closed-loop centering: the face's offset from the frame center is the error;
// the pan/tilt servos CHASE the face (move the same way the face moved) so the
// face is driven back to the center of the frame. The move size is proportional
// to the offset (Kp * error), so a small face shift -> small servo move and a
// large shift -> large move, capped per update by TRACK_MAX_STEP.
// ---------------------------------------------------------------------------
// Ported from the reference Python tracker:
//   error    = frame_center - face_center        (pixels)
//   delta    = clamp(error * Kp, -MAX_STEP, MAX_STEP)
//   position = position + SIGN * delta
// plus an exponential moving-average (EMA) smoothing of the face center so the
// servo reacts to a steady target instead of per-frame detection jitter.
#define TRACK_DEADZONE_PX     12      // ignore face within this many px of center
#define TRACK_KP_PAN          0.10f   // pan  proportional gain
#define TRACK_KP_TILT         0.10f   // tilt proportional gain
#define TRACK_MAX_STEP        140     // max servo units per update
#define TRACK_FACE_SMOOTHING  0.55f   // EMA factor (0..1): higher = snappier, lower = smoother
#define TRACK_TILT_MIN_POS    DXL_NEUTRAL  // do not tilt downward past neutral
// Require a few consecutive "face found" frames before moving servos. This
// suppresses one-off false detections that look like random twitching.
#define TRACK_FACE_ACQUIRE_HITS  3
// If no face is seen for this many update frames while tracking is ON, return
// both servos to neutral (512) so they do not stay stuck at a rail.
#define TRACK_FACE_LOST_FRAMES   15
// Direction signs. Verified from hardware logs: with +1 (the Python's
// convention) pan ran to the MIN rail while the face was on the right, and tilt
// ran to the MAX rail while the face was above center - i.e. BOTH axes were
// inverted for this camera/mount (mirrored image). So both are -1.
#define TRACK_PAN_SIGN    (1)
#define TRACK_TILT_SIGN   (-1)


# face_detect.hpp

#pragma once

#include <stddef.h>
#include <stdint.h>
#include <stdbool.h>
#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

// Result of running the detector on one frame.
//
// IMPORTANT: cx/cy and the box are in the DECODED IMAGE coordinate space, whose
// size is reported in frame_w/frame_h. Always center against frame_w/frame_h
// (not the requested camera resolution) - the decoder may hand back a different
// size, and the detector rescales boxes to whatever it was given.
typedef struct {
    bool  found;            // true if at least one face was detected
    int   cx, cy;           // center of the best face, in decoded-image pixels
    int   x1, y1, x2, y2;   // best-face bounding box, in decoded-image pixels
    int   frame_w, frame_h; // decoded image size the coords are relative to
    float score;            // confidence of the best face
} face_t;

// Load the ESP-DL HumanFaceDetect model. Call once before face_detect_process().
esp_err_t face_detect_init(void);

// Decode one MJPEG frame (hardware JPEG codec on the P4) and run face
// detection. Returns ESP_OK if the frame decoded (check out->found for a face),
// or an error if decoding/detection could not run.
esp_err_t face_detect_process(const uint8_t *jpeg, size_t len, face_t *out);

#ifdef __cplusplus
}
#endif

# face_detect.cpp 

#include "face_detect.hpp"

#include <list>
#include "sdkconfig.h"
#include "esp_heap_caps.h"
#include "esp_log.h"

#include "dl_image_jpeg.hpp"
#include "human_face_detect.hpp"

static const char *TAG = "face";

static HumanFaceDetect *s_detector = nullptr;

esp_err_t face_detect_init(void)
{
    if (s_detector) {
        return ESP_OK;
    }
    s_detector = new HumanFaceDetect(HumanFaceDetect::ESPDET_PICO_224_224_FACE, false);
    if (!s_detector) {
        return ESP_ERR_NO_MEM;
    }
    ESP_LOGI(TAG, "ESP-DL HumanFaceDetect loaded (ESPDET_PICO_224_224_FACE)");
    return ESP_OK;
}

esp_err_t face_detect_process(const uint8_t *jpeg, size_t len, face_t *out)
{
    if (!out) {
        return ESP_ERR_INVALID_ARG;
    }
    *out = {};
    if (!s_detector || !jpeg || len == 0) {
        return ESP_ERR_INVALID_ARG;
    }

    dl::image::jpeg_img_t jpeg_img = {
        .data     = const_cast<uint8_t *>(jpeg),
        .data_len = len,
    };

#if CONFIG_SOC_JPEG_CODEC_SUPPORTED
    dl::image::img_t img = dl::image::hw_decode_jpeg(jpeg_img, dl::image::DL_IMAGE_PIX_TYPE_RGB888, 120);
#else
    dl::image::img_t img = dl::image::sw_decode_jpeg(jpeg_img, dl::image::DL_IMAGE_PIX_TYPE_RGB888);
#endif
    if (img.data == nullptr) {
        return ESP_FAIL;
    }

    // The detector rescales its boxes to this decoded-image size, so the caller
    // must center against these dimensions (NOT the requested camera size).
    out->frame_w = img.width;
    out->frame_h = img.height;

    std::list<dl::detect::result_t> &results = s_detector->run(img);

    float best = -1.0f;
    for (const auto &res : results) {
        if (res.box.size() < 4) {
            continue;
        }
        if (res.score > best) {
            best = res.score;
            out->found = true;
            out->score = res.score;
            out->x1 = res.box[0];
            out->y1 = res.box[1];
            out->x2 = res.box[2];
            out->y2 = res.box[3];
            out->cx = (res.box[0] + res.box[2]) / 2;
            out->cy = (res.box[1] + res.box[3]) / 2;
        }
    }

    heap_caps_free(img.data);
    return ESP_OK;
}


# track_console.h

#pragma once

#ifdef __cplusplus
extern "C" {
#endif

// Start an interactive UART console exposing the "track on|off|status" command.
void track_console_start(void);

#ifdef __cplusplus
}
#endif


# track_console.c

#include "track_console.h"
#include "tracker.h"

#include <stdio.h>
#include <string.h>
#include "esp_console.h"
#include "esp_err.h"

static int cmd_track(int argc, char **argv)
{
    if (argc < 2) {
        printf("usage: track on | off | status\n");
        return 0;
    }
    if (strcmp(argv[1], "on") == 0) {
        tracker_set_enabled(true);
    } else if (strcmp(argv[1], "off") == 0) {
        tracker_set_enabled(false);
    } else if (strcmp(argv[1], "status") == 0) {
        printf("tracking is %s\n", tracker_enabled() ? "ON" : "OFF");
    } else {
        printf("usage: track on | off | status\n");
    }
    return 0;
}

void track_console_start(void)
{
    esp_console_repl_t *repl = NULL;
    esp_console_repl_config_t repl_config = ESP_CONSOLE_REPL_CONFIG_DEFAULT();
    repl_config.prompt = "facetrack>";
    repl_config.max_cmdline_length = 64;

    esp_console_dev_uart_config_t uart_config = ESP_CONSOLE_DEV_UART_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_console_new_repl_uart(&uart_config, &repl_config, &repl));

    const esp_console_cmd_t cmd = {
        .command = "track",
        .help = "Enable/disable face tracking: track on | off | status",
        .hint = NULL,
        .func = &cmd_track,
        .argtable = NULL,
    };
    ESP_ERROR_CHECK(esp_console_cmd_register(&cmd));
    ESP_ERROR_CHECK(esp_console_register_help_command());

    ESP_ERROR_CHECK(esp_console_start_repl(repl));
}


# tracker.h

#pragma once

#include <stdbool.h>

#ifdef __cplusplus
extern "C" {
#endif

// Initialise the tracker: torque on, moving speed set, both servos to neutral,
// tracking disabled. Call after the U2D2/servos are up.
void tracker_init(void);

// Enable/disable tracking. Disabling returns both servos to neutral (512).
void tracker_set_enabled(bool enabled);
bool tracker_enabled(void);

// Feed one detection result. When tracking is enabled and a face was found, the
// pan/tilt servos are nudged to bring the face toward the frame center. When no
// face is found (or tracking is disabled) the servos hold their position.
void tracker_update(bool face_found, int face_cx, int face_cy, int frame_w, int frame_h);

// Read the last commanded pan/tilt servo positions (for diagnostics/logging).
void tracker_get_state(int *pan, int *tilt);

#ifdef __cplusplus
}
#endif

# tracker.c

#include "tracker.h"
#include "app_config.h"
#include "dynamixel.h"

#include <math.h>
#include "esp_log.h"

static const char *TAG = "tracker";

static bool  s_enabled = false;
static int   s_pan  = DXL_NEUTRAL;   // last commanded pan position
static int   s_tilt = DXL_NEUTRAL;   // last commanded tilt position
static float s_smooth_x = -1.0f;     // EMA-smoothed face center (-1 = uninitialised)
static float s_smooth_y = -1.0f;
static int   s_face_hits = 0;        // consecutive face-found frames
static int   s_face_misses = 0;      // consecutive no-face frames

static int clampi(int v, int lo, int hi)
{
    if (v < lo) return lo;
    if (v > hi) return hi;
    return v;
}

static void go_neutral(void)
{
    s_pan  = DXL_NEUTRAL;
    s_tilt = DXL_NEUTRAL;
    dxl_set_goal_position(DXL_ID_PAN,  DXL_NEUTRAL);
    dxl_set_goal_position(DXL_ID_TILT, DXL_NEUTRAL);
}

void tracker_init(void)
{
    dxl_set_torque(DXL_ID_PAN,  true);
    dxl_set_torque(DXL_ID_TILT, true);
    dxl_set_moving_speed(DXL_ID_PAN,  DXL_MOVING_SPEED);
    dxl_set_moving_speed(DXL_ID_TILT, DXL_MOVING_SPEED);
    go_neutral();
    s_enabled = false;
    ESP_LOGI(TAG, "tracker ready (disabled). Use 'track on' to start.");
}

void tracker_set_enabled(bool enabled)
{
    if (enabled == s_enabled) {
        return;
    }
    s_enabled = enabled;
    s_smooth_x = -1.0f;   // restart face smoothing on every toggle
    s_smooth_y = -1.0f;
    s_face_hits = 0;
    s_face_misses = 0;
    if (enabled) {
        dxl_set_torque(DXL_ID_PAN,  true);
        dxl_set_torque(DXL_ID_TILT, true);
        ESP_LOGI(TAG, "tracking ENABLED");
    } else {
        go_neutral();
        ESP_LOGI(TAG, "tracking DISABLED -> servos to neutral (%d)", DXL_NEUTRAL);
    }
}

bool tracker_enabled(void)
{
    return s_enabled;
}

void tracker_update(bool face_found, int face_cx, int face_cy, int frame_w, int frame_h)
{
    if (!s_enabled) {
        return;
    }

    if (!face_found || frame_w <= 0 || frame_h <= 0) {
        s_face_hits = 0;
        s_face_misses++;
        if (s_face_misses >= TRACK_FACE_LOST_FRAMES) {
            // Face lost for a while: clear filter state so the next lock starts
            // cleanly, but keep the servos at their current position.
            s_smooth_x = -1.0f;
            s_smooth_y = -1.0f;
        }
        return;
    }

    s_face_misses = 0;
    if (s_face_hits < TRACK_FACE_ACQUIRE_HITS) {
        s_face_hits++;
        // Prime EMA but do not move yet until lock is stable.
        s_smooth_x = (float)face_cx;
        s_smooth_y = (float)face_cy;
        return;
    }

    // EMA-smooth the face center (filters per-frame detection jitter).
    if (s_smooth_x < 0.0f) {
        s_smooth_x = (float)face_cx;
        s_smooth_y = (float)face_cy;
    } else {
        s_smooth_x += ((float)face_cx - s_smooth_x) * TRACK_FACE_SMOOTHING;
        s_smooth_y += ((float)face_cy - s_smooth_y) * TRACK_FACE_SMOOTHING;
    }

    // error = frame_center - smoothed_face_center  (Python convention).
    float err_x = (frame_w / 2.0f) - s_smooth_x;
    float err_y = (frame_h / 2.0f) - s_smooth_y;

    if (fabsf(err_x) > TRACK_DEADZONE_PX) {
        int delta = (int)lroundf(err_x * TRACK_KP_PAN);
        if (delta == 0) {                       // never stall while off-center:
            delta = (err_x > 0) ? 1 : -1;       // guarantee at least a 1-unit nudge
        }
        delta = clampi(delta, -TRACK_MAX_STEP, TRACK_MAX_STEP);
        int new_pan = clampi(s_pan + TRACK_PAN_SIGN * delta, DXL_POS_MIN, DXL_POS_MAX);
        if (new_pan != s_pan) {
            s_pan = new_pan;
            dxl_set_goal_position(DXL_ID_PAN, (uint16_t)new_pan);
        }
    }

    // Tilt tracking disabled for now; keep the tilt axis fixed while tuning pan.
    (void)err_y;
}

void tracker_get_state(int *pan, int *tilt)
{
    if (pan)  *pan  = s_pan;
    if (tilt) *tilt = s_tilt;
}


# idf_component.yml

## IDF Component Manager Manifest File
## USB camera (UVC) + U2D2 (CDC-ACM @ 0x16D0:0x06A7) + on-device face detection.
dependencies:
  idf: ">=5.5"
  espressif/usb_host_uvc: "^2.5.1"
  espressif/usb_host_cdc_acm: "^2.0.0"
  espressif/esp-dl: "^3.3.4"
  espressif/human_face_detect: "^0.4.1"

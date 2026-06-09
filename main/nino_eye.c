#include "nino_eye.h"

#include <ctype.h>
#include <stdint.h>
#include <string.h>
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "ssd1351.h"

static const char *TAG = "nino_eye";

/* One SSD1351 OLED per eye, native landscape 128 x 96. */
#define LOGICAL_WIDTH   OLED_WIDTH
#define LOGICAL_HEIGHT  OLED_HEIGHT
#define EYE_CX          (LOGICAL_WIDTH / 2)
/* Global downward shift for all states. */
#define NINO_VOFFSET    8
#define EYE_CY          (LOGICAL_HEIGHT / 2 + NINO_VOFFSET)

/* Only this central region may be drawn/erased (eye + blink). The rest of the
 * screen is never touched after boot — only the oval changes. */
#define EYE_CLIP_HALF_W   46
#define EYE_CLIP_Y0       (EYE_CY - 52)
#define EYE_CLIP_Y1       (EYE_CY + 44)

/* Quick eyelid blink: per-frame delay for the close/open sweep. */
#define BLINK_FRAME_MS      24
#define BLINK_CLOSE_STEP    6
#define MAX_GAZE_POINTS 5

typedef struct {
    int rx;
    int ry;
    int hold_ms;
    int closed_hold_ms;
    int blink_step;
    int blink_ms;
    int gaze_offsets[MAX_GAZE_POINTS];
    int gaze_count;
    uint8_t eye_r;       /* emotion eye colour on white background */
    uint8_t eye_g;
    uint8_t eye_b;
} nino_state_profile_t;

static volatile nino_eye_state_t s_state = NINO_EYE_IDLE;

static const nino_state_profile_t s_profiles[NINO_EYE_STATE_COUNT] = {
    /* idle: neutral / half-open, slow blink. */
    [NINO_EYE_IDLE] = {
        .rx = 24,
        .ry = 30,
        .hold_ms = 10000,
        .closed_hold_ms = 240,
        .blink_step = 3,
        .blink_ms = 45,
        .gaze_offsets = {0},
        .gaze_count = 1,
        .eye_r = 0, .eye_g = 0, .eye_b = 0,
    },
    /* listening: wide enlarged eye, centered - blinks in place. ~3 s cycle. */
    [NINO_EYE_LISTENING] = {
        .rx = 30,
        .ry = 36,
        .hold_ms = 6000,
        .closed_hold_ms = 120,
        .blink_step = 4,
        .blink_ms = 20,
        .gaze_offsets = {0},
        .gaze_count = 1,
        .eye_r = 0, .eye_g = 0, .eye_b = 0,
    },
    /* thinking: a normal solid eye like idle that slowly rolls around the top
     * (looking up + side to side). No blink. */
    [NINO_EYE_THINKING] = {
        .rx = 24,
        .ry = 30,
        .eye_r = 0, .eye_g = 0, .eye_b = 0,
    },
};

/* Background is white; eyes are black (per-state colour). */
static uint16_t s_eye_color = 0x0000;

static uint16_t color_bg(void)
{
    return ssd1351_color(255, 255, 255);
}

static uint16_t color_eye(void)
{
    return s_eye_color;
}

static void set_eye_color(const nino_state_profile_t *profile)
{
    s_eye_color = ssd1351_color(profile->eye_r, profile->eye_g, profile->eye_b);
}

static int ellipse_half_width(int rx, int ry, int dy)
{
    int dy2 = dy * dy;
    int ry2 = ry * ry;
    if (dy2 > ry2) {
        return -1;
    }

    int64_t target = (int64_t)rx * rx * (ry2 - dy2);
    int dx = 0;
    while ((int64_t)(dx + 1) * (dx + 1) * ry2 <= target) {
        dx++;
    }
    return dx;
}

static void draw_landscape_hline(int x, int y, int width, uint16_t color)
{
    if (y < EYE_CLIP_Y0 || y > EYE_CLIP_Y1 || width <= 0) {
        return;
    }

    const int clip_x0 = EYE_CX - EYE_CLIP_HALF_W;
    const int clip_x1 = EYE_CX + EYE_CLIP_HALF_W;
    if (x + width <= clip_x0 || x > clip_x1) {
        return;
    }
    if (x < clip_x0) {
        width -= (clip_x0 - x);
        x = clip_x0;
    }
    if (x + width - 1 > clip_x1) {
        width = clip_x1 - x + 1;
    }

    if (y < 0 || y >= LOGICAL_HEIGHT) {
        return;
    }
    if (x < 0) {
        width += x;
        x = 0;
    }
    if (x + width > LOGICAL_WIDTH) {
        width = LOGICAL_WIDTH - x;
    }
    if (width <= 0) {
        return;
    }

    ssd1351_fill_rect(x, y, width, 1, color);
}

/*
 * Remember the EXACT shape currently painted, so the next state can un-draw it
 * along its own outline (writing background over only the pixels that were the
 * eye) instead of erasing a white rectangle. Re-writing background pixels that
 * were already white reads slightly different on this OLED -> visible "window".
 */
typedef enum { PREV_NONE, PREV_ELLIPSE, PREV_BLOB } prev_kind_t;
static prev_kind_t s_prev_kind = PREV_NONE;
static int s_prev_cx, s_prev_rx, s_prev_ry, s_prev_top, s_prev_bottom;
static int s_prev_blob_cy;

static void remember_ellipse(int cx, int rx, int ry, int top, int bottom)
{
    s_prev_kind = PREV_ELLIPSE;
    s_prev_cx = cx;
    s_prev_rx = rx;
    s_prev_ry = ry;
    s_prev_top = top;
    s_prev_bottom = bottom;
}

/* Blob = a solid eye drawn at an arbitrary center (cx, cy), e.g. the thinking
 * eye that is shifted/rolled around. Erasing fills that ellipse with bg. */
static void remember_blob(int cx, int cy, int rx, int ry)
{
    s_prev_kind = PREV_BLOB;
    s_prev_cx = cx;
    s_prev_blob_cy = cy;
    s_prev_rx = rx;
    s_prev_ry = ry;
}

static void draw_eye_rows(int center_x, int rx, int ry, int top, int bottom, uint16_t fill)
{
    if (top < 0) {
        top = 0;
    }
    if (bottom >= LOGICAL_HEIGHT) {
        bottom = LOGICAL_HEIGHT - 1;
    }
    if (top > bottom) {
        return;
    }

    for (int y = top; y <= bottom; y++) {
        int dx = ellipse_half_width(rx, ry, y - EYE_CY);
        if (dx < 0) {
            continue;
        }
        draw_landscape_hline(center_x - dx, y, (dx * 2) + 1, fill);
    }
}

/* Erase EXACTLY the eye footprint: only flips black eye pixels back to white,
 * never re-touching the surrounding background (no "window" flash). */
static void erase_eye_rows(int center_x, int rx, int ry, int top, int bottom)
{
    draw_eye_rows(center_x, rx, ry, top, bottom, color_bg());
}

static void draw_full_eye(int center_x, int rx, int ry)
{
    draw_eye_rows(center_x, rx, ry, EYE_CY - ry, EYE_CY + ry, color_eye());
    remember_ellipse(center_x, rx, ry, EYE_CY - ry, EYE_CY + ry);
}

/* Filled ellipse centered at an arbitrary (cx, cy). */
static void fill_ellipse(int cx, int cy, int rx, int ry, uint16_t color)
{
    for (int dy = -ry; dy <= ry; dy++) {
        int dx = ellipse_half_width(rx, ry, dy);
        if (dx < 0) {
            continue;
        }
        draw_landscape_hline(cx - dx, cy + dy, (dx * 2) + 1, color);
    }
}

/* Un-draw the previously painted shape using the SAME geometry. */
static void erase_prev_eye(void)
{
    if (s_prev_kind == PREV_ELLIPSE) {
        draw_eye_rows(s_prev_cx, s_prev_rx, s_prev_ry, s_prev_top, s_prev_bottom, color_bg());
    } else if (s_prev_kind == PREV_BLOB) {
        fill_ellipse(s_prev_cx, s_prev_blob_cy, s_prev_rx, s_prev_ry, color_bg());
    }
    s_prev_kind = PREV_NONE;
}

static nino_eye_state_t current_state(void)
{
    return (nino_eye_state_t)s_state;
}

static bool delay_ms_interruptible(int total_ms, nino_eye_state_t expected)
{
    int elapsed = 0;
    while (elapsed < total_ms) {
        if (current_state() != expected) {
            return false;
        }

        int slice_ms = total_ms - elapsed;
        if (slice_ms > 25) {
            slice_ms = 25;
        }
        vTaskDelay(pdMS_TO_TICKS(slice_ms));
        elapsed += slice_ms;
    }

    return current_state() == expected;
}

static int blink_eye_to_position(const nino_state_profile_t *profile,
                                 int current_x,
                                 int next_x,
                                 nino_eye_state_t expected)
{
    int rx = profile->rx;
    int ry = profile->ry;
    int step = profile->blink_step > 0 ? profile->blink_step : BLINK_CLOSE_STEP;
    if (step <= 0) {
        step = 1;
    }
    int frame_ms = profile->blink_ms > 0 ? profile->blink_ms : BLINK_FRAME_MS;

    if (!delay_ms_interruptible(profile->hold_ms / 2, expected)) {
        return current_x;
    }

    /* Geometric close: erase oval rows from top/bottom toward center. */
    int previous_open = ry;
    for (int open = ry - step; open >= 0; open -= step) {
        if (current_state() != expected) {
            return current_x;
        }
        erase_eye_rows(current_x, rx, ry, EYE_CY - previous_open, EYE_CY - open - 1);
        erase_eye_rows(current_x, rx, ry, EYE_CY + open + 1, EYE_CY + previous_open);
        previous_open = open;
        if (!delay_ms_interruptible(frame_ms, expected)) {
            return current_x;
        }
    }

    erase_eye_rows(current_x, rx, ry, EYE_CY - ry, EYE_CY + ry);
    if (!delay_ms_interruptible(profile->closed_hold_ms, expected)) {
        return current_x;
    }

    int previous_reveal = 0;
    draw_eye_rows(next_x, rx, ry, EYE_CY, EYE_CY, color_eye());
    for (int open = step; open <= ry; open += step) {
        if (current_state() != expected) {
            return next_x;
        }
        draw_eye_rows(next_x, rx, ry, EYE_CY - open, EYE_CY - previous_reveal - 1, color_eye());
        draw_eye_rows(next_x, rx, ry, EYE_CY + previous_reveal + 1, EYE_CY + open, color_eye());
        previous_reveal = open;
        if (!delay_ms_interruptible(frame_ms, expected)) {
            return next_x;
        }
    }

    if (previous_reveal < ry) {
        draw_eye_rows(next_x, rx, ry, EYE_CY - ry, EYE_CY - previous_reveal - 1, color_eye());
        draw_eye_rows(next_x, rx, ry, EYE_CY + previous_reveal + 1, EYE_CY + ry, color_eye());
    }

    remember_ellipse(next_x, rx, ry, EYE_CY - ry, EYE_CY + ry);
    return next_x;
}

static void run_blink_profile_once(const nino_state_profile_t *profile, nino_eye_state_t expected)
{
    int center_x = EYE_CX;

    /* Un-draw the previous state's shape (only its own pixels), then draw the
     * eye ONCE. After that we only blink incrementally, so there is no
     * full-eye erase-and-redraw flash on every cycle. */
    erase_prev_eye();
    draw_full_eye(center_x, profile->rx, profile->ry);

    while (current_state() == expected) {
        for (int i = 0; i < profile->gaze_count; i++) {
            if (current_state() != expected) {
                return;
            }
            center_x = blink_eye_to_position(profile,
                                             center_x,
                                             EYE_CX + profile->gaze_offsets[i],
                                             expected);
        }
    }
}

/*
 * Thinking: a normal solid eye (like idle) that slowly rolls around the top -
 * up-left -> up -> up-right -> up ... - to convey pondering. No blink. The whole
 * eye moves; we erase the old position and draw the new one (both touch only
 * eye-shaped pixels, never the surrounding background).
 */
static void run_thinking_eye(const nino_state_profile_t *profile, nino_eye_state_t expected)
{
    const int rx = profile->rx;
    const int ry = profile->ry;
    /* Gaze sequence (dx, dy) relative to screen center, all shifted well up:
     * centre -> up -> left -> up -> right -> (loop). */
    static const int gx[] = {0,   0,  -14,   0,   14};
    static const int gy[] = {-10, -22, -16, -22, -16};
    const int gaze_n = (int)(sizeof(gx) / sizeof(gx[0]));

    erase_prev_eye();

    int prev_cx = 0, prev_cy = 0;
    bool have_eye = false;
    int i = 0;
    while (current_state() == expected) {
        int ex = EYE_CX + gx[i];
        int ey = EYE_CY + gy[i];
        if (have_eye) {
            fill_ellipse(prev_cx, prev_cy, rx, ry, color_bg());
        }
        fill_ellipse(ex, ey, rx, ry, color_eye());
        remember_blob(ex, ey, rx, ry);
        prev_cx = ex;
        prev_cy = ey;
        have_eye = true;
        i = (i + 1) % gaze_n;
        if (!delay_ms_interruptible(2800, expected)) {
            return;
        }
    }
}

void nino_eye_set_state(nino_eye_state_t state)
{
    if (state >= NINO_EYE_STATE_COUNT) {
        return;
    }
    s_state = state;
    ESP_LOGI(TAG, "state set -> %d", (int)state);
}

nino_eye_state_t nino_eye_get_state(void)
{
    return current_state();
}

void nino_eye_idle(void)
{
    nino_eye_set_state(NINO_EYE_IDLE);
}

void nino_eye_listening(void)
{
    nino_eye_set_state(NINO_EYE_LISTENING);
}

void nino_eye_thinking(void)
{
    nino_eye_set_state(NINO_EYE_THINKING);
}

bool nino_eye_apply_command(const char *line)
{
    if (line == NULL) {
        return false;
    }

    while (*line && isspace((unsigned char)*line)) {
        line++;
    }
    if (*line == '\0') {
        return false;
    }

    char token[24];
    size_t len = 0;
    while (line[len] && !isspace((unsigned char)line[len]) && len < sizeof(token) - 1) {
        token[len] = (char)tolower((unsigned char)line[len]);
        len++;
    }
    token[len] = '\0';

    if (token[0] >= '0' && token[0] <= '9' && token[1] == '\0') {
        int value = token[0] - '0';
        if (value >= NINO_EYE_STATE_COUNT) {
            return false;
        }
        nino_eye_set_state((nino_eye_state_t)value);
        return true;
    }

    if (strcmp(token, "idle") == 0) {
        nino_eye_set_state(NINO_EYE_IDLE);
        return true;
    }
    if (strcmp(token, "listening") == 0) {
        nino_eye_set_state(NINO_EYE_LISTENING);
        return true;
    }
    if (strcmp(token, "thinking") == 0) {
        nino_eye_set_state(NINO_EYE_THINKING);
        return true;
    }
    return false;
}

static void run_current_state_once(void)
{
    nino_eye_state_t state = current_state();
    if (state >= NINO_EYE_STATE_COUNT) {
        return;
    }

    const nino_state_profile_t *profile = &s_profiles[state];
    set_eye_color(profile);
    switch (state) {
    case NINO_EYE_THINKING:
        run_thinking_eye(profile, state);
        break;
    default:
        run_blink_profile_once(profile, state);
        break;
    }
}

static void eye_engine_task(void *arg)
{
    (void)arg;
    ESP_LOGI(TAG, "eye engine task started");
    ssd1351_fill_screen(color_bg());

    /* Keep whatever state was set before the task started (defaults to
     * NINO_EYE_IDLE) so an early nino_eye_<emotion>() call isn't overridden. */
    while (true) {
        run_current_state_once();
    }
}

void nino_eye_begin(void)
{
    static bool s_engine_started = false;
    if (s_engine_started) {
        return;
    }
    s_engine_started = true;
    ESP_LOGI(TAG, "Nino eye starting (engine only)");
    xTaskCreate(eye_engine_task, "nino_eye", 4096, NULL, 5, NULL);
}

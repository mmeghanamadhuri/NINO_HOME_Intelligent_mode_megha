#include "nino_eye.h"

#include <ctype.h>
#include <stdio.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "driver/uart.h"
#include "sdkconfig.h"
#include "ssd1351.h"

static const char *TAG = "nino_eye";

/* One SSD1351 OLED per eye, native landscape 128 x 96. */
#define LOGICAL_WIDTH   OLED_WIDTH
#define LOGICAL_HEIGHT  OLED_HEIGHT
#define EYE_CX          (LOGICAL_WIDTH / 2)
/* Global downward shift for all states (idle, happy, tired, thinking, etc.). */
#define NINO_VOFFSET    8
#define EYE_CY          (LOGICAL_HEIGHT / 2 + NINO_VOFFSET)
/* Legacy draw-time trim (0 = off); prefer NINO_VOFFSET for vertical centering. */
#define NINO_VSHIFT     0

/* Only this central region may be drawn/erased (eye, heart, blink). The rest of
 * the screen is never touched after boot — matches "only the oval changes". */
#define EYE_CLIP_HALF_W   46
#define EYE_CLIP_Y0       (EYE_CY - 52)
#define EYE_CLIP_Y1       (EYE_CY + 44)

/* Quick eyelid blink: per-frame delay for the close/open sweep. Smaller =
 * snappier. The number of frames is ry / BLINK_CLOSE_STEP. */
#define BLINK_FRAME_MS      24
#define BLINK_CLOSE_STEP    6
#define MAX_GAZE_POINTS 5

/* 1 = cycle all states for testing. 0 = run only NINO_SINGLE_STATE (loops forever). */
#define DEMO_CYCLE          0
#define NINO_SINGLE_STATE   NINO_EYE_IDLE

/* 1 = show an orientation test (TOP bar + top-left marker) instead of eyes. */
#define NINO_ORIENT_TEST    0

typedef enum {
    NINO_RENDER_BLINK,
    NINO_RENDER_STATIC,
    NINO_RENDER_HEART,
} nino_render_mode_t;

typedef struct {
    nino_render_mode_t mode;
    int rx;
    int ry;
    int top;
    int bottom;
    int hold_ms;
    int closed_hold_ms;
    int blink_step;
    int blink_ms;
    int state_ms;
    int gaze_offsets[MAX_GAZE_POINTS];
    int gaze_count;
    int heart_min_scale;
    int heart_max_scale;
    int heart_frame_ms;
    uint8_t eye_r;       /* emotion eye colour on black background */
    uint8_t eye_g;
    uint8_t eye_b;
} nino_state_profile_t;

static volatile nino_eye_state_t s_state = NINO_EYE_IDLE;

static const nino_state_profile_t s_profiles[NINO_EYE_STATE_COUNT] = {
    /* idle: neutral / half-open, normal pupil, slow blink (~4-7 s -> ~5 s). */
    [NINO_EYE_IDLE] = {
        .mode = NINO_RENDER_BLINK,
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
    /* happy: kept as the single red heart symbol (no eyelid/pupil/blink). */
    [NINO_EYE_HAPPY] = {
        .mode = NINO_RENDER_HEART,
        .state_ms = 900,
        .heart_min_scale = 20,
        .heart_max_scale = 20,
        .heart_frame_ms = 900,
        .eye_r = 255, .eye_g = 40, .eye_b = 70,   /* happy = red heart (only coloured state) */
    },
    /* tired: low eye with heavy lowered lid (bottom sliver visible). Slow blink. */
    [NINO_EYE_TIRED] = {
        .mode = NINO_RENDER_STATIC,
        .rx = 24,
        .ry = 30,
        .top = EYE_CY + 4,
        .bottom = EYE_CY + 30,
        .hold_ms = 4500,
        .closed_hold_ms = 300,
        .blink_step = 2,
        .blink_ms = 45,
        .state_ms = 4000,
        .eye_r = 0, .eye_g = 0, .eye_b = 0,
    },
    /* thinking: a normal solid eye like idle that slowly rolls around the top
     * (looking up + side to side). No blink. */
    [NINO_EYE_THINKING] = {
        .mode = NINO_RENDER_STATIC,
        .rx = 24,
        .ry = 30,
        .state_ms = 4000,
        .eye_r = 0, .eye_g = 0, .eye_b = 0,
    },
    /* curious: wide enlarged eye that tilts up + to a side and holds, then
     * blinks across to the other side (head-tilt, inquisitive). */
    [NINO_EYE_CURIOUS_QUIZ] = {
        .mode = NINO_RENDER_BLINK,
        .rx = 28,
        .ry = 33,
        .hold_ms = 2500,
        .closed_hold_ms = 120,
        .blink_step = 4,
        .blink_ms = 20,
        .gaze_offsets = {0},
        .gaze_count = 1,
        .eye_r = 0, .eye_g = 0, .eye_b = 0,
    },
    /* sad: heavy upper lid covering top 40% of eye. Slow ~6 s lidded blink. */
    [NINO_EYE_SAD] = {
        .mode = NINO_RENDER_STATIC,
        .rx = 24,
        .ry = 30,
        .top = EYE_CY - 6,
        .bottom = EYE_CY + 30,
        .hold_ms = 6000,
        .closed_hold_ms = 300,
        .blink_step = 3,
        .blink_ms = 45,
        .state_ms = 4000,
        .eye_r = 0, .eye_g = 0, .eye_b = 0,
    },
    /* surprised: widest/tallest eye; one fast snap-open on entry, then hold wide
     * (no frantic blink). blink_step/blink_ms tune the entry snap only. */
    [NINO_EYE_SURPRISED] = {
        .mode = NINO_RENDER_STATIC,
        .rx = 27,
        .ry = 36,
        .hold_ms = 5000,
        .blink_step = 8,
        .blink_ms = 12,
        .state_ms = 5000,
        .eye_r = 0, .eye_g = 0, .eye_b = 0,
    },
    /* listening: same wide enlarged eye as curious, but centered - it blinks in
     * place (no left/right tilt). ~3 s blink cycle. */
    [NINO_EYE_LISTENING] = {
        .mode = NINO_RENDER_BLINK,
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
    /* recalling: normal soft eye, slow upward memory-gaze path; slow blink when
     * shifting between look-points (calmer than thinking's roll). */
    [NINO_EYE_RECALLING] = {
        .mode = NINO_RENDER_BLINK,
        .rx = 24,
        .ry = 28,
        .hold_ms = 3600,
        .closed_hold_ms = 280,
        .blink_step = 3,
        .blink_ms = 45,
        .gaze_offsets = {0},
        .gaze_count = 1,
        .eye_r = 0, .eye_g = 0, .eye_b = 0,
    },
};

/* Background is white; eyes are black (per-state colour). */
static uint16_t s_eye_color = 0xFFFF;

static uint16_t color_bg(void)
{
    return ssd1351_color(255, 255, 255);
}

static uint16_t color_eye(void)
{
    return s_eye_color;
}

static uint16_t color_red(void)
{
    return ssd1351_color(255, 0, 0);
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

/*
 * Dirty-rectangle tracking (logical coords). Every shape draw records the area
 * it touches; redraw_bg_region() then erases ONLY that area to background
 * instead of repainting the whole screen. This keeps the white background
 * static during transitions so only the black eye appears to change, rather
 * than a full-screen white "window" wiping top-to-bottom each frame.
 */
static int s_dirty_x0 = LOGICAL_WIDTH;
static int s_dirty_y0 = LOGICAL_HEIGHT;
static int s_dirty_x1 = -1;
static int s_dirty_y1 = -1;

static void dirty_reset(void)
{
    s_dirty_x0 = LOGICAL_WIDTH;
    s_dirty_y0 = LOGICAL_HEIGHT;
    s_dirty_x1 = -1;
    s_dirty_y1 = -1;
}

static void dirty_add(int x0, int y0, int x1, int y1)
{
    if (x0 < s_dirty_x0) s_dirty_x0 = x0;
    if (y0 < s_dirty_y0) s_dirty_y0 = y0;
    if (x1 > s_dirty_x1) s_dirty_x1 = x1;
    if (y1 > s_dirty_y1) s_dirty_y1 = y1;
}

static void draw_landscape_hline(int x, int y, int width, uint16_t color)
{
    int logical_y = y;

    if (logical_y < EYE_CLIP_Y0 || logical_y > EYE_CLIP_Y1 || width <= 0) {
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
    if (width <= 0) {
        return;
    }

    y -= NINO_VSHIFT;
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
    dirty_add(x, logical_y, x + width - 1, logical_y);
}

#if NINO_ORIENT_TEST
static void draw_landscape_rect(int x, int y, int width, int height, uint16_t color)
{
    for (int row = 0; row < height; row++) {
        draw_landscape_hline(x, y + row, width, color);
    }
}
#endif

static void clear_screen(uint16_t color)
{
    ssd1351_fill_screen(color);
    dirty_reset();
}

/*
 * Remember the EXACT shape currently painted, so when the next state begins it
 * can un-draw that shape along its own outline (writing background over only
 * the pixels that were the eye) instead of erasing a white rectangle. Erasing a
 * rectangle re-writes background pixels that were already white, and on this
 * OLED freshly-written white reads slightly different from held white -> the
 * "window". Un-drawing by shape never touches the untouched background.
 */
typedef enum { PREV_NONE, PREV_ELLIPSE, PREV_HEART, PREV_BLOB } prev_kind_t;
static prev_kind_t s_prev_kind = PREV_NONE;
static int s_prev_cx, s_prev_rx, s_prev_ry, s_prev_top, s_prev_bottom;
static int s_prev_blob_cy;
static int s_prev_heart_cx, s_prev_heart_cy, s_prev_heart_scale;

static void remember_ellipse(int cx, int rx, int ry, int top, int bottom)
{
    s_prev_kind = PREV_ELLIPSE;
    s_prev_cx = cx;
    s_prev_rx = rx;
    s_prev_ry = ry;
    s_prev_top = top;
    s_prev_bottom = bottom;
}

static void remember_heart(int cx, int cy, int scale)
{
    s_prev_kind = PREV_HEART;
    s_prev_heart_cx = cx;
    s_prev_heart_cy = cy;
    s_prev_heart_scale = scale;
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

static void erase_eye_rows(int center_x, int rx, int ry, int top, int bottom)
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

    /* Erase EXACTLY the eye footprint (identical columns to draw_eye_rows, no
     * margin). We only flip black eye pixels back to white and never re-touch
     * the surrounding white background, so the static background never flashes
     * (no "window"). */
    for (int y = top; y <= bottom; y++) {
        int dx = ellipse_half_width(rx, ry, y - EYE_CY);
        if (dx < 0) {
            continue;
        }
        draw_landscape_hline(center_x - dx, y, (dx * 2) + 1, color_bg());
    }
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

static void draw_full_eye(int center_x, int rx, int ry)
{
    /* The previous shape was already un-drawn on entry, so just draw the eye;
     * no white rectangle box is painted. */
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

/* Draw rows [top, bottom] of an ellipse centered at an arbitrary (cx, cy),
 * exact footprint (used for blinks at off-center positions). */
static void draw_blob_rows(int cx, int cy, int rx, int ry, int top, int bottom, uint16_t fill)
{
    if (top < 0) {
        top = 0;
    }
    if (bottom >= LOGICAL_HEIGHT) {
        bottom = LOGICAL_HEIGHT - 1;
    }
    for (int y = top; y <= bottom; y++) {
        int dx = ellipse_half_width(rx, ry, y - cy);
        if (dx < 0) {
            continue;
        }
        draw_landscape_hline(cx - dx, y, (dx * 2) + 1, fill);
    }
}

static bool heart_pixel(int lx, int ly, int cx, int cy, int scale)
{
    int x = lx - cx;
    int y = ly - cy;
    int lobe_radius = (scale * 7) / 12;
    int lobe_dx = scale / 2;
    int lobe_y = -scale / 3;

    int left_dx = x + lobe_dx;
    int right_dx = x - lobe_dx;
    int lobe_dy = y - lobe_y;
    bool in_left_lobe = (left_dx * left_dx) + (lobe_dy * lobe_dy) <= lobe_radius * lobe_radius;
    bool in_right_lobe = (right_dx * right_dx) + (lobe_dy * lobe_dy) <= lobe_radius * lobe_radius;

    if (y < -(scale / 5)) {
        return in_left_lobe || in_right_lobe;
    }

    float fx = x / (float)scale;
    float fy = (cy - ly) / (float)scale;
    float a = (fx * fx) + (fy * fy) - 1.0f;
    bool in_smooth_point = ((a * a * a) - (fx * fx * fy * fy * fy)) <= 0.0f;

    return in_left_lobe || in_right_lobe || in_smooth_point;
}

static void draw_heart(int cx, int cy, int scale, uint16_t color)
{
    int x0 = cx - (scale * 2);
    int x1 = cx + (scale * 2);
    int y0 = cy - scale - 2;
    int y1 = cy + scale + 4;
    if (x0 < 0) {
        x0 = 0;
    }
    if (x1 >= LOGICAL_WIDTH) {
        x1 = LOGICAL_WIDTH - 1;
    }
    if (y0 < 0) {
        y0 = 0;
    }
    if (y1 >= LOGICAL_HEIGHT) {
        y1 = LOGICAL_HEIGHT - 1;
    }

    for (int y = y0; y <= y1; y++) {
        int span_start = -1;
        for (int x = x0; x <= x1; x++) {
            bool in_heart = heart_pixel(x, y, cx, cy, scale);
            if (in_heart && span_start < 0) {
                span_start = x;
            } else if (!in_heart && span_start >= 0) {
                draw_landscape_hline(span_start, y, x - span_start, color);
                span_start = -1;
            }
        }

        if (span_start >= 0) {
            draw_landscape_hline(span_start, y, x1 - span_start + 1, color);
        }
    }
}

/*
 * Un-draw the previously painted shape by re-rendering it in the background
 * colour using the SAME geometry, so only the pixels that were the eye/heart
 * are flipped back to white. The surrounding background is never re-touched.
 */
static void erase_prev_eye(void)
{
    if (s_prev_kind == PREV_ELLIPSE) {
        draw_eye_rows(s_prev_cx, s_prev_rx, s_prev_ry, s_prev_top, s_prev_bottom, color_bg());
    } else if (s_prev_kind == PREV_HEART) {
        draw_heart(s_prev_heart_cx, s_prev_heart_cy, s_prev_heart_scale, color_bg());
    } else if (s_prev_kind == PREV_BLOB) {
        fill_ellipse(s_prev_cx, s_prev_blob_cy, s_prev_rx, s_prev_ry, color_bg());
    }
    s_prev_kind = PREV_NONE;
    dirty_reset();
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

    /* Geometric close: erase oval rows from top/bottom toward center (profile
     * blink_step + blink_ms set the original per-state timing). */
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

static void draw_static_eye(const nino_state_profile_t *profile)
{
    erase_prev_eye();
    draw_eye_rows(EYE_CX, profile->rx, profile->ry, profile->top, profile->bottom, color_eye());
    remember_ellipse(EYE_CX, profile->rx, profile->ry, profile->top, profile->bottom);
}

static void run_blink_profile_once(const nino_state_profile_t *profile, nino_eye_state_t expected)
{
    int center_x = EYE_CX;

    /* Un-draw the previous state's shape (only its own pixels), then draw the
     * eye ONCE. After that we only blink/move incrementally, so there is no
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

static void run_static_profile_once(const nino_state_profile_t *profile, nino_eye_state_t expected)
{
    /* Draw once, then hold without re-erasing/redrawing so the eye stays
     * perfectly steady (no periodic white flash that looks like a blink). */
    draw_static_eye(profile);
    while (delay_ms_interruptible(profile->state_ms, expected)) {
        /* still in this state: keep holding the same steady image */
    }
}

/*
 * Tired blink: the eye sits in its lidded window [top, bottom] (top 30% already
 * covered). On each cycle the lids close from both edges toward the window's
 * mid row, hold briefly, then reopen back to the lidded window. All erases use
 * the exact eye footprint, so the background is never re-touched (no "window").
 */
static void run_lidded_blink(const nino_state_profile_t *profile, nino_eye_state_t expected)
{
    const int rx = profile->rx;
    const int ry = profile->ry;
    const int top = profile->top;
    const int bottom = profile->bottom;
    const int cy = (top + bottom) / 2;
    int step = profile->blink_step > 0 ? profile->blink_step : BLINK_CLOSE_STEP;
    if (step <= 0) {
        step = 1;
    }
    const int frame_ms = profile->blink_ms > 0 ? profile->blink_ms : BLINK_FRAME_MS;

    erase_prev_eye();
    draw_eye_rows(EYE_CX, rx, ry, top, bottom, color_eye());
    remember_ellipse(EYE_CX, rx, ry, top, bottom);

    while (current_state() == expected) {
        if (!delay_ms_interruptible(profile->hold_ms, expected)) {
            return;
        }

        int cur_top = top;
        int cur_bot = bottom;
        for (int off = step; ; off += step) {
            if (current_state() != expected) {
                return;
            }
            int new_top = top + off;
            int new_bot = bottom - off;
            if (new_top > cy) {
                new_top = cy;
            }
            if (new_bot < cy) {
                new_bot = cy;
            }
            erase_eye_rows(EYE_CX, rx, ry, cur_top, new_top - 1);
            erase_eye_rows(EYE_CX, rx, ry, new_bot + 1, cur_bot);
            cur_top = new_top;
            cur_bot = new_bot;
            if (!delay_ms_interruptible(frame_ms, expected)) {
                return;
            }
            if (new_top >= cy && new_bot <= cy) {
                break;
            }
        }

        erase_eye_rows(EYE_CX, rx, ry, top, bottom);
        if (!delay_ms_interruptible(profile->closed_hold_ms, expected)) {
            return;
        }

        cur_top = cy;
        cur_bot = cy;
        draw_eye_rows(EYE_CX, rx, ry, cy, cy, color_eye());
        for (int off = step; ; off += step) {
            if (current_state() != expected) {
                return;
            }
            int new_top = cy - off;
            int new_bot = cy + off;
            if (new_top < top) {
                new_top = top;
            }
            if (new_bot > bottom) {
                new_bot = bottom;
            }
            draw_eye_rows(EYE_CX, rx, ry, new_top, cur_top - 1, color_eye());
            draw_eye_rows(EYE_CX, rx, ry, cur_bot + 1, new_bot, color_eye());
            cur_top = new_top;
            cur_bot = new_bot;
            if (!delay_ms_interruptible(frame_ms, expected)) {
                return;
            }
            if (new_top <= top && new_bot >= bottom) {
                break;
            }
        }

        draw_eye_rows(EYE_CX, rx, ry, top, bottom, color_eye());
        remember_ellipse(EYE_CX, rx, ry, top, bottom);
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

/*
 * Blink that also moves the eye: close at (cx0, cy0), then open at (cx1, cy1).
 * Used to tilt the curious eye from one side to the other during the blink.
 * All erases/draws use the exact eye footprint, so the background is untouched.
 */
static bool blink_move_blob(int cx0, int cy0, int cx1, int cy1, int rx, int ry,
                            int step, int frame_ms, int closed_hold,
                            nino_eye_state_t expected)
{
    int previous_open = ry;
    for (int open = ry - step; open >= 0; open -= step) {
        if (current_state() != expected) {
            return false;
        }
        draw_blob_rows(cx0, cy0, rx, ry, cy0 - previous_open, cy0 - open - 1, color_bg());
        draw_blob_rows(cx0, cy0, rx, ry, cy0 + open + 1, cy0 + previous_open, color_bg());
        previous_open = open;
        if (!delay_ms_interruptible(frame_ms, expected)) {
            return false;
        }
    }

    draw_blob_rows(cx0, cy0, rx, ry, cy0 - ry, cy0 + ry, color_bg());
    if (!delay_ms_interruptible(closed_hold, expected)) {
        return false;
    }

    int previous_reveal = 0;
    draw_blob_rows(cx1, cy1, rx, ry, cy1, cy1, color_eye());
    for (int open = step; open <= ry; open += step) {
        if (current_state() != expected) {
            return false;
        }
        draw_blob_rows(cx1, cy1, rx, ry, cy1 - open, cy1 - previous_reveal - 1, color_eye());
        draw_blob_rows(cx1, cy1, rx, ry, cy1 + previous_reveal + 1, cy1 + open, color_eye());
        previous_reveal = open;
        if (!delay_ms_interruptible(frame_ms, expected)) {
            return false;
        }
    }

    if (previous_reveal < ry) {
        draw_blob_rows(cx1, cy1, rx, ry, cy1 - ry, cy1 - previous_reveal - 1, color_eye());
        draw_blob_rows(cx1, cy1, rx, ry, cy1 + previous_reveal + 1, cy1 + ry, color_eye());
    }

    remember_blob(cx1, cy1, rx, ry);
    return true;
}

/*
 * Curious: a wide, enlarged eye that tilts up-and-to-a-side and holds that
 * inquisitive look, then blinks across to the other side and holds again.
 */
static void run_curious_eye(const nino_state_profile_t *profile, nino_eye_state_t expected)
{
    const int rx = profile->rx;
    const int ry = profile->ry;
    int step = profile->blink_step > 0 ? profile->blink_step : BLINK_CLOSE_STEP;
    if (step <= 0) {
        step = 1;
    }
    const int frame_ms = profile->blink_ms > 0 ? profile->blink_ms : BLINK_FRAME_MS;

    /* Tilted look-points: up-left then up-right (head-tilt feel). */
    static const int px[] = {-16, 16};
    static const int py[] = {-10, -10};
    const int n = (int)(sizeof(px) / sizeof(px[0]));

    erase_prev_eye();
    int i = 0;
    int cx = EYE_CX + px[i];
    int cy = EYE_CY + py[i];
    fill_ellipse(cx, cy, rx, ry, color_eye());
    remember_blob(cx, cy, rx, ry);

    while (current_state() == expected) {
        if (!delay_ms_interruptible(profile->hold_ms, expected)) {
            return;
        }
        int next = (i + 1) % n;
        int ncx = EYE_CX + px[next];
        int ncy = EYE_CY + py[next];
        if (!blink_move_blob(cx, cy, ncx, ncy, rx, ry, step, frame_ms,
                             profile->closed_hold_ms, expected)) {
            return;
        }
        i = next;
        cx = ncx;
        cy = ncy;
    }
}

static void snap_open_eye(int cx, int rx, int ry, int step, int frame_ms, nino_eye_state_t expected)
{
    int previous_reveal = 0;
    draw_eye_rows(cx, rx, ry, EYE_CY, EYE_CY, color_eye());
    for (int open = step; open <= ry; open += step) {
        if (current_state() != expected) {
            return;
        }
        draw_eye_rows(cx, rx, ry, EYE_CY - open, EYE_CY - previous_reveal - 1, color_eye());
        draw_eye_rows(cx, rx, ry, EYE_CY + previous_reveal + 1, EYE_CY + open, color_eye());
        previous_reveal = open;
        if (!delay_ms_interruptible(frame_ms, expected)) {
            return;
        }
    }
    if (previous_reveal < ry) {
        draw_eye_rows(cx, rx, ry, EYE_CY - ry, EYE_CY - previous_reveal - 1, color_eye());
        draw_eye_rows(cx, rx, ry, EYE_CY + previous_reveal + 1, EYE_CY + ry, color_eye());
    }
}

/*
 * Surprised: fast snap-open on entry (profile blink_step/blink_ms), then hold.
 */
static void run_surprised_eye(const nino_state_profile_t *profile, nino_eye_state_t expected)
{
    const int rx = profile->rx;
    const int ry = profile->ry;
    int step = profile->blink_step > 0 ? profile->blink_step : 8;
    if (step <= 0) {
        step = 1;
    }
    const int frame_ms = profile->blink_ms > 0 ? profile->blink_ms : 12;
    const int hold_ms = profile->hold_ms > 0 ? profile->hold_ms : 5000;

    erase_prev_eye();
    snap_open_eye(EYE_CX, rx, ry, step, frame_ms, expected);
    if (current_state() != expected) {
        return;
    }
    remember_ellipse(EYE_CX, rx, ry, EYE_CY - ry, EYE_CY + ry);

    while (delay_ms_interruptible(hold_ms, expected)) {
        /* hold wide open */
    }
}

/*
 * Recalling: softer normal eye drifting upward through memory-gaze points
 * (centre -> up-left -> up -> up-right -> centre). Holds at each point, then
 * a slow blink while shifting to the next — introspective, not frantic.
 */
static void run_recalling_eye(const nino_state_profile_t *profile, nino_eye_state_t expected)
{
    const int rx = profile->rx;
    const int ry = profile->ry;
    int step = profile->blink_step > 0 ? profile->blink_step : 3;
    if (step <= 0) {
        step = 1;
    }
    const int frame_ms = profile->blink_ms > 0 ? profile->blink_ms : 45;

    static const int px[] = {0, -12, 0, 12, 0};
    static const int py[] = {-4, -12, -16, -12, -6};
    const int n = (int)(sizeof(px) / sizeof(px[0]));

    erase_prev_eye();
    int i = 0;
    int cx = EYE_CX + px[i];
    int cy = EYE_CY + py[i];
    fill_ellipse(cx, cy, rx, ry, color_eye());
    remember_blob(cx, cy, rx, ry);

    while (current_state() == expected) {
        if (!delay_ms_interruptible(profile->hold_ms, expected)) {
            return;
        }
        int next = (i + 1) % n;
        int ncx = EYE_CX + px[next];
        int ncy = EYE_CY + py[next];
        if (!blink_move_blob(cx, cy, ncx, ncy, rx, ry, step, frame_ms,
                             profile->closed_hold_ms, expected)) {
            return;
        }
        i = next;
        cx = ncx;
        cy = ncy;
    }
}

static void run_heart_profile_once(const nino_state_profile_t *profile, nino_eye_state_t expected)
{
    erase_prev_eye();
    /* Heart's pointed bottom reaches lower than its lobes rise, so lift the
     * center well above mid-screen to keep the symbol visually centered. */
    draw_heart(EYE_CX, EYE_CY - 4, profile->heart_max_scale, color_red());
    remember_heart(EYE_CX, EYE_CY - 4, profile->heart_max_scale);
    while (delay_ms_interruptible(profile->state_ms, expected)) {
        /* Happy is intentionally still: one red symbol, no pulse or flicker. */
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

/* ---- Per-emotion triggers (thin wrappers over nino_eye_set_state) ---- */
void nino_eye_idle(void)      { nino_eye_set_state(NINO_EYE_IDLE); }
void nino_eye_happy(void)     { nino_eye_set_state(NINO_EYE_HAPPY); }
void nino_eye_tired(void)     { nino_eye_set_state(NINO_EYE_TIRED); }
void nino_eye_thinking(void)  { nino_eye_set_state(NINO_EYE_THINKING); }
void nino_eye_curious(void)   { nino_eye_set_state(NINO_EYE_CURIOUS_QUIZ); }
void nino_eye_sad(void)       { nino_eye_set_state(NINO_EYE_SAD); }
void nino_eye_surprised(void) { nino_eye_set_state(NINO_EYE_SURPRISED); }
void nino_eye_listening(void) { nino_eye_set_state(NINO_EYE_LISTENING); }
void nino_eye_recalling(void) { nino_eye_set_state(NINO_EYE_RECALLING); }

static bool name_to_state(const char *name, nino_eye_state_t *out)
{
    if (strcmp(name, "idle") == 0) {
        *out = NINO_EYE_IDLE;
    } else if (strcmp(name, "happy") == 0) {
        *out = NINO_EYE_HAPPY;
    } else if (strcmp(name, "tired") == 0) {
        *out = NINO_EYE_TIRED;
    } else if (strcmp(name, "thinking") == 0) {
        *out = NINO_EYE_THINKING;
    } else if (strcmp(name, "curious") == 0 || strcmp(name, "quiz") == 0) {
        *out = NINO_EYE_CURIOUS_QUIZ;
    } else if (strcmp(name, "sad") == 0) {
        *out = NINO_EYE_SAD;
    } else if (strcmp(name, "surprised") == 0) {
        *out = NINO_EYE_SURPRISED;
    } else if (strcmp(name, "listening") == 0) {
        *out = NINO_EYE_LISTENING;
    } else if (strcmp(name, "recalling") == 0) {
        *out = NINO_EYE_RECALLING;
    } else {
        return false;
    }
    return true;
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

    nino_eye_state_t state;
    if (!name_to_state(token, &state)) {
        return false;
    }

    nino_eye_set_state(state);
    return true;
}

static void print_command_help(void)
{
    printf("\nNINO eye — type a state name and press Enter:\n");
    printf("  idle  happy  tired  thinking  curious\n");
    printf("  sad   surprised  listening  recalling\n> ");
    fflush(stdout);
}

nino_eye_state_t nino_eye_get_state(void)
{
    return current_state();
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
    case NINO_EYE_HAPPY:
        run_heart_profile_once(profile, state);
        break;
    case NINO_EYE_TIRED:
        run_lidded_blink(profile, state);
        break;
    case NINO_EYE_THINKING:
        run_thinking_eye(profile, state);
        break;
    case NINO_EYE_CURIOUS_QUIZ:
        run_curious_eye(profile, state);
        break;
    case NINO_EYE_SAD:
        run_lidded_blink(profile, state);
        break;
    case NINO_EYE_SURPRISED:
        run_surprised_eye(profile, state);
        break;
    case NINO_EYE_RECALLING:
        run_recalling_eye(profile, state);
        break;
    default:
        if (profile->mode == NINO_RENDER_STATIC) {
            run_static_profile_once(profile, state);
        } else {
            run_blink_profile_once(profile, state);
        }
        break;
    }
}

#if NINO_ORIENT_TEST
static void orientation_test(void)
{
    clear_screen(color_bg());

    /* Whole TOP half lit white (logical y = 0 .. mid). Bottom half stays black. */
    draw_landscape_rect(0, 0, LOGICAL_WIDTH, LOGICAL_HEIGHT / 2, ssd1351_color(255, 255, 255));

    /* Small RED square in the logical TOP-LEFT corner (x = 0, y = 0). */
    draw_landscape_rect(0, 0, 22, 22, ssd1351_color(255, 0, 0));

    while (true) {
        vTaskDelay(pdMS_TO_TICKS(1000));
    }
}
#endif

static void eye_engine_task(void *arg)
{
    (void)arg;
    ESP_LOGI(TAG, "eye engine task started");
    clear_screen(color_bg());

#if NINO_ORIENT_TEST
    orientation_test();
    return;
#endif

#if DEMO_CYCLE
    while (true) {
        for (nino_eye_state_t state = NINO_EYE_IDLE;
             state < NINO_EYE_STATE_COUNT;
             state = (nino_eye_state_t)(state + 1)) {
            s_state = state;
            run_current_state_once();
        }
    }
#else
    /* Keep whatever state was set before the task started (defaults to
     * NINO_EYE_IDLE) so an early nino_eye_<emotion>() call isn't overridden. */
    ESP_LOGI(TAG, "default state %d (change via serial or nino_eye_<emotion>())", (int)s_state);

    while (true) {
        run_current_state_once();
    }
#endif
}

/*
 * Read the console UART directly via the driver instead of fgets()/stdin. The
 * default stdin is non-blocking, so fgets() returns on the first character and
 * multi-character names ("listening") never assemble. Installing the UART driver
 * and reading bytes ourselves (assembling a line until CR/LF) is reliable.
 */
static void console_init(void)
{
    setvbuf(stdout, NULL, _IONBF, 0);

    const uart_config_t uart_config = {
        .baud_rate = CONFIG_ESP_CONSOLE_UART_BAUDRATE,
        .data_bits = UART_DATA_8_BITS,
        .parity = UART_PARITY_DISABLE,
        .stop_bits = UART_STOP_BITS_1,
        .source_clk = UART_SCLK_DEFAULT,
    };
    if (!uart_is_driver_installed(CONFIG_ESP_CONSOLE_UART_NUM)) {
        ESP_ERROR_CHECK(uart_driver_install(CONFIG_ESP_CONSOLE_UART_NUM, 256, 0, 0, NULL, 0));
    }
    uart_param_config(CONFIG_ESP_CONSOLE_UART_NUM, &uart_config);
}

static void input_task(void *arg)
{
    (void)arg;
    char line[32];
    int len = 0;

    console_init();
    vTaskDelay(pdMS_TO_TICKS(500));
    /* Discard any boot-time line noise / break byte sitting in the RX FIFO so it
     * doesn't show up as a stray symbol before the first command. */
    uart_flush_input(CONFIG_ESP_CONSOLE_UART_NUM);
    print_command_help();

    while (true) {
        uint8_t ch = 0;
        int n = uart_read_bytes(CONFIG_ESP_CONSOLE_UART_NUM, &ch, 1, pdMS_TO_TICKS(100));
        if (n <= 0) {
            continue;
        }

        if (ch == '\r' || ch == '\n') {
            if (len == 0) {
                continue;
            }
            line[len] = '\0';
            len = 0;
            printf("\n");
            if (nino_eye_apply_command(line)) {
                printf("OK -> state %d\n> ", (int)s_state);
            } else {
                printf("Unknown. Type a state name (idle/happy/listening/...)\n> ");
            }
        } else if (ch == 0x08 || ch == 0x7F) {       /* backspace */
            if (len > 0) {
                len--;
                printf("\b \b");
            }
        } else if (ch >= 0x20 && ch < 0x7F && len < (int)sizeof(line) - 1) {
            /* Only accept printable ASCII; ignore stray control/noise bytes. */
            line[len++] = (char)ch;
            printf("%c", ch);                         /* echo so typing is visible */
        }
    }
}

/* Spawn the animator task once; safe to call multiple times. */
static void start_engine_once(void)
{
    static bool s_engine_started = false;
    if (s_engine_started) {
        return;
    }
    s_engine_started = true;
    xTaskCreate(eye_engine_task, "nino_eye", 8192, NULL, 5, NULL);
}

void nino_eye_begin(void)
{
    ESP_LOGI(TAG, "Nino eye starting (engine only)");
    start_engine_once();
}

void nino_eye_start(void)
{
    ESP_LOGI(TAG, "Nino eye starting (engine + serial input on monitor UART)");
    start_engine_once();
    xTaskCreate(input_task, "nino_in", 4096, NULL, 4, NULL);
}

void nino_eye_run(void)
{
    nino_eye_start();
}


nino_eye.h

#pragma once

#include <stdbool.h>

typedef enum {
    NINO_EYE_IDLE = 0,
    NINO_EYE_HAPPY,
    NINO_EYE_TIRED,
    NINO_EYE_THINKING,
    NINO_EYE_CURIOUS_QUIZ,
    NINO_EYE_SAD,
    NINO_EYE_SURPRISED,
    NINO_EYE_LISTENING,
    NINO_EYE_RECALLING,
    NINO_EYE_STATE_COUNT,
} nino_eye_state_t;

void nino_eye_set_state(nino_eye_state_t state);
nino_eye_state_t nino_eye_get_state(void);

/** Parse monitor input: "0"-"8", or idle/happy/tired/... */
bool nino_eye_apply_command(const char *line);

/*
 * Start the eye system, then trigger any emotion from anywhere in your code.
 *
 * Integration in 2 steps:
 *   1) Call nino_eye_begin() ONCE after ssd1351_init() (it spawns the animator
 *      task in the background and returns immediately).
 *   2) Call any nino_eye_<emotion>() function whenever you want that emotion.
 *      The change is instant and non-blocking - the running animation switches
 *      to the new one on its next frame.
 *
 * Example:
 *      ssd1351_init();
 *      nino_eye_begin();        // start once
 *      ...
 *      nino_eye_happy();        // show happy
 *      nino_eye_listening();    // later, switch to listening
 */

/** Start ONLY the animation engine (no serial listener). Use this when
 *  integrating into your own project and driving emotions from code. */
void nino_eye_begin(void);

/** Start animation engine + the serial-monitor command listener (for testing,
 *  lets you type "happy", "idle", ... in the monitor). */
void nino_eye_start(void);

/** Same as nino_eye_start(). */
void nino_eye_run(void);

/* ---- Per-emotion triggers: call any of these from anywhere ---- */
void nino_eye_idle(void);
void nino_eye_happy(void);
void nino_eye_tired(void);
void nino_eye_thinking(void);
void nino_eye_curious(void);
void nino_eye_sad(void);
void nino_eye_surprised(void);
void nino_eye_listening(void);
void nino_eye_recalling(void);



main.c

#include "nino_eye.h"
#include "ssd1351.h"

#include "esp_log.h"

static const char *TAG = "nino_eye_app";

/*
 * Integrating into your own project:
 *
 *   1) ssd1351_init();        // bring up the displays (once)
 *   2) nino_eye_begin();      // start the eye animator (once, non-blocking)
 *   3) call any emotion whenever you want, from any task/function:
 *          nino_eye_happy();
 *          nino_eye_listening();
 *          nino_eye_sad();
 *      ... etc. The switch is instant and non-blocking.
 *
 * Below uses nino_eye_start() instead of nino_eye_begin() so you can also type
 * state names ("happy", "idle", ...) in the serial monitor for quick testing.
 * For a production build, replace nino_eye_start() with nino_eye_begin().
 */
void app_main(void)
{
    ESP_ERROR_CHECK(ssd1351_init());
    ESP_LOGI(TAG, "NINO eyes ready (dual OLED) — type state in monitor (0-8 or happy/sad/...)");
    nino_eye_start();

    /* Example of code-driven control (uncomment to use instead of serial):
     *   nino_eye_begin();
     *   nino_eye_happy();
     */
}

ssd1351.c

#include "ssd1351.h"

#include <string.h>
#include "driver/gpio.h"
#include "driver/spi_master.h"
#include "esp_check.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

static const char *TAG = "ssd1351";

/* SSD1351 command set */
#define SSD1351_CMD_SETCOLUMN     0x15
#define SSD1351_CMD_SETROW        0x75
#define SSD1351_CMD_WRITERAM      0x5C
#define SSD1351_CMD_SETREMAP      0xA0
#define SSD1351_CMD_STARTLINE     0xA1
#define SSD1351_CMD_DISPLAYOFFSET 0xA2
#define SSD1351_CMD_NORMALDISPLAY 0xA6
#define SSD1351_CMD_DISPLAYALLOFF 0xA4
#define SSD1351_CMD_DISPLAYOFF    0xAE
#define SSD1351_CMD_DISPLAYON     0xAF
#define SSD1351_CMD_FUNCTIONSEL   0xAB
#define SSD1351_CMD_PRECHARGE     0xB1
#define SSD1351_CMD_DISPLAYENH    0xB2
#define SSD1351_CMD_CLOCKDIV      0xB3
#define SSD1351_CMD_SETVSL        0xB4
#define SSD1351_CMD_SETGPIO       0xB5
#define SSD1351_CMD_PRECHARGE2    0xB6
#define SSD1351_CMD_VCOMH         0xBE
#define SSD1351_CMD_PRECHARGEV    0xBB
#define SSD1351_CMD_CONTRASTABC   0xC1
#define SSD1351_CMD_CONTRASTMAST  0xC7
#define SSD1351_CMD_MUXRATIO      0xCA
#define SSD1351_CMD_COMMANDLOCK   0xFD

#define SPI_HOST_ID     SPI2_HOST
#define SPI_CLOCK_HZ    (20 * 1000 * 1000)
#define CHUNK_PIXELS    2048

static const int s_cs_pins[OLED_COUNT] = { OLED_PIN_CS0, OLED_PIN_CS1 };
static spi_device_handle_t s_spi[OLED_COUNT];
static int s_target = SSD1351_TARGET_ALL;
static uint8_t s_color_chunk[CHUNK_PIXELS * 2];

static void dev_write_cmd(spi_device_handle_t dev, uint8_t cmd)
{
    gpio_set_level(OLED_PIN_DC, 0);

    spi_transaction_t trans = {
        .length = 8,
        .tx_buffer = &cmd,
    };
    ESP_ERROR_CHECK(spi_device_polling_transmit(dev, &trans));
}

static void dev_write_data(spi_device_handle_t dev, const uint8_t *data, size_t len)
{
    if (len == 0) {
        return;
    }

    gpio_set_level(OLED_PIN_DC, 1);

    spi_transaction_t trans = {
        .length = len * 8,
        .tx_buffer = data,
    };
    ESP_ERROR_CHECK(spi_device_polling_transmit(dev, &trans));
}

static void dev_write_data_byte(spi_device_handle_t dev, uint8_t value)
{
    dev_write_data(dev, &value, 1);
}

static void dev_set_window(spi_device_handle_t dev, int x0, int y0, int x1, int y1)
{
    dev_write_cmd(dev, SSD1351_CMD_SETCOLUMN);
    dev_write_data_byte(dev, (uint8_t)x0);
    dev_write_data_byte(dev, (uint8_t)x1);

    dev_write_cmd(dev, SSD1351_CMD_SETROW);
    dev_write_data_byte(dev, (uint8_t)y0);
    dev_write_data_byte(dev, (uint8_t)y1);

    dev_write_cmd(dev, SSD1351_CMD_WRITERAM);
}

static int target_first(void)
{
    return (s_target == SSD1351_TARGET_ALL) ? 0 : s_target;
}

static int target_last(void)
{
    return (s_target == SSD1351_TARGET_ALL) ? (OLED_COUNT - 1) : s_target;
}

void ssd1351_target(int target)
{
    if (target != SSD1351_TARGET_ALL && (target < 0 || target >= OLED_COUNT)) {
        return;
    }
    s_target = target;
}

int ssd1351_get_target(void)
{
    return s_target;
}

static esp_err_t init_spi_bus(void)
{
    spi_bus_config_t buscfg = {
        .mosi_io_num = OLED_PIN_MOSI,
        .miso_io_num = -1,
        .sclk_io_num = OLED_PIN_SCLK,
        .quadwp_io_num = -1,
        .quadhd_io_num = -1,
        .max_transfer_sz = OLED_WIDTH * OLED_HEIGHT * 2,
    };

    esp_err_t err = spi_bus_initialize(SPI_HOST_ID, &buscfg, SPI_DMA_CH_AUTO);
    if (err != ESP_OK && err != ESP_ERR_INVALID_STATE) {
        return err;
    }

    for (int i = 0; i < OLED_COUNT; i++) {
        spi_device_interface_config_t devcfg = {
            .clock_speed_hz = SPI_CLOCK_HZ,
            .mode = 0,
            .spics_io_num = s_cs_pins[i],
            .queue_size = 1,
            .flags = SPI_DEVICE_NO_DUMMY,
        };

        err = spi_bus_add_device(SPI_HOST_ID, &devcfg, &s_spi[i]);
        if (err != ESP_OK) {
            return err;
        }
    }

    return ESP_OK;
}

static void hardware_reset(void)
{
    gpio_set_level(OLED_PIN_RST, 1);
    vTaskDelay(pdMS_TO_TICKS(10));
    gpio_set_level(OLED_PIN_RST, 0);
    vTaskDelay(pdMS_TO_TICKS(20));
    gpio_set_level(OLED_PIN_RST, 1);
    vTaskDelay(pdMS_TO_TICKS(120));
}

static void init_panel(spi_device_handle_t dev)
{
    dev_write_cmd(dev, SSD1351_CMD_COMMANDLOCK);
    dev_write_data_byte(dev, 0x12);
    dev_write_cmd(dev, SSD1351_CMD_COMMANDLOCK);
    dev_write_data_byte(dev, 0xB1);

    dev_write_cmd(dev, SSD1351_CMD_DISPLAYOFF);

    dev_write_cmd(dev, SSD1351_CMD_CLOCKDIV);
    dev_write_data_byte(dev, 0xF1);

    /*
     * 1.27" 128x96 panel mapping (matches the known-good fbtft/Waveshare
     * sequence for this module): full 128 MUX, display start line = 96, and
     * zero display offset. This aligns RAM rows 0..95 to the visible 96 rows
     * top-to-bottom (earlier MUX 0x5F + offset 0x60 squeezed it into a band).
     */
    dev_write_cmd(dev, SSD1351_CMD_MUXRATIO);
    dev_write_data_byte(dev, 0x7F);

    dev_write_cmd(dev, SSD1351_CMD_DISPLAYOFFSET);
    dev_write_data_byte(dev, 0x00);

    dev_write_cmd(dev, SSD1351_CMD_STARTLINE);
    dev_write_data_byte(dev, 0x60);

    /* 0x74: 65k colour, horizontal increment, COM split + scan as per Waveshare. */
    dev_write_cmd(dev, SSD1351_CMD_SETREMAP);
    dev_write_data_byte(dev, 0x74);

    dev_write_cmd(dev, SSD1351_CMD_SETGPIO);
    dev_write_data_byte(dev, 0x00);

    dev_write_cmd(dev, SSD1351_CMD_FUNCTIONSEL);
    dev_write_data_byte(dev, 0x01); /* internal VDD regulator */

    dev_write_cmd(dev, SSD1351_CMD_PRECHARGE);
    dev_write_data_byte(dev, 0x32);

    dev_write_cmd(dev, SSD1351_CMD_DISPLAYENH);
    dev_write_data_byte(dev, 0xA4);
    dev_write_data_byte(dev, 0x00);
    dev_write_data_byte(dev, 0x00);

    dev_write_cmd(dev, SSD1351_CMD_SETVSL);
    dev_write_data_byte(dev, 0xA0);
    dev_write_data_byte(dev, 0xB5);
    dev_write_data_byte(dev, 0x55);

    dev_write_cmd(dev, SSD1351_CMD_PRECHARGEV);
    dev_write_data_byte(dev, 0x17);

    dev_write_cmd(dev, SSD1351_CMD_PRECHARGE2);
    dev_write_data_byte(dev, 0x01);

    dev_write_cmd(dev, SSD1351_CMD_VCOMH);
    dev_write_data_byte(dev, 0x05);

    dev_write_cmd(dev, SSD1351_CMD_CONTRASTABC);
    dev_write_data_byte(dev, 0xC8);
    dev_write_data_byte(dev, 0x80);
    dev_write_data_byte(dev, 0xC8);

    dev_write_cmd(dev, SSD1351_CMD_CONTRASTMAST);
    dev_write_data_byte(dev, 0x0F);

    dev_write_cmd(dev, SSD1351_CMD_NORMALDISPLAY);

    dev_write_cmd(dev, SSD1351_CMD_DISPLAYON);
    vTaskDelay(pdMS_TO_TICKS(50));
}

esp_err_t ssd1351_init(void)
{
    gpio_config_t io = {
        .pin_bit_mask = (1ULL << OLED_PIN_DC) | (1ULL << OLED_PIN_RST),
        .mode = GPIO_MODE_OUTPUT,
        .pull_up_en = GPIO_PULLUP_DISABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type = GPIO_INTR_DISABLE,
    };
    ESP_ERROR_CHECK(gpio_config(&io));

    gpio_set_level(OLED_PIN_DC, 1);
    gpio_set_level(OLED_PIN_RST, 1);

    ESP_RETURN_ON_ERROR(init_spi_bus(), TAG, "spi init failed");

    /* RST is shared, so one reset pulse covers all panels. */
    hardware_reset();
    for (int i = 0; i < OLED_COUNT; i++) {
        init_panel(s_spi[i]);
    }

    s_target = SSD1351_TARGET_ALL;
    ssd1351_fill_screen(0x0000);

    ESP_LOGI(TAG, "SSD1351 ready: %d panel(s) %dx%d", OLED_COUNT, OLED_WIDTH, OLED_HEIGHT);
    return ESP_OK;
}

void ssd1351_fill_rect(int x, int y, int w, int h, uint16_t color)
{
    if (w <= 0 || h <= 0) {
        return;
    }

    if (x < 0) {
        w += x;
        x = 0;
    }
    if (y < 0) {
        h += y;
        y = 0;
    }
    if (x + w > OLED_WIDTH) {
        w = OLED_WIDTH - x;
    }
    if (y + h > OLED_HEIGHT) {
        h = OLED_HEIGHT - y;
    }
    if (w <= 0 || h <= 0) {
        return;
    }

    const uint8_t hi = (uint8_t)(color >> 8);
    const uint8_t lo = (uint8_t)(color & 0xFF);

    const size_t total = (size_t)w * (size_t)h;
    size_t prefill = total > CHUNK_PIXELS ? CHUNK_PIXELS : total;
    for (size_t i = 0; i < prefill; i++) {
        s_color_chunk[i * 2] = hi;
        s_color_chunk[(i * 2) + 1] = lo;
    }

    for (int d = target_first(); d <= target_last(); d++) {
        spi_device_handle_t dev = s_spi[d];
        dev_set_window(dev, x, y, x + w - 1, y + h - 1);

        size_t remaining = total;
        while (remaining > 0) {
            size_t batch = remaining > CHUNK_PIXELS ? CHUNK_PIXELS : remaining;

            gpio_set_level(OLED_PIN_DC, 1);
            spi_transaction_t trans = {
                .length = batch * 16,
                .tx_buffer = s_color_chunk,
            };
            ESP_ERROR_CHECK(spi_device_polling_transmit(dev, &trans));
            remaining -= batch;
        }
    }
}

/*
 * The SSD1351 controller RAM is 128x128, but the 1.27" glass only shows a
 * 96-row window whose position within RAM is offset. Filling just 0..95 leaves
 * the unmapped visible rows un-painted (they show black). To guarantee the
 * whole glass is covered, the full-screen clear paints the entire 128x128 RAM.
 */
#define SSD1351_GRAM_DIM 128

void ssd1351_fill_screen(uint16_t color)
{
    const uint8_t hi = (uint8_t)(color >> 8);
    const uint8_t lo = (uint8_t)(color & 0xFF);

    size_t prefill = CHUNK_PIXELS;
    for (size_t i = 0; i < prefill; i++) {
        s_color_chunk[i * 2] = hi;
        s_color_chunk[(i * 2) + 1] = lo;
    }

    const size_t total = (size_t)SSD1351_GRAM_DIM * (size_t)SSD1351_GRAM_DIM;

    for (int d = target_first(); d <= target_last(); d++) {
        spi_device_handle_t dev = s_spi[d];
        dev_set_window(dev, 0, 0, SSD1351_GRAM_DIM - 1, SSD1351_GRAM_DIM - 1);

        size_t remaining = total;
        while (remaining > 0) {
            size_t batch = remaining > CHUNK_PIXELS ? CHUNK_PIXELS : remaining;

            gpio_set_level(OLED_PIN_DC, 1);
            spi_transaction_t trans = {
                .length = batch * 16,
                .tx_buffer = s_color_chunk,
            };
            ESP_ERROR_CHECK(spi_device_polling_transmit(dev, &trans));
            remaining -= batch;
        }
    }
}

void ssd1351_draw_pixel(int x, int y, uint16_t color)
{
    if (x < 0 || y < 0 || x >= OLED_WIDTH || y >= OLED_HEIGHT) {
        return;
    }

    const uint8_t bytes[2] = { (uint8_t)(color >> 8), (uint8_t)(color & 0xFF) };
    for (int d = target_first(); d <= target_last(); d++) {
        dev_set_window(s_spi[d], x, y, x, y);
        dev_write_data(s_spi[d], bytes, sizeof(bytes));
    }
}

void ssd1351_draw_bitmap(int x, int y, int w, int h, const uint16_t *colors)
{
    if (colors == NULL || w <= 0 || h <= 0) {
        return;
    }

    if (x < 0 || y < 0 || x + w > OLED_WIDTH || y + h > OLED_HEIGHT) {
        return;
    }

    const size_t total = (size_t)w * (size_t)h;

    for (int d = target_first(); d <= target_last(); d++) {
        spi_device_handle_t dev = s_spi[d];
        dev_set_window(dev, x, y, x + w - 1, y + h - 1);

        size_t remaining = total;
        size_t source_offset = 0;
        while (remaining > 0) {
            size_t batch = remaining > CHUNK_PIXELS ? CHUNK_PIXELS : remaining;
            for (size_t i = 0; i < batch; i++) {
                uint16_t color = colors[source_offset + i];
                s_color_chunk[i * 2] = (uint8_t)(color >> 8);
                s_color_chunk[(i * 2) + 1] = (uint8_t)(color & 0xFF);
            }

            gpio_set_level(OLED_PIN_DC, 1);
            spi_transaction_t trans = {
                .length = batch * 16,
                .tx_buffer = s_color_chunk,
            };
            ESP_ERROR_CHECK(spi_device_polling_transmit(dev, &trans));

            source_offset += batch;
            remaining -= batch;
        }
    }
}



ssd1351.h
#pragma once

#include <stdint.h>
#include "esp_err.h"

/*
 * Waveshare 1.27" RGB OLED Module (SSD1351, 128x96, 4-wire SPI).
 * Two panels share one SPI bus; only CS differs per display.
 */
#define OLED_PIN_SCLK   23   /* shared CLK -> both displays */
#define OLED_PIN_MOSI   22   /* shared DIN -> both displays */
#define OLED_PIN_DC     21   /* shared DC  -> both displays */
#define OLED_PIN_RST    20   /* shared RST -> both displays */
#define OLED_PIN_CS0    26   /* CS for display 0 (left eye)  */
#define OLED_PIN_CS1    27   /* CS for display 1 (right eye) - any free GPIO works */

/* Number of OLED panels on the shared bus. */
#define OLED_COUNT      2

/* Draw target values for ssd1351_target(). */
#define SSD1351_TARGET_ALL   (-1)
#define SSD1351_TARGET_LEFT  0
#define SSD1351_TARGET_RIGHT 1

/* SSD1351 RAM is 128x128; the 1.27" panel shows 128x96. */
#define OLED_WIDTH      128
#define OLED_HEIGHT     96

/*
 * The Waveshare remap (0x74) uses a swapped colour sub-order (BGR).
 * Keeping this at 1 makes ssd1351_color(255,0,0) appear red on screen.
 * If red/blue look swapped on your panel, set this to 0 and rebuild.
 */
#ifndef SSD1351_SWAP_RB
#define SSD1351_SWAP_RB 0
#endif

esp_err_t ssd1351_init(void);

/*
 * Select which panel(s) subsequent draw calls target:
 *   SSD1351_TARGET_ALL   -> both eyes (mirrored, default)
 *   SSD1351_TARGET_LEFT  -> display 0 only
 *   SSD1351_TARGET_RIGHT -> display 1 only
 */
void ssd1351_target(int target);
int  ssd1351_get_target(void);

void ssd1351_fill_screen(uint16_t color);
void ssd1351_fill_rect(int x, int y, int w, int h, uint16_t color);
void ssd1351_draw_pixel(int x, int y, uint16_t color);
void ssd1351_draw_bitmap(int x, int y, int w, int h, const uint16_t *colors);

static inline uint16_t ssd1351_color(uint8_t r, uint8_t g, uint8_t b)
{
#if SSD1351_SWAP_RB
    uint8_t t = r;
    r = b;
    b = t;
#endif
    return (uint16_t)(((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3));
}


#include "nino_eye.h"

#include <ctype.h>
#include <stdint.h>
#include <string.h>
#include <math.h>
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "ssd1351.h"
#include "fire_emoji.h"
#include "smile_emoji.h"
#include "sparkle_emoji.h"
#include "pencil_emoji.h"
#include "radio_emoji.h"
#include "tv_emoji.h"
#include "bulb_emoji.h"
#include "robot_emoji.h"
#include "bigsmile_emoji.h"

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

/* 1 = cycle all states for testing. 0 = hold current state (loops forever). */
#define DEMO_CYCLE          0

/* 1 = show an orientation test (TOP bar + top-left marker) instead of eyes. */
#define NINO_ORIENT_TEST    0

typedef enum {
    NINO_RENDER_BLINK,
    NINO_RENDER_STATIC,
    NINO_RENDER_HEART,
    NINO_RENDER_MED_CAPSULE,
    NINO_RENDER_FIRE,
    NINO_RENDER_SMILE,
    NINO_RENDER_SPARKLE,
    NINO_RENDER_EMOJI,
} nino_render_mode_t;

typedef struct {
    const uint16_t *pixels;
    int w;
    int h;
} nino_emoji_bmp_t;

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
    uint8_t eye_r;       /* emotion eye colour on white background */
    uint8_t eye_g;
    uint8_t eye_b;
} nino_state_profile_t;

static volatile nino_eye_state_t s_state = NINO_EYE_IDLE;
static volatile bool s_restart_requested = false;

static const nino_state_profile_t s_profiles[NINO_EYE_STATE_COUNT] = {
    /* idle: neutral / half-open, normal pupil, slow blink (~4-7 s -> ~5 s).
     * blink_ms = 17 ≈ 60 FPS for blink frames (pair with CONFIG_FREERTOS_HZ=1000). */
    [NINO_EYE_IDLE] = {
        .mode = NINO_RENDER_BLINK,
        .rx = 24,
        .ry = 30,
        .hold_ms = 10000,
        .closed_hold_ms = 240,
        .blink_step = 3,
        .blink_ms = 17,
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
    /* mad: idle-size eye that shakes frantically - 3 s fast left<->right, then
     * 2 s fast up<->down, repeating. hold_ms = horizontal phase, state_ms =
     * vertical phase, blink_ms = per-frame delay (smaller = faster shake). */
    [NINO_EYE_MAD] = {
        .mode = NINO_RENDER_STATIC,
        .rx = 24,
        .ry = 30,
        .hold_ms = 3000,
        .state_ms = 2000,
        .blink_ms = 6,
        .eye_r = 0, .eye_g = 0, .eye_b = 0,
    },
    /* med: red/white capsule pill, slanted (45 deg from vertical), static symbol. */
    [NINO_EYE_MED] = {
        .mode = NINO_RENDER_MED_CAPSULE,
        .state_ms = 900,
        .heart_max_scale = 17,   /* half-length of capsule body (reuse field) */
        .eye_r = 255, .eye_g = 0, .eye_b = 0,
    },
    /* jai Bhalaiah: exact fire emoji bitmap, static on black background. */
    [NINO_EYE_JAI_BHALAIAH] = {
        .mode = NINO_RENDER_FIRE,
        .state_ms = 900,
        .eye_r = 255, .eye_g = 110, .eye_b = 0,
    },
    /* smile: exact WhatsApp-style 😊 bitmap, static, a bit smaller than fire. */
    [NINO_EYE_SMILE] = {
        .mode = NINO_RENDER_SMILE,
        .state_ms = 900,
        .eye_r = 255, .eye_g = 200, .eye_b = 0,
    },
    /* sparkle: WhatsApp-style ✨ bitmap, static on black background. */
    [NINO_EYE_SPARKLE] = {
        .mode = NINO_RENDER_SPARKLE,
        .state_ms = 900,
        .eye_r = 255, .eye_g = 220, .eye_b = 40,
    },
    [NINO_EYE_PENCIL] = {
        .mode = NINO_RENDER_EMOJI,
        .state_ms = 900,
        .eye_r = 255, .eye_g = 140, .eye_b = 40,
    },
    [NINO_EYE_RADIO] = {
        .mode = NINO_RENDER_EMOJI,
        .state_ms = 900,
        .eye_r = 120, .eye_g = 140, .eye_b = 160,
    },
    [NINO_EYE_TV] = {
        .mode = NINO_RENDER_EMOJI,
        .state_ms = 900,
        .eye_r = 100, .eye_g = 160, .eye_b = 220,
    },
    [NINO_EYE_BULB] = {
        .mode = NINO_RENDER_EMOJI,
        .state_ms = 900,
        .eye_r = 255, .eye_g = 220, .eye_b = 40,
    },
    [NINO_EYE_ROBOT] = {
        .mode = NINO_RENDER_EMOJI,
        .state_ms = 900,
        .eye_r = 80, .eye_g = 180, .eye_b = 220,
    },
    [NINO_EYE_BIGSMILE] = {
        .mode = NINO_RENDER_EMOJI,
        .state_ms = 900,
        .eye_r = 255, .eye_g = 200, .eye_b = 0,
    },
};

static const nino_emoji_bmp_t s_emoji_pencil   = { s_pencil_emoji,   PENCIL_EMOJI_W,   PENCIL_EMOJI_H };
static const nino_emoji_bmp_t s_emoji_radio    = { s_radio_emoji,    RADIO_EMOJI_W,    RADIO_EMOJI_H };
static const nino_emoji_bmp_t s_emoji_tv       = { s_tv_emoji,       TV_EMOJI_W,       TV_EMOJI_H };
static const nino_emoji_bmp_t s_emoji_bulb     = { s_bulb_emoji,     BULB_EMOJI_W,     BULB_EMOJI_H };
static const nino_emoji_bmp_t s_emoji_robot    = { s_robot_emoji,    ROBOT_EMOJI_W,    ROBOT_EMOJI_H };
static const nino_emoji_bmp_t s_emoji_bigsmile = { s_bigsmile_emoji, BIGSMILE_EMOJI_W, BIGSMILE_EMOJI_H };

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

static uint16_t color_red(void)
{
    return ssd1351_color(255, 0, 0);
}

static uint16_t color_capsule_body(void)
{
    return ssd1351_color(210, 210, 210);
}

static uint16_t color_capsule_outline(void)
{
    return ssd1351_color(40, 40, 40);
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
typedef enum { PREV_NONE, PREV_ELLIPSE, PREV_HEART, PREV_BLOB, PREV_CAPSULE, PREV_FIRE, PREV_SMILE, PREV_SPARKLE, PREV_EMOJI } prev_kind_t;
static prev_kind_t s_prev_kind = PREV_NONE;
static int s_prev_cx, s_prev_rx, s_prev_ry, s_prev_top, s_prev_bottom;
static int s_prev_blob_cy;
static int s_prev_heart_cx, s_prev_heart_cy, s_prev_heart_scale;
static int s_prev_cap_cx, s_prev_cap_cy, s_prev_cap_half_len, s_prev_cap_radius;
static int s_prev_fire_cx, s_prev_fire_cy;
static int s_prev_smile_cx, s_prev_smile_cy;
static int s_prev_sparkle_cx, s_prev_sparkle_cy;
static int s_prev_emoji_cx, s_prev_emoji_cy;
static const nino_emoji_bmp_t *s_prev_emoji_bmp;

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

static void remember_capsule(int cx, int cy, int half_len, int radius)
{
    s_prev_kind = PREV_CAPSULE;
    s_prev_cap_cx = cx;
    s_prev_cap_cy = cy;
    s_prev_cap_half_len = half_len;
    s_prev_cap_radius = radius;
}

static void remember_fire(int cx, int cy)
{
    s_prev_kind = PREV_FIRE;
    s_prev_fire_cx = cx;
    s_prev_fire_cy = cy;
}

static void remember_smile(int cx, int cy)
{
    s_prev_kind = PREV_SMILE;
    s_prev_smile_cx = cx;
    s_prev_smile_cy = cy;
}

static void remember_sparkle(int cx, int cy)
{
    s_prev_kind = PREV_SPARKLE;
    s_prev_sparkle_cx = cx;
    s_prev_sparkle_cy = cy;
}

static void remember_emoji(int cx, int cy, const nino_emoji_bmp_t *bmp)
{
    s_prev_kind = PREV_EMOJI;
    s_prev_emoji_cx = cx;
    s_prev_emoji_cy = cy;
    s_prev_emoji_bmp = bmp;
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

/* Slanted capsule pill (red cap on +u end, gray body with red beads on -u end).
 * half_len = half of total capsule length along axis; radius = cap radius.
 * Slant ~30 deg from vertical, red cap toward upper-right (matches reference). */
#define MED_CAPSULE_SLANT_DEG  45

static float capsule_dist_local(float u, float v, float half_len, float radius)
{
    float body = half_len - radius;
    if (body < 0.0f) {
        body = 0.0f;
    }
    if (u < -body) {
        float du = u + body;
        return sqrtf((du * du) + (v * v));
    }
    if (u > body) {
        float du = u - body;
        return sqrtf((du * du) + (v * v));
    }
    return fabsf(v);
}

static bool capsule_bead_at(float u, float v)
{
    static const struct { float u; float v; } beads[] = {
        {-7.0f,  2.0f},
        {-4.0f, -2.5f},
        {-1.0f,  3.0f},
        { 2.0f,  0.5f},
        {-5.0f,  4.0f},
    };
    for (size_t i = 0; i < sizeof(beads) / sizeof(beads[0]); i++) {
        float du = u - beads[i].u;
        float dv = v - beads[i].v;
        if ((du * du) + (dv * dv) <= 3.5f) {
            return true;
        }
    }
    return false;
}

static uint16_t capsule_pixel_color(float u, float v, float half_len, float radius, bool erase)
{
    float dist = capsule_dist_local(u, v, half_len, radius);
    if (dist > radius + 0.5f) {
        return 0;
    }
    if (erase) {
        return color_bg();
    }
    if (dist > radius - 1.1f) {
        return color_capsule_outline();
    }
    if (u > 0.0f) {
        return color_red();
    }
    if (capsule_bead_at(u, v)) {
        return color_red();
    }
    return color_capsule_body();
}

static void draw_capsule(int cx, int cy, int half_len, int radius)
{
    const float slant = (float)MED_CAPSULE_SLANT_DEG * (3.14159265f / 180.0f);
    /* Axis tilts upper-right: 30 deg from vertical. */
    const float cos_a = sinf(slant);
    const float sin_a = -cosf(slant);
    const float flen = (float)half_len;
    const float fr = (float)radius;

    int x0 = cx - half_len - radius - 4;
    int x1 = cx + half_len + radius + 4;
    int y0 = cy - half_len - radius - 4;
    int y1 = cy + half_len + radius + 4;
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
        uint16_t span_color = 0;
        for (int x = x0; x <= x1; x++) {
            float dx = (float)(x - cx);
            float dy = (float)(y - cy);
            float u = (dx * cos_a) + (dy * sin_a);
            float v = (-dx * sin_a) + (dy * cos_a);
            uint16_t pix = capsule_pixel_color(u, v, flen, fr, false);
            if (pix != 0) {
                if (span_start < 0) {
                    span_start = x;
                    span_color = pix;
                } else if (pix != span_color) {
                    draw_landscape_hline(span_start, y, x - span_start, span_color);
                    span_start = x;
                    span_color = pix;
                }
            } else if (span_start >= 0) {
                draw_landscape_hline(span_start, y, x - span_start, span_color);
                span_start = -1;
            }
        }
        if (span_start >= 0) {
            draw_landscape_hline(span_start, y, x1 - span_start + 1, span_color);
        }
    }
}

static void erase_capsule(int cx, int cy, int half_len, int radius)
{
    const float slant = (float)MED_CAPSULE_SLANT_DEG * (3.14159265f / 180.0f);
    const float cos_a = sinf(slant);
    const float sin_a = -cosf(slant);
    const float flen = (float)half_len;
    const float fr = (float)radius;

    int x0 = cx - half_len - radius - 4;
    int x1 = cx + half_len + radius + 4;
    int y0 = cy - half_len - radius - 4;
    int y1 = cy + half_len + radius + 4;
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
            float dx = (float)(x - cx);
            float dy = (float)(y - cy);
            float u = (dx * cos_a) + (dy * sin_a);
            float v = (-dx * sin_a) + (dy * cos_a);
            bool inside = capsule_dist_local(u, v, flen, fr) <= fr + 0.5f;
            if (inside && span_start < 0) {
                span_start = x;
            } else if (!inside && span_start >= 0) {
                draw_landscape_hline(span_start, y, x - span_start, color_bg());
                span_start = -1;
            }
        }
        if (span_start >= 0) {
            draw_landscape_hline(span_start, y, x1 - span_start + 1, color_bg());
        }
    }
}

/*
 * Fire emoji (jai Bhalaiah): exact bitmap from the reference 🔥 image.
 * Drawn static on the white background; black/transparent pixels are skipped.
 * (cx, cy) is the center of the bitmap.
 */
static void draw_fire(int cx, int cy)
{
    const int x0 = cx - (FIRE_EMOJI_W / 2);
    const int y0 = cy - (FIRE_EMOJI_H / 2);

    for (int row = 0; row < FIRE_EMOJI_H; row++) {
        int y = y0 + row;
        if (y < 0 || y >= LOGICAL_HEIGHT) {
            continue;
        }
        int span_start = -1;
        uint16_t span_color = 0;
        for (int col = 0; col < FIRE_EMOJI_W; col++) {
            uint16_t pix = s_fire_emoji[(row * FIRE_EMOJI_W) + col];
            int x = x0 + col;
            if (pix != 0 && x >= 0 && x < LOGICAL_WIDTH) {
                if (span_start < 0) {
                    span_start = x;
                    span_color = pix;
                } else if (pix != span_color) {
                    draw_landscape_hline(span_start, y, x - span_start, span_color);
                    span_start = x;
                    span_color = pix;
                }
            } else if (span_start >= 0) {
                draw_landscape_hline(span_start, y, x - span_start, span_color);
                span_start = -1;
            }
        }
        if (span_start >= 0) {
            int end = x0 + FIRE_EMOJI_W;
            if (end > LOGICAL_WIDTH) {
                end = LOGICAL_WIDTH;
            }
            draw_landscape_hline(span_start, y, end - span_start, span_color);
        }
    }
}

static void erase_fire(int cx, int cy)
{
    const int x0 = cx - (FIRE_EMOJI_W / 2);
    const int y0 = cy - (FIRE_EMOJI_H / 2);

    for (int row = 0; row < FIRE_EMOJI_H; row++) {
        int y = y0 + row;
        if (y < 0 || y >= LOGICAL_HEIGHT) {
            continue;
        }
        int span_start = -1;
        for (int col = 0; col < FIRE_EMOJI_W; col++) {
            uint16_t pix = s_fire_emoji[(row * FIRE_EMOJI_W) + col];
            int x = x0 + col;
            if (pix != 0 && x >= 0 && x < LOGICAL_WIDTH) {
                if (span_start < 0) {
                    span_start = x;
                }
            } else if (span_start >= 0) {
                draw_landscape_hline(span_start, y, x - span_start, color_bg());
                span_start = -1;
            }
        }
        if (span_start >= 0) {
            int end = x0 + FIRE_EMOJI_W;
            if (end > LOGICAL_WIDTH) {
                end = LOGICAL_WIDTH;
            }
            draw_landscape_hline(span_start, y, end - span_start, color_bg());
        }
    }
}

/*
 * Smile emoji: exact WhatsApp-style 😊 bitmap (smaller than fire).
 * Drawn static on the white background; transparent (0) pixels are skipped.
 */
static void draw_smile(int cx, int cy)
{
    const int x0 = cx - (SMILE_EMOJI_W / 2);
    const int y0 = cy - (SMILE_EMOJI_H / 2);

    for (int row = 0; row < SMILE_EMOJI_H; row++) {
        int y = y0 + row;
        if (y < 0 || y >= LOGICAL_HEIGHT) {
            continue;
        }
        int span_start = -1;
        uint16_t span_color = 0;
        for (int col = 0; col < SMILE_EMOJI_W; col++) {
            uint16_t pix = s_smile_emoji[(row * SMILE_EMOJI_W) + col];
            int x = x0 + col;
            if (pix != 0 && x >= 0 && x < LOGICAL_WIDTH) {
                if (span_start < 0) {
                    span_start = x;
                    span_color = pix;
                } else if (pix != span_color) {
                    draw_landscape_hline(span_start, y, x - span_start, span_color);
                    span_start = x;
                    span_color = pix;
                }
            } else if (span_start >= 0) {
                draw_landscape_hline(span_start, y, x - span_start, span_color);
                span_start = -1;
            }
        }
        if (span_start >= 0) {
            int end = x0 + SMILE_EMOJI_W;
            if (end > LOGICAL_WIDTH) {
                end = LOGICAL_WIDTH;
            }
            draw_landscape_hline(span_start, y, end - span_start, span_color);
        }
    }
}

static void erase_smile(int cx, int cy)
{
    const int x0 = cx - (SMILE_EMOJI_W / 2);
    const int y0 = cy - (SMILE_EMOJI_H / 2);

    for (int row = 0; row < SMILE_EMOJI_H; row++) {
        int y = y0 + row;
        if (y < 0 || y >= LOGICAL_HEIGHT) {
            continue;
        }
        int span_start = -1;
        for (int col = 0; col < SMILE_EMOJI_W; col++) {
            uint16_t pix = s_smile_emoji[(row * SMILE_EMOJI_W) + col];
            int x = x0 + col;
            if (pix != 0 && x >= 0 && x < LOGICAL_WIDTH) {
                if (span_start < 0) {
                    span_start = x;
                }
            } else if (span_start >= 0) {
                draw_landscape_hline(span_start, y, x - span_start, color_bg());
                span_start = -1;
            }
        }
        if (span_start >= 0) {
            int end = x0 + SMILE_EMOJI_W;
            if (end > LOGICAL_WIDTH) {
                end = LOGICAL_WIDTH;
            }
            draw_landscape_hline(span_start, y, end - span_start, color_bg());
        }
    }
}

/*
 * Sparkle emoji: WhatsApp-style ✨ bitmap.
 * Drawn static on the white background; transparent (0) pixels are skipped.
 */
static void draw_sparkle(int cx, int cy)
{
    const int x0 = cx - (SPARKLE_EMOJI_W / 2);
    const int y0 = cy - (SPARKLE_EMOJI_H / 2);

    for (int row = 0; row < SPARKLE_EMOJI_H; row++) {
        int y = y0 + row;
        if (y < 0 || y >= LOGICAL_HEIGHT) {
            continue;
        }
        int span_start = -1;
        uint16_t span_color = 0;
        for (int col = 0; col < SPARKLE_EMOJI_W; col++) {
            uint16_t pix = s_sparkle_emoji[(row * SPARKLE_EMOJI_W) + col];
            int x = x0 + col;
            if (pix != 0 && x >= 0 && x < LOGICAL_WIDTH) {
                if (span_start < 0) {
                    span_start = x;
                    span_color = pix;
                } else if (pix != span_color) {
                    draw_landscape_hline(span_start, y, x - span_start, span_color);
                    span_start = x;
                    span_color = pix;
                }
            } else if (span_start >= 0) {
                draw_landscape_hline(span_start, y, x - span_start, span_color);
                span_start = -1;
            }
        }
        if (span_start >= 0) {
            int end = x0 + SPARKLE_EMOJI_W;
            if (end > LOGICAL_WIDTH) {
                end = LOGICAL_WIDTH;
            }
            draw_landscape_hline(span_start, y, end - span_start, span_color);
        }
    }
}

static void erase_sparkle(int cx, int cy)
{
    const int x0 = cx - (SPARKLE_EMOJI_W / 2);
    const int y0 = cy - (SPARKLE_EMOJI_H / 2);

    for (int row = 0; row < SPARKLE_EMOJI_H; row++) {
        int y = y0 + row;
        if (y < 0 || y >= LOGICAL_HEIGHT) {
            continue;
        }
        int span_start = -1;
        for (int col = 0; col < SPARKLE_EMOJI_W; col++) {
            uint16_t pix = s_sparkle_emoji[(row * SPARKLE_EMOJI_W) + col];
            int x = x0 + col;
            if (pix != 0 && x >= 0 && x < LOGICAL_WIDTH) {
                if (span_start < 0) {
                    span_start = x;
                }
            } else if (span_start >= 0) {
                draw_landscape_hline(span_start, y, x - span_start, color_bg());
                span_start = -1;
            }
        }
        if (span_start >= 0) {
            int end = x0 + SPARKLE_EMOJI_W;
            if (end > LOGICAL_WIDTH) {
                end = LOGICAL_WIDTH;
            }
            draw_landscape_hline(span_start, y, end - span_start, color_bg());
        }
    }
}

/* Generic RGB565 emoji bitmap (0 = transparent), same path as smile/sparkle. */
static void draw_emoji_bmp(int cx, int cy, const nino_emoji_bmp_t *bmp)
{
    const int x0 = cx - (bmp->w / 2);
    const int y0 = cy - (bmp->h / 2);

    for (int row = 0; row < bmp->h; row++) {
        int y = y0 + row;
        if (y < 0 || y >= LOGICAL_HEIGHT) {
            continue;
        }
        int span_start = -1;
        uint16_t span_color = 0;
        for (int col = 0; col < bmp->w; col++) {
            uint16_t pix = bmp->pixels[(row * bmp->w) + col];
            int x = x0 + col;
            if (pix != 0 && x >= 0 && x < LOGICAL_WIDTH) {
                if (span_start < 0) {
                    span_start = x;
                    span_color = pix;
                } else if (pix != span_color) {
                    draw_landscape_hline(span_start, y, x - span_start, span_color);
                    span_start = x;
                    span_color = pix;
                }
            } else if (span_start >= 0) {
                draw_landscape_hline(span_start, y, x - span_start, span_color);
                span_start = -1;
            }
        }
        if (span_start >= 0) {
            int end = x0 + bmp->w;
            if (end > LOGICAL_WIDTH) {
                end = LOGICAL_WIDTH;
            }
            draw_landscape_hline(span_start, y, end - span_start, span_color);
        }
    }
}

static void erase_emoji_bmp(int cx, int cy, const nino_emoji_bmp_t *bmp)
{
    const int x0 = cx - (bmp->w / 2);
    const int y0 = cy - (bmp->h / 2);

    for (int row = 0; row < bmp->h; row++) {
        int y = y0 + row;
        if (y < 0 || y >= LOGICAL_HEIGHT) {
            continue;
        }
        int span_start = -1;
        for (int col = 0; col < bmp->w; col++) {
            uint16_t pix = bmp->pixels[(row * bmp->w) + col];
            int x = x0 + col;
            if (pix != 0 && x >= 0 && x < LOGICAL_WIDTH) {
                if (span_start < 0) {
                    span_start = x;
                }
            } else if (span_start >= 0) {
                draw_landscape_hline(span_start, y, x - span_start, color_bg());
                span_start = -1;
            }
        }
        if (span_start >= 0) {
            int end = x0 + bmp->w;
            if (end > LOGICAL_WIDTH) {
                end = LOGICAL_WIDTH;
            }
            draw_landscape_hline(span_start, y, end - span_start, color_bg());
        }
    }
}

/*
 * Clear the previous glyph. Shape-only white redraw leaves a brighter OLED
 * "window" oval (fresh white vs untouched white). A full-screen bg clear keeps
 * the panel uniform white with no leftover oval plate.
 */
static void erase_prev_eye(void)
{
    clear_screen(color_bg());
    s_prev_kind = PREV_NONE;
    s_prev_emoji_bmp = NULL;
}

static nino_eye_state_t current_state(void)
{
    return (nino_eye_state_t)s_state;
}

static bool delay_ms_interruptible(int total_ms, nino_eye_state_t expected)
{
    int elapsed = 0;
    while (elapsed < total_ms) {
        if (s_restart_requested) {
            s_restart_requested = false;
            return false;
        }
        if (current_state() != expected) {
            return false;
        }

        int slice_ms = total_ms - elapsed;
        if (slice_ms > 25) {
            slice_ms = 25;
        }
        /* Always wait at least 1 tick. With CONFIG_FREERTOS_HZ=100 (10 ms/tick),
         * pdMS_TO_TICKS() of a small value rounds to 0, so vTaskDelay(0) would
         * NOT yield - a fast loop (e.g. mad's 6 ms frames) would then starve the
         * idle task and trip the task watchdog. */
        TickType_t ticks = pdMS_TO_TICKS(slice_ms);
        if (ticks == 0) {
            ticks = 1;
        }
        vTaskDelay(ticks);
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

/*
 * Move an already-drawn eye blob from (ox,oy) to (nx,ny): draw the new ellipse,
 * then erase only the old pixels NOT covered by the new one. The eye never fully
 * disappears (no flash) and only changed pixels are touched. Works for any
 * horizontal/vertical/diagonal move.
 */
static void move_eye_blob(int ox, int oy, int nx, int ny, int rx, int ry)
{
    int ytop = ((oy < ny) ? oy : ny) - ry;
    int ybot = ((oy > ny) ? oy : ny) + ry;
    for (int y = ytop; y <= ybot; y++) {
        int nw = ellipse_half_width(rx, ry, y - ny);
        int ow = ellipse_half_width(rx, ry, y - oy);
        if (nw >= 0) {
            draw_landscape_hline(nx - nw, y, (nw * 2) + 1, color_eye());
        }
        if (ow < 0) {
            continue;
        }
        int oxl = ox - ow;
        int oxr = ox + ow;
        if (nw < 0) {
            draw_landscape_hline(oxl, y, (oxr - oxl) + 1, color_bg());
            continue;
        }
        int nxl = nx - nw;
        int nxr = nx + nw;
        if (oxl < nxl) {
            int r = (oxr < nxl - 1) ? oxr : (nxl - 1);
            draw_landscape_hline(oxl, y, (r - oxl) + 1, color_bg());
        }
        if (oxr > nxr) {
            int l = (oxl > nxr + 1) ? oxl : (nxr + 1);
            draw_landscape_hline(l, y, (oxr - l) + 1, color_bg());
        }
    }
}

/*
 * Mad: an idle-size eye that shakes frantically. Phase 1 darts left<->right for
 * hold_ms, phase 2 darts up<->down for state_ms, then repeats. blink_ms is the
 * per-frame delay (smaller = faster). Only the moving eye is redrawn each frame.
 */
static void run_mad_eye(const nino_state_profile_t *profile, nino_eye_state_t expected)
{
    const int rx = profile->rx;
    const int ry = profile->ry;
    const int frame_ms = profile->blink_ms > 0 ? profile->blink_ms : 6;
    const int h_amp = 18;   /* horizontal shake amplitude (px from center) */
    const int v_amp = 12;   /* vertical shake amplitude */
    const int h_step = 9;   /* px moved per frame -> very fast dart */
    const int v_step = 8;

    erase_prev_eye();
    int cx = EYE_CX;
    int cy = EYE_CY;
    fill_ellipse(cx, cy, rx, ry, color_eye());
    remember_blob(cx, cy, rx, ry);

    while (current_state() == expected) {
        /* Phase 1: fast horizontal shake. */
        int elapsed = 0;
        int target = EYE_CX + h_amp;
        while (elapsed < profile->hold_ms) {
            if (current_state() != expected) {
                return;
            }
            int ncx = cx;
            if (cx < target) {
                ncx = (cx + h_step > target) ? target : cx + h_step;
            } else if (cx > target) {
                ncx = (cx - h_step < target) ? target : cx - h_step;
            }
            if (ncx != cx) {
                move_eye_blob(cx, cy, ncx, cy, rx, ry);
                cx = ncx;
                remember_blob(cx, cy, rx, ry);
            }
            if (cx == target) {
                target = (target > EYE_CX) ? (EYE_CX - h_amp) : (EYE_CX + h_amp);
            }
            if (!delay_ms_interruptible(frame_ms, expected)) {
                return;
            }
            elapsed += frame_ms;
        }
        if (cx != EYE_CX) {
            move_eye_blob(cx, cy, EYE_CX, cy, rx, ry);
            cx = EYE_CX;
            remember_blob(cx, cy, rx, ry);
        }

        /* Phase 2: fast vertical shake. */
        elapsed = 0;
        int vtarget = EYE_CY + v_amp;
        while (elapsed < profile->state_ms) {
            if (current_state() != expected) {
                return;
            }
            int ncy = cy;
            if (cy < vtarget) {
                ncy = (cy + v_step > vtarget) ? vtarget : cy + v_step;
            } else if (cy > vtarget) {
                ncy = (cy - v_step < vtarget) ? vtarget : cy - v_step;
            }
            if (ncy != cy) {
                move_eye_blob(cx, cy, cx, ncy, rx, ry);
                cy = ncy;
                remember_blob(cx, cy, rx, ry);
            }
            if (cy == vtarget) {
                vtarget = (vtarget > EYE_CY) ? (EYE_CY - v_amp) : (EYE_CY + v_amp);
            }
            if (!delay_ms_interruptible(frame_ms, expected)) {
                return;
            }
            elapsed += frame_ms;
        }
        if (cy != EYE_CY) {
            move_eye_blob(cx, cy, cx, EYE_CY, rx, ry);
            cy = EYE_CY;
            remember_blob(cx, cy, rx, ry);
        }
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

static void run_med_profile_once(const nino_state_profile_t *profile, nino_eye_state_t expected)
{
    const int half_len = profile->heart_max_scale > 0 ? profile->heart_max_scale : 17;
    const int radius = (half_len * 5) / 8;
    if (radius < 6) {
        return;
    }

    erase_prev_eye();
    draw_capsule(EYE_CX, EYE_CY, half_len, radius);
    remember_capsule(EYE_CX, EYE_CY, half_len, radius);
    while (delay_ms_interruptible(profile->state_ms, expected)) {
        /* Static slanted capsule, like happy. */
    }
}

static void run_jai_bhalaiah_profile_once(const nino_state_profile_t *profile, nino_eye_state_t expected)
{
    const int cx = EYE_CX;
    const int cy = EYE_CY + 2;

    erase_prev_eye();
    draw_fire(cx, cy);
    remember_fire(cx, cy);
    while (delay_ms_interruptible(profile->state_ms, expected)) {
        /* Static fire emoji — exact bitmap from the reference image. */
    }
}

static void run_smile_profile_once(const nino_state_profile_t *profile, nino_eye_state_t expected)
{
    const int cx = EYE_CX;
    const int cy = EYE_CY;

    erase_prev_eye();
    draw_smile(cx, cy);
    remember_smile(cx, cy);
    while (delay_ms_interruptible(profile->state_ms, expected)) {
        /* Static WhatsApp-style smile emoji. */
    }
}

static void run_sparkle_profile_once(const nino_state_profile_t *profile, nino_eye_state_t expected)
{
    const int cx = EYE_CX;
    const int cy = EYE_CY;

    erase_prev_eye();
    draw_sparkle(cx, cy);
    remember_sparkle(cx, cy);
    while (delay_ms_interruptible(profile->state_ms, expected)) {
        /* Static WhatsApp-style sparkle emoji. */
    }
}

static void run_emoji_bmp_profile_once(const nino_state_profile_t *profile, nino_eye_state_t expected,
                                       const nino_emoji_bmp_t *bmp)
{
    const int cx = EYE_CX;
    const int cy = EYE_CY;

    erase_prev_eye();
    draw_emoji_bmp(cx, cy, bmp);
    remember_emoji(cx, cy, bmp);
    while (delay_ms_interruptible(profile->state_ms, expected)) {
        /* Static emoji bitmap. */
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

void nino_eye_idle(void)      { nino_eye_set_state(NINO_EYE_IDLE); }
void nino_eye_happy(void)     { nino_eye_set_state(NINO_EYE_HAPPY); }
void nino_eye_tired(void)     { nino_eye_set_state(NINO_EYE_TIRED); }
void nino_eye_thinking(void)  { nino_eye_set_state(NINO_EYE_THINKING); }
void nino_eye_curious(void)   { nino_eye_set_state(NINO_EYE_CURIOUS_QUIZ); }
void nino_eye_sad(void)       { nino_eye_set_state(NINO_EYE_SAD); }
void nino_eye_surprised(void) { nino_eye_set_state(NINO_EYE_SURPRISED); }
void nino_eye_listening(void) { nino_eye_set_state(NINO_EYE_LISTENING); }
void nino_eye_recalling(void) { nino_eye_set_state(NINO_EYE_RECALLING); }
void nino_eye_mad(void)       { nino_eye_set_state(NINO_EYE_MAD); }
void nino_eye_med(void)            { nino_eye_set_state(NINO_EYE_MED); }
void nino_eye_jai_bhalaiah(void)   { nino_eye_set_state(NINO_EYE_JAI_BHALAIAH); }
void nino_eye_smile(void)          { nino_eye_set_state(NINO_EYE_SMILE); }
void nino_eye_sparkle(void)        { nino_eye_set_state(NINO_EYE_SPARKLE); }
void nino_eye_twinkle(void)        { nino_eye_set_state(NINO_EYE_SPARKLE); }
void nino_eye_pencil(void)         { nino_eye_set_state(NINO_EYE_PENCIL); }
void nino_eye_radio(void)          { nino_eye_set_state(NINO_EYE_RADIO); }
void nino_eye_tv(void)             { nino_eye_set_state(NINO_EYE_TV); }
void nino_eye_bulb(void)           { nino_eye_set_state(NINO_EYE_BULB); }
void nino_eye_robot(void)          { nino_eye_set_state(NINO_EYE_ROBOT); }
void nino_eye_bigsmile(void)       { nino_eye_set_state(NINO_EYE_BIGSMILE); }

nino_eye_state_t nino_eye_state_from_name(const char *name)
{
    if (name == NULL || name[0] == '\0') {
        return NINO_EYE_STATE_COUNT;
    }

    /* Normalize like apply_command: lowercase + collapse spaces. */
    char token[40];
    size_t len = 0;
    bool last_space = false;
    for (size_t i = 0; name[i] != '\0' && len < sizeof(token) - 1; i++) {
        unsigned char c = (unsigned char)name[i];
        if (isspace(c)) {
            if (len > 0 && !last_space) {
                token[len++] = ' ';
                last_space = true;
            }
        } else {
            token[len++] = (char)tolower(c);
            last_space = false;
        }
    }
    while (len > 0 && token[len - 1] == ' ') {
        len--;
    }
    token[len] = '\0';

    if (strcmp(token, "idle") == 0) {
        return NINO_EYE_IDLE;
    } else if (strcmp(token, "happy") == 0) {
        return NINO_EYE_HAPPY;
    } else if (strcmp(token, "tired") == 0) {
        return NINO_EYE_TIRED;
    } else if (strcmp(token, "thinking") == 0) {
        return NINO_EYE_THINKING;
    } else if (strcmp(token, "curious") == 0 || strcmp(token, "quiz") == 0) {
        return NINO_EYE_CURIOUS_QUIZ;
    } else if (strcmp(token, "sad") == 0) {
        return NINO_EYE_SAD;
    } else if (strcmp(token, "surprised") == 0) {
        return NINO_EYE_SURPRISED;
    } else if (strcmp(token, "listening") == 0) {
        return NINO_EYE_LISTENING;
    } else if (strcmp(token, "recalling") == 0) {
        return NINO_EYE_RECALLING;
    } else if (strcmp(token, "mad") == 0) {
        return NINO_EYE_MAD;
    } else if (strcmp(token, "med") == 0) {
        return NINO_EYE_MED;
    } else if (strcmp(token, "jai bhalaiah") == 0 ||
               strcmp(token, "jai_bhalaiah") == 0 ||
               strcmp(token, "jaibhalaiah") == 0 ||
               strcmp(token, "fire") == 0) {
        return NINO_EYE_JAI_BHALAIAH;
    } else if (strcmp(token, "smile") == 0 || strcmp(token, "smiling") == 0) {
        return NINO_EYE_SMILE;
    } else if (strcmp(token, "sparkle") == 0 || strcmp(token, "sparkles") == 0 ||
               strcmp(token, "twinkle") == 0) {
        return NINO_EYE_SPARKLE;
    } else if (strcmp(token, "pencil") == 0) {
        return NINO_EYE_PENCIL;
    } else if (strcmp(token, "radio") == 0) {
        return NINO_EYE_RADIO;
    } else if (strcmp(token, "tv") == 0 || strcmp(token, "television") == 0) {
        return NINO_EYE_TV;
    } else if (strcmp(token, "bulb") == 0 || strcmp(token, "light") == 0) {
        return NINO_EYE_BULB;
    } else if (strcmp(token, "robot") == 0 || strcmp(token, "bot") == 0) {
        return NINO_EYE_ROBOT;
    } else if (strcmp(token, "bigsmile") == 0 || strcmp(token, "big smile") == 0) {
        return NINO_EYE_BIGSMILE;
    }
    return NINO_EYE_STATE_COUNT;
}

void nino_eye_apply_expression(const char *name)
{
    nino_eye_state_t state = nino_eye_state_from_name(name);
    if (state >= NINO_EYE_STATE_COUNT) {
        nino_eye_set_state(NINO_EYE_IDLE);
        return;
    }
    nino_eye_set_state(state);
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

    /* Normalize the whole line to lowercase, collapse spaces, so both
     * "jai Bhalaiah" and "jai_bhalaiah" work. */
    char token[40];
    size_t len = 0;
    bool last_space = false;
    for (size_t i = 0; line[i] != '\0' && len < sizeof(token) - 1; i++) {
        unsigned char c = (unsigned char)line[i];
        if (isspace(c)) {
            if (len > 0 && !last_space) {
                token[len++] = ' ';
                last_space = true;
            }
        } else {
            token[len++] = (char)tolower(c);
            last_space = false;
        }
    }
    while (len > 0 && token[len - 1] == ' ') {
        len--;
    }
    token[len] = '\0';

    /* Single digit 0-9 only covers the first ten states; prefer names. */
    if (token[0] >= '0' && token[0] <= '9' && token[1] == '\0') {
        int value = token[0] - '0';
        if (value >= NINO_EYE_STATE_COUNT) {
            return false;
        }
        nino_eye_set_state((nino_eye_state_t)value);
        return true;
    }

    nino_eye_state_t state = nino_eye_state_from_name(token);
    if (state >= NINO_EYE_STATE_COUNT) {
        return false;
    }
    nino_eye_set_state(state);
    return true;
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
    case NINO_EYE_MED:
        run_med_profile_once(profile, state);
        break;
    case NINO_EYE_JAI_BHALAIAH:
        run_jai_bhalaiah_profile_once(profile, state);
        break;
    case NINO_EYE_SMILE:
        run_smile_profile_once(profile, state);
        break;
    case NINO_EYE_SPARKLE:
        run_sparkle_profile_once(profile, state);
        break;
    case NINO_EYE_PENCIL:
        run_emoji_bmp_profile_once(profile, state, &s_emoji_pencil);
        break;
    case NINO_EYE_RADIO:
        run_emoji_bmp_profile_once(profile, state, &s_emoji_radio);
        break;
    case NINO_EYE_TV:
        run_emoji_bmp_profile_once(profile, state, &s_emoji_tv);
        break;
    case NINO_EYE_BULB:
        run_emoji_bmp_profile_once(profile, state, &s_emoji_bulb);
        break;
    case NINO_EYE_ROBOT:
        run_emoji_bmp_profile_once(profile, state, &s_emoji_robot);
        break;
    case NINO_EYE_BIGSMILE:
        run_emoji_bmp_profile_once(profile, state, &s_emoji_bigsmile);
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
    case NINO_EYE_MAD:
        run_mad_eye(profile, state);
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
    ESP_LOGI(TAG, "default state %d (drive via nino_eye_<emotion>() / apply_expression)", (int)s_state);

    while (true) {
        run_current_state_once();
    }
#endif
}

void nino_eye_begin(void)
{
    static bool s_engine_started = false;
    if (s_engine_started) {
        return;
    }
    s_engine_started = true;
    ESP_LOGI(TAG, "Nino eye starting (engine only)");
    xTaskCreate(eye_engine_task, "nino_eye", 8192, NULL, 5, NULL);
}

void nino_eye_restart_current(void)
{
    s_restart_requested = true;
    ESP_LOGI(TAG, "eye animation restart requested");
}

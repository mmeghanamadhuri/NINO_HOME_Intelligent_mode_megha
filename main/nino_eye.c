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
/* Legacy draw-time trim (0 = off); prefer NINO_VOFFSET for vertical centering. */
#define NINO_VSHIFT     0

/* Only this central region may be drawn/erased (eye, heart, blink). The rest of
 * the screen is never touched after boot — matches "only the oval changes". */
#define EYE_CLIP_HALF_W   46
#define EYE_CLIP_Y0       (EYE_CY - 52)
#define EYE_CLIP_Y1       (EYE_CY + 44)

/* Quick eyelid blink: per-frame delay for the close/open sweep. */
#define BLINK_FRAME_MS      24
#define BLINK_CLOSE_STEP    6
#define MAX_GAZE_POINTS 5

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
    uint8_t eye_r;       /* emotion eye colour on white background */
    uint8_t eye_g;
    uint8_t eye_b;
} nino_state_profile_t;

static volatile nino_eye_state_t s_state = NINO_EYE_IDLE;

static const nino_state_profile_t s_profiles[NINO_EYE_STATE_COUNT] = {
    /* idle: neutral / half-open, normal pupil, slow blink (~5 s). */
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
    /* happy: single red heart symbol (no eyelid/pupil/blink). */
    [NINO_EYE_HAPPY] = {
        .mode = NINO_RENDER_HEART,
        .state_ms = 900,
        .heart_min_scale = 20,
        .heart_max_scale = 20,
        .heart_frame_ms = 900,
        .eye_r = 255, .eye_g = 40, .eye_b = 70,
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
    /* thinking: a normal solid eye like idle that slowly rolls around the top. No blink. */
    [NINO_EYE_THINKING] = {
        .mode = NINO_RENDER_STATIC,
        .rx = 24,
        .ry = 30,
        .state_ms = 4000,
        .eye_r = 0, .eye_g = 0, .eye_b = 0,
    },
    /* curious: wide enlarged eye that tilts up + to a side and holds, then
     * blinks across to the other side and holds again. */
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
    /* surprised: widest/tallest eye; one fast snap-open on entry, then hold wide. */
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
    /* listening: same wide enlarged eye as curious, but centered - blinks in place. */
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
    /* recalling: normal soft eye, slow upward memory-gaze path; slow blink between points. */
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
 * it touches; the engine erases ONLY that area to background instead of
 * repainting the whole screen, keeping the white background static.
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

static void clear_screen(uint16_t color)
{
    ssd1351_fill_screen(color);
    dirty_reset();
}

/*
 * Remember the EXACT shape currently painted so the next state can un-draw it
 * along its own outline (writing background over only the eye/heart pixels)
 * instead of erasing a white rectangle that would flash the held background.
 */
typedef enum { PREV_NONE, PREV_ELLIPSE, PREV_HEART, PREV_BLOB } prev_kind_t;
static prev_kind_t s_prev_kind = PREV_NONE;
static int s_prev_cx, s_prev_rx, s_prev_ry, s_prev_top, s_prev_bottom;
static int s_prev_blob_cy;
static int s_prev_heart_cx, s_prev_heart_cy, s_prev_heart_scale;

/*
 * When true, the central blink transition has already drawn (and remembered)
 * the new state's initial shape, so the per-state renderer must skip its own
 * instant initial draw and go straight to its steady loop.
 */
static bool s_skip_initial = false;

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

/* Blob = a solid eye drawn at an arbitrary center (cx, cy). */
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

/* Draw rows [top, bottom] of an ellipse centered at an arbitrary (cx, cy). */
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
        int x;
        for (x = x0; x <= x1; x++) {
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
 * colour using the SAME geometry, so only the eye/heart pixels are flipped back.
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

/* ---- Smooth state transitions ----
 *
 * Every expression change is wrapped in a uniform blink: the shape currently on
 * screen blink-closes, the eye holds shut for a beat, then the new state's
 * initial shape blink-opens. This replaces the old hard cut (instant erase +
 * instant redraw) with a natural, consistent transition for all 9 states.
 */
#define TRANS_STEP        4   /* rows revealed/hidden per frame */
#define TRANS_FRAME_MS    14  /* per-frame delay during a sweep */
#define TRANS_CLOSED_MS   110 /* eyes-shut dwell between close and open */
#define TRANS_HEART_STEP  3   /* heart scale change per frame */

typedef struct {
    prev_kind_t kind;
    int cx, cy;
    int rx, ry;
    int top, bottom;
    int heart_scale;
} eye_shape_t;

/* Paint the whole drawable clip window back to background. Safe because the
 * clip region is solid white except for the eye, so this never flashes. */
static void clear_eye_region(void)
{
    int x0 = EYE_CX - EYE_CLIP_HALF_W;
    int w = (EYE_CLIP_HALF_W * 2) + 1;
    int y0 = EYE_CLIP_Y0;
    int y1 = EYE_CLIP_Y1;
    if (x0 < 0) {
        w += x0;
        x0 = 0;
    }
    if (x0 + w > LOGICAL_WIDTH) {
        w = LOGICAL_WIDTH - x0;
    }
    if (y0 < 0) {
        y0 = 0;
    }
    if (y1 >= LOGICAL_HEIGHT) {
        y1 = LOGICAL_HEIGHT - 1;
    }
    if (w > 0 && y1 >= y0) {
        ssd1351_fill_rect(x0, y0, w, y1 - y0 + 1, color_bg());
    }
    dirty_reset();
    s_prev_kind = PREV_NONE;
}

/* Blink-close whatever shape is currently displayed, then wipe the region clean.
 * Returns false if the target state changed mid-transition (caller should bail). */
static bool transition_close(nino_eye_state_t target)
{
    switch (s_prev_kind) {
    case PREV_ELLIPSE: {
        const int cx = s_prev_cx, rx = s_prev_rx, ry = s_prev_ry;
        const int top = s_prev_top, bottom = s_prev_bottom;
        const int cy = (top + bottom) / 2;
        int cur_top = top, cur_bot = bottom;
        for (int off = TRANS_STEP;; off += TRANS_STEP) {
            if (current_state() != target) {
                return false;
            }
            int nt = top + off;
            int nb = bottom - off;
            if (nt > cy) nt = cy;
            if (nb < cy) nb = cy;
            erase_eye_rows(cx, rx, ry, cur_top, nt - 1);
            erase_eye_rows(cx, rx, ry, nb + 1, cur_bot);
            cur_top = nt;
            cur_bot = nb;
            if (!delay_ms_interruptible(TRANS_FRAME_MS, target)) {
                return false;
            }
            if (nt >= cy && nb <= cy) {
                break;
            }
        }
        break;
    }
    case PREV_BLOB: {
        const int cx = s_prev_cx, cy = s_prev_blob_cy, rx = s_prev_rx, ry = s_prev_ry;
        int cur = ry;
        for (int off = ry - TRANS_STEP;; off -= TRANS_STEP) {
            if (current_state() != target) {
                return false;
            }
            if (off < 0) off = 0;
            draw_blob_rows(cx, cy, rx, ry, cy - cur, cy - off - 1, color_bg());
            draw_blob_rows(cx, cy, rx, ry, cy + off + 1, cy + cur, color_bg());
            cur = off;
            if (!delay_ms_interruptible(TRANS_FRAME_MS, target)) {
                return false;
            }
            if (off <= 0) {
                break;
            }
        }
        break;
    }
    case PREV_HEART: {
        const int cx = s_prev_heart_cx, cy = s_prev_heart_cy;
        for (int s = s_prev_heart_scale; s > 0; s -= TRANS_HEART_STEP) {
            if (current_state() != target) {
                return false;
            }
            draw_heart(cx, cy, s, color_bg());
            int ns = s - TRANS_HEART_STEP;
            if (ns > 0) {
                draw_heart(cx, cy, ns, color_red());
            }
            if (!delay_ms_interruptible(TRANS_FRAME_MS, target)) {
                return false;
            }
        }
        break;
    }
    case PREV_NONE:
    default:
        break;
    }

    clear_eye_region();
    return current_state() == target;
}

/* Blink-open the new state's initial shape from a closed eye, then remember it
 * so the per-state renderer can continue its steady loop from here. */
static bool transition_open(nino_eye_state_t target, const eye_shape_t *sh)
{
    if (sh->kind == PREV_HEART) {
        const int cx = sh->cx, cy = sh->cy, full = sh->heart_scale;
        int prev = 0;
        for (int s = TRANS_HEART_STEP;; s += TRANS_HEART_STEP) {
            if (current_state() != target) {
                return false;
            }
            int cur = s > full ? full : s;
            if (prev > 0) {
                draw_heart(cx, cy, prev, color_bg());
            }
            draw_heart(cx, cy, cur, color_red());
            prev = cur;
            if (!delay_ms_interruptible(TRANS_FRAME_MS, target)) {
                return false;
            }
            if (cur >= full) {
                break;
            }
        }
        remember_heart(cx, cy, full);
        return current_state() == target;
    }

    if (sh->kind == PREV_BLOB) {
        const int cx = sh->cx, cy = sh->cy, rx = sh->rx, ry = sh->ry;
        int prev = 0;
        draw_blob_rows(cx, cy, rx, ry, cy, cy, color_eye());
        for (int off = TRANS_STEP;; off += TRANS_STEP) {
            if (current_state() != target) {
                return false;
            }
            if (off > ry) off = ry;
            draw_blob_rows(cx, cy, rx, ry, cy - off, cy - prev - 1, color_eye());
            draw_blob_rows(cx, cy, rx, ry, cy + prev + 1, cy + off, color_eye());
            prev = off;
            if (!delay_ms_interruptible(TRANS_FRAME_MS, target)) {
                return false;
            }
            if (off >= ry) {
                break;
            }
        }
        remember_blob(cx, cy, rx, ry);
        return current_state() == target;
    }

    /* Ellipse (full or lidded), horizontally centered at sh->cx, vertically at EYE_CY. */
    const int cx = sh->cx, rx = sh->rx, ry = sh->ry;
    const int top = sh->top, bottom = sh->bottom;
    const int cy = (top + bottom) / 2;
    int cur_top = cy, cur_bot = cy;
    draw_eye_rows(cx, rx, ry, cy, cy, color_eye());
    for (int off = TRANS_STEP;; off += TRANS_STEP) {
        if (current_state() != target) {
            return false;
        }
        int nt = cy - off;
        int nb = cy + off;
        if (nt < top) nt = top;
        if (nb > bottom) nb = bottom;
        draw_eye_rows(cx, rx, ry, nt, cur_top - 1, color_eye());
        draw_eye_rows(cx, rx, ry, cur_bot + 1, nb, color_eye());
        cur_top = nt;
        cur_bot = nb;
        if (!delay_ms_interruptible(TRANS_FRAME_MS, target)) {
            return false;
        }
        if (nt <= top && nb >= bottom) {
            break;
        }
    }
    remember_ellipse(cx, rx, ry, top, bottom);
    return current_state() == target;
}

/* Initial resting shape each state draws on entry — must match the first frame
 * produced by that state's renderer so the steady loop continues seamlessly. */
static eye_shape_t initial_shape_for(nino_eye_state_t st, const nino_state_profile_t *p)
{
    eye_shape_t s = {
        .kind = PREV_ELLIPSE,
        .cx = EYE_CX,
        .cy = EYE_CY,
        .rx = p->rx,
        .ry = p->ry,
        .top = EYE_CY - p->ry,
        .bottom = EYE_CY + p->ry,
        .heart_scale = p->heart_max_scale,
    };

    switch (st) {
    case NINO_EYE_HAPPY:
        s.kind = PREV_HEART;
        s.cx = EYE_CX;
        s.cy = EYE_CY - 4;
        break;
    case NINO_EYE_TIRED:
    case NINO_EYE_SAD:
        s.kind = PREV_ELLIPSE;
        s.top = p->top;
        s.bottom = p->bottom;
        break;
    case NINO_EYE_THINKING:
        s.kind = PREV_BLOB;
        s.cx = EYE_CX;
        s.cy = EYE_CY - 10;
        break;
    case NINO_EYE_CURIOUS_QUIZ:
        s.kind = PREV_BLOB;
        s.cx = EYE_CX - 16;
        s.cy = EYE_CY - 10;
        break;
    case NINO_EYE_RECALLING:
        s.kind = PREV_BLOB;
        s.cx = EYE_CX;
        s.cy = EYE_CY - 4;
        break;
    default:
        /* IDLE, SURPRISED, LISTENING: full ellipse centered. */
        break;
    }
    return s;
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

    if (!s_skip_initial) {
        erase_prev_eye();
        draw_full_eye(center_x, profile->rx, profile->ry);
    }

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
 * Tired/sad lidded blink: the eye sits in its lidded window [top, bottom]. On
 * each cycle the lids close from both edges toward the window mid row, hold,
 * then reopen back to the lidded window. Erases use the exact footprint.
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

    if (!s_skip_initial) {
        erase_prev_eye();
        draw_eye_rows(EYE_CX, rx, ry, top, bottom, color_eye());
        remember_ellipse(EYE_CX, rx, ry, top, bottom);
    }

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
 * Thinking: a normal solid eye that slowly rolls around the top to convey
 * pondering. No blink. The whole eye moves.
 */
static void run_thinking_eye(const nino_state_profile_t *profile, nino_eye_state_t expected)
{
    const int rx = profile->rx;
    const int ry = profile->ry;
    static const int gx[] = {0,   0,  -14,   0,   14};
    static const int gy[] = {-10, -22, -16, -22, -16};
    const int gaze_n = (int)(sizeof(gx) / sizeof(gx[0]));

    if (!s_skip_initial) {
        erase_prev_eye();
    }

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
 * Used to tilt the curious/recalling eye during the blink.
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

    static const int px[] = {-16, 16};
    static const int py[] = {-10, -10};
    const int n = (int)(sizeof(px) / sizeof(px[0]));

    int i = 0;
    int cx = EYE_CX + px[i];
    int cy = EYE_CY + py[i];
    if (!s_skip_initial) {
        erase_prev_eye();
        fill_ellipse(cx, cy, rx, ry, color_eye());
        remember_blob(cx, cy, rx, ry);
    }

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

/* Surprised: fast snap-open on entry, then hold wide. */
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

    if (!s_skip_initial) {
        erase_prev_eye();
        snap_open_eye(EYE_CX, rx, ry, step, frame_ms, expected);
        if (current_state() != expected) {
            return;
        }
        remember_ellipse(EYE_CX, rx, ry, EYE_CY - ry, EYE_CY + ry);
    }

    while (delay_ms_interruptible(hold_ms, expected)) {
        /* hold wide open */
    }
}

/*
 * Recalling: softer normal eye drifting upward through memory-gaze points.
 * Holds at each point, then a slow blink while shifting to the next.
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

    int i = 0;
    int cx = EYE_CX + px[i];
    int cy = EYE_CY + py[i];
    if (!s_skip_initial) {
        erase_prev_eye();
        fill_ellipse(cx, cy, rx, ry, color_eye());
        remember_blob(cx, cy, rx, ry);
    }

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
    if (!s_skip_initial) {
        erase_prev_eye();
        /* Heart's pointed bottom reaches lower than its lobes rise, so lift the
         * center above mid-screen to keep the symbol visually centered. */
        draw_heart(EYE_CX, EYE_CY - 4, profile->heart_max_scale, color_red());
        remember_heart(EYE_CX, EYE_CY - 4, profile->heart_max_scale);
    }
    while (delay_ms_interruptible(profile->state_ms, expected)) {
        /* Happy is intentionally still: one red symbol, no pulse or flicker. */
    }
}

void nino_eye_set_state(nino_eye_state_t state)
{
    if (state >= NINO_EYE_STATE_COUNT) {
        return;
    }
    if (s_state == state) {
        return; /* Ignore no-op transitions to keep runtime/logs clean. */
    }
    s_state = state;
    ESP_LOGI(TAG, "state set -> %d", (int)state);
}

nino_eye_state_t nino_eye_get_state(void)
{
    return current_state();
}

/* ---- Per-emotion triggers ---- */
void nino_eye_idle(void)      { nino_eye_set_state(NINO_EYE_IDLE); }
void nino_eye_happy(void)     { nino_eye_set_state(NINO_EYE_HAPPY); }
void nino_eye_tired(void)     { nino_eye_set_state(NINO_EYE_TIRED); }
void nino_eye_thinking(void)  { nino_eye_set_state(NINO_EYE_THINKING); }
void nino_eye_curious(void)   { nino_eye_set_state(NINO_EYE_CURIOUS_QUIZ); }
void nino_eye_sad(void)       { nino_eye_set_state(NINO_EYE_SAD); }
void nino_eye_surprised(void) { nino_eye_set_state(NINO_EYE_SURPRISED); }
void nino_eye_listening(void) { nino_eye_set_state(NINO_EYE_LISTENING); }
void nino_eye_recalling(void) { nino_eye_set_state(NINO_EYE_RECALLING); }

nino_eye_state_t nino_eye_state_from_name(const char *name)
{
    if (name == NULL || name[0] == '\0') {
        return NINO_EYE_STATE_COUNT;
    }
    if (strcmp(name, "idle") == 0) {
        return NINO_EYE_IDLE;
    } else if (strcmp(name, "happy") == 0) {
        return NINO_EYE_HAPPY;
    } else if (strcmp(name, "tired") == 0) {
        return NINO_EYE_TIRED;
    } else if (strcmp(name, "thinking") == 0) {
        return NINO_EYE_THINKING;
    } else if (strcmp(name, "curious") == 0 || strcmp(name, "quiz") == 0) {
        return NINO_EYE_CURIOUS_QUIZ;
    } else if (strcmp(name, "sad") == 0) {
        return NINO_EYE_SAD;
    } else if (strcmp(name, "surprised") == 0) {
        return NINO_EYE_SURPRISED;
    } else if (strcmp(name, "listening") == 0) {
        return NINO_EYE_LISTENING;
    } else if (strcmp(name, "recalling") == 0) {
        return NINO_EYE_RECALLING;
    }
    return NINO_EYE_STATE_COUNT;
}

void nino_eye_apply_expression(const char *name)
{
    nino_eye_state_t state = nino_eye_state_from_name(name);
    if (state >= NINO_EYE_STATE_COUNT) {
        /* Missing/unknown tag -> stay idle for this reply (server contract). */
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

    nino_eye_state_t state = nino_eye_state_from_name(token);
    if (state >= NINO_EYE_STATE_COUNT) {
        return false;
    }
    nino_eye_set_state(state);
    return true;
}

static void run_current_state_once(void)
{
    nino_eye_state_t state = current_state();
    if (state >= NINO_EYE_STATE_COUNT) {
        return;
    }

    const nino_state_profile_t *profile = &s_profiles[state];
    set_eye_color(profile);

    /* Smooth blink transition into this state: close the old shape, hold shut,
     * then open the new state's initial shape. On success the initial shape is
     * already drawn + remembered, so renderers skip their own instant draw. */
    eye_shape_t shape = initial_shape_for(state, profile);
    if (!transition_close(state)) {
        return;
    }
    if (!delay_ms_interruptible(TRANS_CLOSED_MS, state)) {
        return;
    }
    if (!transition_open(state, &shape)) {
        return;
    }
    s_skip_initial = true;

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
            if (!s_skip_initial) {
                draw_static_eye(profile);
            }
            while (delay_ms_interruptible(profile->state_ms, state)) {
                /* hold steady */
            }
        } else {
            run_blink_profile_once(profile, state);
        }
        break;
    }

    s_skip_initial = false;
}

static void eye_engine_task(void *arg)
{
    (void)arg;
    ESP_LOGI(TAG, "eye engine task started");
    clear_screen(color_bg());

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
    xTaskCreate(eye_engine_task, "nino_eye", 8192, NULL, 5, NULL);
}

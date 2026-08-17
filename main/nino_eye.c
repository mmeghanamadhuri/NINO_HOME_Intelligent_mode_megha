#include "nino_eye.h"

#include <ctype.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <math.h>
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_rom_sys.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "nino_display.h"
#include "sdkconfig.h"
#if CONFIG_NINO_EYE_DISPLAY_TFT
#include "tft_neutral.h"
#endif
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

/* One panel per eye — 128x96 OLED or 128x128 TFT (see nino_display.h). */
#define LOGICAL_WIDTH   OLED_WIDTH
#define LOGICAL_HEIGHT  OLED_HEIGHT
#define EYE_CX          (LOGICAL_WIDTH / 2)
/* Same downward shift on both panels — keeps the eye at a similar vertical ratio. */
#define NINO_VOFFSET    8
#define EYE_CY          (LOGICAL_HEIGHT / 2 + NINO_VOFFSET)
/* Legacy draw-time trim (0 = off); prefer NINO_VOFFSET for vertical centering. */
#define NINO_VSHIFT     0

/* Only this central region may be drawn/erased (eye, heart, blink). The rest of
 * the screen is never touched after boot — matches "only the oval changes". */
#define EYE_CLIP_HALF_W   46
#define EYE_CLIP_Y0       (EYE_CY - 52)
#define EYE_CLIP_Y1       (EYE_CY + 44)

/*
 * Two independent clocks (do not conflate them):
 *
 * 1) Animation FPS — how often we rewrite GDDRAM during motion sweeps.
 *    Locked to ~30.3 FPS (33 ms) to match default phone video capture so the
 *    camera does not sample mid-blink against a 100+ FPS software beat.
 * 2) Panel scan FPS — SSD1351 continuously refreshes the glass from GDDRAM
 *    (CLOCKDIV in ssd1351.c). SPI writes only update RAM; there is no
 *    display stop / refresh command after each image. Between animation
 *    frames we simply wait; the panel keeps showing the last RAM contents.
 *
 * Motion frames are composed in RAM; only changed pixels are SPI-written
 * (delta spans), and both OLEDs latch the same bits together (broadcast CS).
 */
#define NINO_EYE_FRAME_MS   33
#define BLINK_FRAME_MS      NINO_EYE_FRAME_MS
#define BLINK_CLOSE_STEP    2
#define MAX_GAZE_POINTS 5

/* Jetson neutral animation (spi_render.py) — 128×128 TFT values; scaled for OLED. */
#if CONFIG_NINO_EYE_DISPLAY_TFT
#define NEU_MAX_RADIUS          34
#define NEU_LOOK_DIST           28
#else
#define NEU_MAX_RADIUS          26
#define NEU_LOOK_DIST           21
#endif
#define NEU_HOLD_OPEN_MS        1600
#define NEU_SHUTTER_STEP_MS     9
#define NEU_WHITE_MS            480
#define NEU_DIAMETER_STEP_MS    11
#define NEU_SHUTTER_STEP_PX     4
#define NEU_DIAMETER_STEP_PX    2

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
    NINO_RENDER_NEUTRAL,
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
/* When set, idle uses a shorter open-hold so demo cue gaps can blink. */
static volatile bool s_demo_idle_pace = false;

/* Demo idle: ~0.8 s open + ~0.6 s blink ≈ 1.4 s/cycle (vs ~5.6 s normal). */
#define DEMO_IDLE_HOLD_MS 1600

static const nino_state_profile_t s_profiles[NINO_EYE_STATE_COUNT] = {
#if CONFIG_NINO_EYE_DISPLAY_TFT
    /* Idle on TFT: tft_neutral.c (standalone oval blink, no OLED engine). */
    [NINO_EYE_IDLE] = {
        .mode = NINO_RENDER_BLINK,
        .eye_r = 0, .eye_g = 0, .eye_b = 0,
    },
#else
    /* OLED idle: Jetson neutral loop — hold, shutter close, white flash, diameter
     * open with gaze center → right → center → left → center. */
    [NINO_EYE_IDLE] = {
        .mode = NINO_RENDER_NEUTRAL,
        .rx = NEU_MAX_RADIUS,
        .ry = NEU_MAX_RADIUS,
        .eye_r = 0, .eye_g = 0, .eye_b = 0,
    },
#endif
    /* happy: kept as the single red heart symbol (no eyelid/pupil/blink). */
    [NINO_EYE_HAPPY] = {
        .mode = NINO_RENDER_HEART,
        .state_ms = 900,
        .heart_min_scale = 20,
        .heart_max_scale = 20,
        .heart_frame_ms = 900,
        .eye_r = 255, .eye_g = 40, .eye_b = 70,   /* happy = red heart (only coloured state) */
    },
    /* tired: low eye with heavy lowered lid (bottom sliver visible). Slow blink
     * cadence via hold_ms; sweep paced at NINO_EYE_FRAME_MS for phone capture. */
    [NINO_EYE_TIRED] = {
        .mode = NINO_RENDER_STATIC,
        .rx = 24,
        .ry = 30,
        .top = EYE_CY + 4,
        .bottom = EYE_CY + 30,
        .hold_ms = 4500,
        .closed_hold_ms = 300,
        .blink_step = 2,
        .blink_ms = NINO_EYE_FRAME_MS,
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
        .blink_step = 2,
        .blink_ms = NINO_EYE_FRAME_MS,
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
        .blink_step = 2,
        .blink_ms = NINO_EYE_FRAME_MS,
        .state_ms = 4000,
        .eye_r = 0, .eye_g = 0, .eye_b = 0,
    },
    /* surprised: widest/tallest eye; one fast snap-open on entry, then hold wide
     * (no frantic blink). blink_step tunes snap geometry; pace is NINO_EYE_FRAME_MS. */
    [NINO_EYE_SURPRISED] = {
        .mode = NINO_RENDER_STATIC,
        .rx = 27,
        .ry = 36,
        .hold_ms = 5000,
        .blink_step = 4,
        .blink_ms = NINO_EYE_FRAME_MS,
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
        .blink_step = 2,
        .blink_ms = NINO_EYE_FRAME_MS,
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
        .blink_step = 2,
        .blink_ms = NINO_EYE_FRAME_MS,
        .gaze_offsets = {0},
        .gaze_count = 1,
        .eye_r = 0, .eye_g = 0, .eye_b = 0,
    },
    /* mad: idle-size eye that shakes frantically - 3 s fast left<->right, then
     * 2 s fast up<->down, repeating. hold_ms = horizontal phase, state_ms =
     * vertical phase; per-frame pace is NINO_EYE_FRAME_MS (~30 FPS). */
    [NINO_EYE_MAD] = {
        .mode = NINO_RENDER_STATIC,
        .rx = 24,
        .ry = 30,
        .hold_ms = 3000,
        .state_ms = 2000,
        .blink_ms = NINO_EYE_FRAME_MS,
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

/* Background is a warm-blue white (not pure 255,255,255 — that reads pink on
 * one panel and blue on the other). Eyes stay black. */
static uint16_t s_eye_color = 0x0000;

static uint16_t color_bg(void)
{
#if CONFIG_NINO_EYE_DISPLAY_TFT
    return ssd1351_color(255, 255, 255);
#else
    return ssd1351_color(225, 236, 255);
#endif
}

/* Jetson neutral uses pure white background during the blink cycle. */
static uint16_t color_neu_white(void)
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
 * Dirty-rectangle tracking. AABB alone is not enough for present: blitting the
 * whole box rewrites unchanged background pixels inside it, and this Waveshare
 * SSD1351 flashes when background is rewritten (phone flicker up close).
 * Present uses a second "what's on the glass" buffer and SPI-writes only
 * pixels that actually changed (horizontal delta spans).
 */
static int s_dirty_x0 = LOGICAL_WIDTH;
static int s_dirty_y0 = LOGICAL_HEIGHT;
static int s_dirty_x1 = -1;
static int s_dirty_y1 = -1;

/* Compose buffer + mirror of GDDRAM contents after the last successful present. */
static uint16_t *s_fb = NULL;
static uint16_t *s_fb_hw = NULL;
static bool s_fb_batch = false;

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

static bool fb_init(void)
{
    if (s_fb != NULL && s_fb_hw != NULL) {
        return true;
    }
    const size_t bytes = (size_t)LOGICAL_WIDTH * (size_t)LOGICAL_HEIGHT * sizeof(uint16_t);
    if (s_fb == NULL) {
        s_fb = (uint16_t *)heap_caps_malloc(bytes, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
        if (s_fb == NULL) {
            s_fb = (uint16_t *)malloc(bytes);
        }
    }
    if (s_fb_hw == NULL) {
        s_fb_hw = (uint16_t *)heap_caps_malloc(bytes, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
        if (s_fb_hw == NULL) {
            s_fb_hw = (uint16_t *)malloc(bytes);
        }
    }
    if (s_fb == NULL || s_fb_hw == NULL) {
        ESP_LOGE(TAG, "eye framebuffer alloc failed (%u bytes x2)", (unsigned)bytes);
        return false;
    }
    memset(s_fb, 0, bytes);
    memset(s_fb_hw, 0, bytes);
    ESP_LOGI(TAG, "eye framebuffer ready %dx%d (~%u FPS, delta-span present)",
             LOGICAL_WIDTH, LOGICAL_HEIGHT, (unsigned)(1000 / NINO_EYE_FRAME_MS));
    return true;
}

static void fb_present_full(void)
{
    if (s_fb == NULL) {
        return;
    }
    ssd1351_draw_bitmap(0, 0, LOGICAL_WIDTH, LOGICAL_HEIGHT, s_fb);
    if (s_fb_hw != NULL) {
        memcpy(s_fb_hw, s_fb,
               (size_t)LOGICAL_WIDTH * (size_t)LOGICAL_HEIGHT * sizeof(uint16_t));
    }
    dirty_reset();
}

/*
 * SPI only pixels that differ from what is already on the glass. Unchanged
 * background is never rewritten — that is what stopped the close-range flash.
 */
static void fb_present(void)
{
    if (s_fb == NULL || s_fb_hw == NULL) {
        return;
    }
    if (s_dirty_x1 < s_dirty_x0 || s_dirty_y1 < s_dirty_y0) {
        return;
    }

    int x0 = s_dirty_x0;
    int y0 = s_dirty_y0;
    int x1 = s_dirty_x1;
    int y1 = s_dirty_y1;
    if (x0 < 0) {
        x0 = 0;
    }
    if (y0 < 0) {
        y0 = 0;
    }
    if (x1 >= LOGICAL_WIDTH) {
        x1 = LOGICAL_WIDTH - 1;
    }
    if (y1 >= LOGICAL_HEIGHT) {
        y1 = LOGICAL_HEIGHT - 1;
    }
    if (x1 < x0 || y1 < y0) {
        dirty_reset();
        return;
    }

    for (int y = y0; y <= y1; y++) {
        uint16_t *row = &s_fb[(size_t)y * (size_t)LOGICAL_WIDTH];
        uint16_t *hw = &s_fb_hw[(size_t)y * (size_t)LOGICAL_WIDTH];
        int x = x0;
        while (x <= x1) {
            while (x <= x1 && row[x] == hw[x]) {
                x++;
            }
            if (x > x1) {
                break;
            }
            const int xs = x;
            while (x <= x1 && row[x] != hw[x]) {
                x++;
            }
            const int xe = x - 1;
            const int w = xe - xs + 1;
            ssd1351_draw_bitmap(xs, y, w, 1, &row[xs]);
            memcpy(&hw[xs], &row[xs], (size_t)w * sizeof(uint16_t));
        }
    }
    dirty_reset();
}

static void fb_batch_begin(void)
{
    s_fb_batch = true;
    dirty_reset();
}

static void fb_batch_end(void)
{
    s_fb_batch = false;
    fb_present();
}

static void fb_hw_note_span(int x, int y, int width, uint16_t color)
{
    if (s_fb_hw == NULL || width <= 0) {
        return;
    }
    uint16_t *row = &s_fb_hw[(size_t)y * (size_t)LOGICAL_WIDTH + (size_t)x];
    for (int i = 0; i < width; i++) {
        row[i] = color;
    }
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

    /* Always keep the shadow buffer in sync with what we intend to show. */
    if (s_fb != NULL) {
        uint16_t *row = &s_fb[(size_t)y * (size_t)LOGICAL_WIDTH + (size_t)x];
        for (int i = 0; i < width; i++) {
            row[i] = color;
        }
    }

    /* Dirty uses the same coords as the framebuffer / SPI window. */
    dirty_add(x, y, x + width - 1, y);

    /* During a batch with a shadow buffer, defer SPI until fb_batch_end(). */
    if (!s_fb_batch || s_fb == NULL) {
        ssd1351_fill_rect(x, y, width, 1, color);
        fb_hw_note_span(x, y, width, color);
    }
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
    if (s_fb != NULL) {
        const size_t n = (size_t)LOGICAL_WIDTH * (size_t)LOGICAL_HEIGHT;
        for (size_t i = 0; i < n; i++) {
            s_fb[i] = color;
        }
        /* Full clear must rewrite every pixel once (boot / state reset only). */
        fb_present_full();
    } else {
        ssd1351_fill_screen(color);
        dirty_reset();
    }
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
 * Un-draw the previous glyph by shape (background colour over its footprint).
 * Full-screen clear is avoided — on TFT that looked like a pop; on OLED it
 * caused a white flash. Blink frames compose in the shadow FB, then one SPI
 * present per animation step (~30 FPS).
 */
static void erase_prev_eye(void)
{
    fb_batch_begin();
    switch (s_prev_kind) {
    case PREV_ELLIPSE:
        draw_eye_rows(s_prev_cx, s_prev_rx, s_prev_ry, s_prev_top, s_prev_bottom, color_bg());
        break;
    case PREV_HEART:
        draw_heart(s_prev_heart_cx, s_prev_heart_cy, s_prev_heart_scale, color_bg());
        break;
    case PREV_BLOB:
        fill_ellipse(s_prev_cx, s_prev_blob_cy, s_prev_rx, s_prev_ry, color_bg());
        break;
    case PREV_CAPSULE:
        erase_capsule(s_prev_cap_cx, s_prev_cap_cy, s_prev_cap_half_len, s_prev_cap_radius);
        break;
    case PREV_FIRE:
        erase_fire(s_prev_fire_cx, s_prev_fire_cy);
        break;
    case PREV_SMILE:
        erase_smile(s_prev_smile_cx, s_prev_smile_cy);
        break;
    case PREV_SPARKLE:
        erase_sparkle(s_prev_sparkle_cx, s_prev_sparkle_cy);
        break;
    case PREV_EMOJI:
        if (s_prev_emoji_bmp != NULL) {
            erase_emoji_bmp(s_prev_emoji_cx, s_prev_emoji_cy, s_prev_emoji_bmp);
        }
        break;
    default:
        break;
    }
    fb_batch_end();
    s_prev_kind = PREV_NONE;
    s_prev_emoji_bmp = NULL;
}

static nino_eye_state_t current_state(void)
{
    return (nino_eye_state_t)s_state;
}

/*
 * Deadline-based wait so SPI draw time does not stack on top of frame_ms
 * (which made motion look low-FPS / jittery on phone video). Long waits still
 * yield to FreeRTOS; sub-ms remainders spin with esp_rom_delay_us.
 */
static bool delay_ms_interruptible(int total_ms, nino_eye_state_t expected)
{
    if (total_ms <= 0) {
        if (s_restart_requested) {
            s_restart_requested = false;
            return false;
        }
        return current_state() == expected;
    }

    const int64_t deadline_us = esp_timer_get_time() + (int64_t)total_ms * 1000LL;
    while (esp_timer_get_time() < deadline_us) {
        if (s_restart_requested) {
            s_restart_requested = false;
            return false;
        }
        if (current_state() != expected) {
            return false;
        }

        const int64_t remaining_us = deadline_us - esp_timer_get_time();
        if (remaining_us <= 0) {
            break;
        }

        /* Yield in ~1 ms slices so other tasks / the idle task keep running. */
        if (remaining_us > 1000) {
            int slice_ms = (int)(remaining_us / 1000);
            if (slice_ms > 25) {
                slice_ms = 25;
            }
            TickType_t ticks = pdMS_TO_TICKS(slice_ms);
            if (ticks == 0) {
                ticks = 1;
            }
            vTaskDelay(ticks);
        } else {
            esp_rom_delay_us((uint32_t)remaining_us);
            break;
        }
    }

    return current_state() == expected;
}

/* Wait the remainder of a frame budget after drawing (true FPS = 1000/frame_ms). */
static bool pace_frame_interruptible(int64_t frame_start_us, int frame_ms,
                                     nino_eye_state_t expected)
{
    const int64_t budget_us = (int64_t)frame_ms * 1000LL;
    const int64_t elapsed_us = esp_timer_get_time() - frame_start_us;
    const int64_t remain_us = budget_us - elapsed_us;
    if (remain_us <= 0) {
        if (s_restart_requested) {
            s_restart_requested = false;
            return false;
        }
        return current_state() == expected;
    }
    return delay_ms_interruptible((int)((remain_us + 999) / 1000), expected);
}

static void fill_rect_rows(int x, int y, int w, int h, uint16_t color)
{
    if (h <= 0 || w <= 0) {
        return;
    }
    for (int row = 0; row < h; row++) {
        draw_landscape_hline(x, y + row, w, color);
    }
}

static float ease_out_cubic(float t)
{
    if (t <= 0.0f) {
        return 0.0f;
    }
    if (t >= 1.0f) {
        return 1.0f;
    }
    const float u = 1.0f - t;
    return 1.0f - (u * u * u);
}

typedef struct {
    int seg;
    int phase;
    int64_t mark_ms;
    int shutter_y;
    int diameter_d;
    int from_x;
    int to_x;
} neutral_anim_t;

static neutral_anim_t s_neu;

static int64_t neu_now_ms(void)
{
    return esp_timer_get_time() / 1000;
}

static void neu_load_seg(int seg)
{
    static const int k_from[4] = {0, NEU_LOOK_DIST, 0, -NEU_LOOK_DIST};
    static const int k_to[4] = {NEU_LOOK_DIST, 0, -NEU_LOOK_DIST, 0};
    seg &= 3;
    s_neu.seg = seg;
    s_neu.from_x = k_from[seg];
    s_neu.to_x = k_to[seg];
}

static void neu_reset(void)
{
    s_neu.seg = 0;
    neu_load_seg(0);
    s_neu.phase = 0;
    s_neu.mark_ms = neu_now_ms();
    s_neu.shutter_y = 0;
    s_neu.diameter_d = 0;
}

static void neu_paint_shutter_close(int shutter_y, int look_x)
{
    const int cx = EYE_CX + look_x;
    const int cy = EYE_CY;
    const int r = NEU_MAX_RADIUS;
    const uint16_t white = color_neu_white();

    fill_rect_rows(EYE_CX - EYE_CLIP_HALF_W, EYE_CLIP_Y0,
                   EYE_CLIP_HALF_W * 2 + 1, EYE_CLIP_Y1 - EYE_CLIP_Y0 + 1, white);
    fill_ellipse(cx, cy, r, r, color_eye());
    const int shutter_top = cy + r - shutter_y;
    if (shutter_top < LOGICAL_HEIGHT) {
        const int h = LOGICAL_HEIGHT - shutter_top;
        if (h > 0) {
            fill_rect_rows(0, shutter_top, LOGICAL_WIDTH, h, white);
        }
    }
    remember_blob(cx, cy, r, r);
}

static void neu_paint_diameter_open(int open_dist, int look_x)
{
    const int cx = EYE_CX + look_x;
    const int cy = EYE_CY;
    const int r = NEU_MAX_RADIUS;
    const uint16_t white = color_neu_white();

    fill_rect_rows(EYE_CX - EYE_CLIP_HALF_W, EYE_CLIP_Y0,
                   EYE_CLIP_HALF_W * 2 + 1, EYE_CLIP_Y1 - EYE_CLIP_Y0 + 1, white);
    fill_ellipse(cx, cy, r, r, color_eye());
    if (open_dist > 0) {
        const int top_h = cy - open_dist;
        if (top_h > 0) {
            fill_rect_rows(0, 0, LOGICAL_WIDTH, top_h, white);
        }
        const int bot_y = cy + open_dist;
        if (bot_y < LOGICAL_HEIGHT) {
            fill_rect_rows(0, bot_y, LOGICAL_WIDTH, LOGICAL_HEIGHT - bot_y, white);
        }
    }
    remember_blob(cx, cy, r, r);
}

/*
 * Jetson neutral: hold open → shutter close → white flash → diameter open at
 * next gaze (center → right → center → left → center …).
 */
static void run_neutral_nino(const nino_state_profile_t *profile, nino_eye_state_t expected)
{
    (void)profile;
    neu_reset();
    fb_batch_begin();
    erase_prev_eye();
    neu_paint_diameter_open(NEU_MAX_RADIUS, s_neu.from_x);
    fb_batch_end();

    while (current_state() == expected) {
        if (s_restart_requested) {
            s_restart_requested = false;
            neu_reset();
            fb_batch_begin();
            neu_paint_diameter_open(NEU_MAX_RADIUS, s_neu.from_x);
            fb_batch_end();
            continue;
        }

        const int64_t now = neu_now_ms();

        if (s_neu.phase == 0) {
            if (now - s_neu.mark_ms >= NEU_HOLD_OPEN_MS) {
                s_neu.phase = 1;
                s_neu.shutter_y = 0;
                s_neu.mark_ms = now;
            } else if (!delay_ms_interruptible(20, expected)) {
                return;
            }
        } else if (s_neu.phase == 1) {
            if (now - s_neu.mark_ms >= NEU_SHUTTER_STEP_MS) {
                s_neu.mark_ms = now;
                fb_batch_begin();
                neu_paint_shutter_close(s_neu.shutter_y, s_neu.from_x);
                fb_batch_end();
                s_neu.shutter_y += NEU_SHUTTER_STEP_PX;
                if (s_neu.shutter_y > NEU_MAX_RADIUS * 2) {
                    fb_batch_begin();
                    fill_rect_rows(EYE_CX - EYE_CLIP_HALF_W, EYE_CLIP_Y0,
                                   EYE_CLIP_HALF_W * 2 + 1,
                                   EYE_CLIP_Y1 - EYE_CLIP_Y0 + 1, color_neu_white());
                    fb_batch_end();
                    s_prev_kind = PREV_NONE;
                    s_neu.phase = 2;
                    s_neu.mark_ms = now;
                }
            } else if (!delay_ms_interruptible(5, expected)) {
                return;
            }
        } else if (s_neu.phase == 2) {
            if (now - s_neu.mark_ms >= NEU_WHITE_MS) {
                s_neu.phase = 3;
                s_neu.diameter_d = 0;
                s_neu.mark_ms = now;
            } else if (!delay_ms_interruptible(20, expected)) {
                return;
            }
        } else {
            if (now - s_neu.mark_ms >= NEU_DIAMETER_STEP_MS) {
                s_neu.mark_ms = now;
                const float t =
                    fminf(1.0f, (float)s_neu.diameter_d / (float)NEU_MAX_RADIUS);
                const int dd = (int)(ease_out_cubic(t) * (float)NEU_MAX_RADIUS);
                fb_batch_begin();
                neu_paint_diameter_open(dd, s_neu.to_x);
                fb_batch_end();
                s_neu.diameter_d += NEU_DIAMETER_STEP_PX;
                if (s_neu.diameter_d > NEU_MAX_RADIUS) {
                    s_neu.seg = (s_neu.seg + 1) & 3;
                    neu_load_seg(s_neu.seg);
                    s_neu.phase = 0;
                    s_neu.mark_ms = now;
                }
            } else if (!delay_ms_interruptible(5, expected)) {
                return;
            }
        }
    }
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
    const int frame_ms = NINO_EYE_FRAME_MS;

    int open_hold_ms = profile->hold_ms / 2;
    if (expected == NINO_EYE_IDLE && s_demo_idle_pace) {
        open_hold_ms = DEMO_IDLE_HOLD_MS / 2;
    }
    if (!delay_ms_interruptible(open_hold_ms, expected)) {
        return current_x;
    }

    /* Geometric close: erase oval rows from top/bottom toward center. Each
     * step is composed in RAM then blitted once (~30 FPS, camera-safe). */
    int previous_open = ry;
    for (int open = ry - step; open >= 0; open -= step) {
        if (current_state() != expected) {
            return current_x;
        }
        const int64_t frame_start = esp_timer_get_time();
        fb_batch_begin();
        erase_eye_rows(current_x, rx, ry, EYE_CY - previous_open, EYE_CY - open - 1);
        erase_eye_rows(current_x, rx, ry, EYE_CY + open + 1, EYE_CY + previous_open);
        fb_batch_end();
        previous_open = open;
        if (!pace_frame_interruptible(frame_start, frame_ms, expected)) {
            return current_x;
        }
    }

    fb_batch_begin();
    erase_eye_rows(current_x, rx, ry, EYE_CY - ry, EYE_CY + ry);
    fb_batch_end();
    if (!delay_ms_interruptible(profile->closed_hold_ms, expected)) {
        return current_x;
    }

    int previous_reveal = 0;
    fb_batch_begin();
    draw_eye_rows(next_x, rx, ry, EYE_CY, EYE_CY, color_eye());
    fb_batch_end();
    for (int open = step; open <= ry; open += step) {
        if (current_state() != expected) {
            return next_x;
        }
        const int64_t frame_start = esp_timer_get_time();
        fb_batch_begin();
        draw_eye_rows(next_x, rx, ry, EYE_CY - open, EYE_CY - previous_reveal - 1, color_eye());
        draw_eye_rows(next_x, rx, ry, EYE_CY + previous_reveal + 1, EYE_CY + open, color_eye());
        fb_batch_end();
        previous_reveal = open;
        if (!pace_frame_interruptible(frame_start, frame_ms, expected)) {
            return next_x;
        }
    }

    if (previous_reveal < ry) {
        fb_batch_begin();
        draw_eye_rows(next_x, rx, ry, EYE_CY - ry, EYE_CY - previous_reveal - 1, color_eye());
        draw_eye_rows(next_x, rx, ry, EYE_CY + previous_reveal + 1, EYE_CY + ry, color_eye());
        fb_batch_end();
    }

    remember_ellipse(next_x, rx, ry, EYE_CY - ry, EYE_CY + ry);
    return next_x;
}

#if CONFIG_NINO_EYE_DISPLAY_TFT
static bool tft_idle_should_run(void)
{
    if (s_restart_requested) {
        s_restart_requested = false;
        return false;
    }
    return current_state() == NINO_EYE_IDLE;
}
#endif

static void draw_static_eye(const nino_state_profile_t *profile)
{
    fb_batch_begin();
    erase_prev_eye();
    draw_eye_rows(EYE_CX, profile->rx, profile->ry, profile->top, profile->bottom, color_eye());
    fb_batch_end();
    remember_ellipse(EYE_CX, profile->rx, profile->ry, profile->top, profile->bottom);
}

static void run_blink_profile_once(const nino_state_profile_t *profile, nino_eye_state_t expected)
{
    int center_x = EYE_CX;

    /* Un-draw the previous state's shape (only its own pixels), then draw the
     * eye ONCE. After that we only blink/move incrementally, so there is no
     * full-eye erase-and-redraw flash on every cycle. */
    fb_batch_begin();
    erase_prev_eye();
    draw_full_eye(center_x, profile->rx, profile->ry);
    fb_batch_end();

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
    const int frame_ms = NINO_EYE_FRAME_MS;

    fb_batch_begin();
    erase_prev_eye();
    draw_eye_rows(EYE_CX, rx, ry, top, bottom, color_eye());
    fb_batch_end();
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
            const int64_t frame_start = esp_timer_get_time();
            int new_top = top + off;
            int new_bot = bottom - off;
            if (new_top > cy) {
                new_top = cy;
            }
            if (new_bot < cy) {
                new_bot = cy;
            }
            fb_batch_begin();
            erase_eye_rows(EYE_CX, rx, ry, cur_top, new_top - 1);
            erase_eye_rows(EYE_CX, rx, ry, new_bot + 1, cur_bot);
            fb_batch_end();
            cur_top = new_top;
            cur_bot = new_bot;
            if (!pace_frame_interruptible(frame_start, frame_ms, expected)) {
                return;
            }
            if (new_top >= cy && new_bot <= cy) {
                break;
            }
        }

        fb_batch_begin();
        erase_eye_rows(EYE_CX, rx, ry, top, bottom);
        fb_batch_end();
        if (!delay_ms_interruptible(profile->closed_hold_ms, expected)) {
            return;
        }

        cur_top = cy;
        cur_bot = cy;
        fb_batch_begin();
        draw_eye_rows(EYE_CX, rx, ry, cy, cy, color_eye());
        fb_batch_end();
        for (int off = step; ; off += step) {
            if (current_state() != expected) {
                return;
            }
            const int64_t frame_start = esp_timer_get_time();
            int new_top = cy - off;
            int new_bot = cy + off;
            if (new_top < top) {
                new_top = top;
            }
            if (new_bot > bottom) {
                new_bot = bottom;
            }
            fb_batch_begin();
            draw_eye_rows(EYE_CX, rx, ry, new_top, cur_top - 1, color_eye());
            draw_eye_rows(EYE_CX, rx, ry, cur_bot + 1, new_bot, color_eye());
            fb_batch_end();
            cur_top = new_top;
            cur_bot = new_bot;
            if (!pace_frame_interruptible(frame_start, frame_ms, expected)) {
                return;
            }
            if (new_top <= top && new_bot >= bottom) {
                break;
            }
        }

        fb_batch_begin();
        draw_eye_rows(EYE_CX, rx, ry, top, bottom, color_eye());
        fb_batch_end();
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

    fb_batch_begin();
    erase_prev_eye();
    fb_batch_end();

    int prev_cx = 0, prev_cy = 0;
    bool have_eye = false;
    int i = 0;
    while (current_state() == expected) {
        int ex = EYE_CX + gx[i];
        int ey = EYE_CY + gy[i];
        fb_batch_begin();
        if (have_eye) {
            fill_ellipse(prev_cx, prev_cy, rx, ry, color_bg());
        }
        fill_ellipse(ex, ey, rx, ry, color_eye());
        fb_batch_end();
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
        const int64_t frame_start = esp_timer_get_time();
        fb_batch_begin();
        draw_blob_rows(cx0, cy0, rx, ry, cy0 - previous_open, cy0 - open - 1, color_bg());
        draw_blob_rows(cx0, cy0, rx, ry, cy0 + open + 1, cy0 + previous_open, color_bg());
        fb_batch_end();
        previous_open = open;
        if (!pace_frame_interruptible(frame_start, frame_ms, expected)) {
            return false;
        }
    }

    fb_batch_begin();
    draw_blob_rows(cx0, cy0, rx, ry, cy0 - ry, cy0 + ry, color_bg());
    fb_batch_end();
    if (!delay_ms_interruptible(closed_hold, expected)) {
        return false;
    }

    int previous_reveal = 0;
    fb_batch_begin();
    draw_blob_rows(cx1, cy1, rx, ry, cy1, cy1, color_eye());
    fb_batch_end();
    for (int open = step; open <= ry; open += step) {
        if (current_state() != expected) {
            return false;
        }
        const int64_t frame_start = esp_timer_get_time();
        fb_batch_begin();
        draw_blob_rows(cx1, cy1, rx, ry, cy1 - open, cy1 - previous_reveal - 1, color_eye());
        draw_blob_rows(cx1, cy1, rx, ry, cy1 + previous_reveal + 1, cy1 + open, color_eye());
        fb_batch_end();
        previous_reveal = open;
        if (!pace_frame_interruptible(frame_start, frame_ms, expected)) {
            return false;
        }
    }

    if (previous_reveal < ry) {
        fb_batch_begin();
        draw_blob_rows(cx1, cy1, rx, ry, cy1 - ry, cy1 - previous_reveal - 1, color_eye());
        draw_blob_rows(cx1, cy1, rx, ry, cy1 + previous_reveal + 1, cy1 + ry, color_eye());
        fb_batch_end();
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
    const int frame_ms = NINO_EYE_FRAME_MS;

    /* Tilted look-points: up-left then up-right (head-tilt feel). */
    static const int px[] = {-16, 16};
    static const int py[] = {-10, -10};
    const int n = (int)(sizeof(px) / sizeof(px[0]));

    fb_batch_begin();
    erase_prev_eye();
    int i = 0;
    int cx = EYE_CX + px[i];
    int cy = EYE_CY + py[i];
    fill_ellipse(cx, cy, rx, ry, color_eye());
    fb_batch_end();
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
    fb_batch_begin();
    draw_eye_rows(cx, rx, ry, EYE_CY, EYE_CY, color_eye());
    fb_batch_end();
    for (int open = step; open <= ry; open += step) {
        if (current_state() != expected) {
            return;
        }
        const int64_t frame_start = esp_timer_get_time();
        fb_batch_begin();
        draw_eye_rows(cx, rx, ry, EYE_CY - open, EYE_CY - previous_reveal - 1, color_eye());
        draw_eye_rows(cx, rx, ry, EYE_CY + previous_reveal + 1, EYE_CY + open, color_eye());
        fb_batch_end();
        previous_reveal = open;
        if (!pace_frame_interruptible(frame_start, frame_ms, expected)) {
            return;
        }
    }
    if (previous_reveal < ry) {
        fb_batch_begin();
        draw_eye_rows(cx, rx, ry, EYE_CY - ry, EYE_CY - previous_reveal - 1, color_eye());
        draw_eye_rows(cx, rx, ry, EYE_CY + previous_reveal + 1, EYE_CY + ry, color_eye());
        fb_batch_end();
    }
}

/*
 * Surprised: snap-open on entry (geometry from blink_step), then hold.
 * Paced at NINO_EYE_FRAME_MS for phone 30 fps capture.
 */
static void run_surprised_eye(const nino_state_profile_t *profile, nino_eye_state_t expected)
{
    const int rx = profile->rx;
    const int ry = profile->ry;
    int step = profile->blink_step > 0 ? profile->blink_step : 8;
    if (step <= 0) {
        step = 1;
    }
    const int frame_ms = NINO_EYE_FRAME_MS;
    const int hold_ms = profile->hold_ms > 0 ? profile->hold_ms : 5000;

    fb_batch_begin();
    erase_prev_eye();
    fb_batch_end();
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
    const int frame_ms = NINO_EYE_FRAME_MS;

    static const int px[] = {0, -12, 0, 12, 0};
    static const int py[] = {-4, -12, -16, -12, -6};
    const int n = (int)(sizeof(px) / sizeof(px[0]));

    fb_batch_begin();
    erase_prev_eye();
    int i = 0;
    int cx = EYE_CX + px[i];
    int cy = EYE_CY + py[i];
    fill_ellipse(cx, cy, rx, ry, color_eye());
    fb_batch_end();
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
    fb_batch_begin();
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
    fb_batch_end();
}

/*
 * Mad: idle-size eye that shakes. Phase 1 left<->right for hold_ms, phase 2
 * up<->down for state_ms. Motion paced at NINO_EYE_FRAME_MS (~30 FPS).
 */
static void run_mad_eye(const nino_state_profile_t *profile, nino_eye_state_t expected)
{
    const int rx = profile->rx;
    const int ry = profile->ry;
    const int frame_ms = NINO_EYE_FRAME_MS;
    const int h_amp = 18;   /* horizontal shake amplitude (px from center) */
    const int v_amp = 12;   /* vertical shake amplitude */
    const int h_step = 4;   /* px/frame — smaller steps look smoother at 30 FPS */
    const int v_step = 4;

    fb_batch_begin();
    erase_prev_eye();
    int cx = EYE_CX;
    int cy = EYE_CY;
    fill_ellipse(cx, cy, rx, ry, color_eye());
    fb_batch_end();
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
                const int64_t frame_start = esp_timer_get_time();
                move_eye_blob(cx, cy, ncx, cy, rx, ry);
                cx = ncx;
                remember_blob(cx, cy, rx, ry);
                if (!pace_frame_interruptible(frame_start, frame_ms, expected)) {
                    return;
                }
            } else if (!delay_ms_interruptible(frame_ms, expected)) {
                return;
            }
            if (cx == target) {
                target = (target > EYE_CX) ? (EYE_CX - h_amp) : (EYE_CX + h_amp);
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
                const int64_t frame_start = esp_timer_get_time();
                move_eye_blob(cx, cy, cx, ncy, rx, ry);
                cy = ncy;
                remember_blob(cx, cy, rx, ry);
                if (!pace_frame_interruptible(frame_start, frame_ms, expected)) {
                    return;
                }
            } else if (!delay_ms_interruptible(frame_ms, expected)) {
                return;
            }
            if (cy == vtarget) {
                vtarget = (vtarget > EYE_CY) ? (EYE_CY - v_amp) : (EYE_CY + v_amp);
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
    fb_batch_begin();
    erase_prev_eye();
    /* Heart's pointed bottom reaches lower than its lobes rise, so lift the
     * center well above mid-screen to keep the symbol visually centered. */
    draw_heart(EYE_CX, EYE_CY - 4, profile->heart_max_scale, color_red());
    fb_batch_end();
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

    fb_batch_begin();
    erase_prev_eye();
    draw_capsule(EYE_CX, EYE_CY, half_len, radius);
    fb_batch_end();
    remember_capsule(EYE_CX, EYE_CY, half_len, radius);
    while (delay_ms_interruptible(profile->state_ms, expected)) {
        /* Static slanted capsule, like happy. */
    }
}

static void run_jai_bhalaiah_profile_once(const nino_state_profile_t *profile, nino_eye_state_t expected)
{
    const int cx = EYE_CX;
    const int cy = EYE_CY + 2;

    fb_batch_begin();
    erase_prev_eye();
    draw_fire(cx, cy);
    fb_batch_end();
    remember_fire(cx, cy);
    while (delay_ms_interruptible(profile->state_ms, expected)) {
        /* Static fire emoji — exact bitmap from the reference image. */
    }
}

static void run_smile_profile_once(const nino_state_profile_t *profile, nino_eye_state_t expected)
{
    const int cx = EYE_CX;
    const int cy = EYE_CY;

    fb_batch_begin();
    erase_prev_eye();
    draw_smile(cx, cy);
    fb_batch_end();
    remember_smile(cx, cy);
    while (delay_ms_interruptible(profile->state_ms, expected)) {
        /* Static WhatsApp-style smile emoji. */
    }
}

static void run_sparkle_profile_once(const nino_state_profile_t *profile, nino_eye_state_t expected)
{
    const int cx = EYE_CX;
    const int cy = EYE_CY;

    fb_batch_begin();
    erase_prev_eye();
    draw_sparkle(cx, cy);
    fb_batch_end();
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

    fb_batch_begin();
    erase_prev_eye();
    draw_emoji_bmp(cx, cy, bmp);
    fb_batch_end();
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
    const bool same = (s_state == state);
    s_state = state;
    if (same) {
      /* Re-applying the same emotion must force a redraw (e.g. smile again). */
      s_restart_requested = true;
    }
    ESP_LOGI(TAG, "state set -> %d%s", (int)state, same ? " (restart)" : "");
}

void nino_eye_set_demo_idle_pace(bool enabled)
{
    s_demo_idle_pace = enabled;
    if (current_state() == NINO_EYE_IDLE) {
        /* Restart so the new open-hold is picked up immediately. */
        s_restart_requested = true;
    }
    ESP_LOGI(TAG, "demo idle pace %s", enabled ? "on (~1.4s/cycle)" : "off");
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

    if (strcmp(token, "idle") == 0 || strcmp(token, "neutral") == 0 ||
        strcmp(token, "normal") == 0) {
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

const char *nino_eye_state_to_name(nino_eye_state_t state)
{
    switch (state) {
    case NINO_EYE_IDLE:           return "idle";
    case NINO_EYE_HAPPY:          return "happy";
    case NINO_EYE_TIRED:          return "tired";
    case NINO_EYE_THINKING:       return "thinking";
    case NINO_EYE_CURIOUS_QUIZ:   return "curious";
    case NINO_EYE_SAD:            return "sad";
    case NINO_EYE_SURPRISED:      return "surprised";
    case NINO_EYE_LISTENING:      return "listening";
    case NINO_EYE_RECALLING:      return "recalling";
    case NINO_EYE_MAD:            return "mad";
    case NINO_EYE_MED:            return "med";
    case NINO_EYE_JAI_BHALAIAH:   return "fire";
    case NINO_EYE_SMILE:          return "smile";
    case NINO_EYE_SPARKLE:        return "sparkle";
    case NINO_EYE_PENCIL:         return "pencil";
    case NINO_EYE_RADIO:          return "radio";
    case NINO_EYE_TV:             return "tv";
    case NINO_EYE_BULB:           return "bulb";
    case NINO_EYE_ROBOT:          return "robot";
    case NINO_EYE_BIGSMILE:       return "bigsmile";
    default:                      return "?";
    }
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
    case NINO_EYE_IDLE:
#if CONFIG_NINO_EYE_DISPLAY_TFT
        tft_neutral_run(tft_idle_should_run);
#else
        if (profile->mode == NINO_RENDER_NEUTRAL) {
            run_neutral_nino(profile, state);
        } else {
            run_blink_profile_once(profile, state);
        }
#endif
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
    ESP_LOGI(TAG, "eye engine task started (animation %d ms/frame ≈ %u FPS)",
             NINO_EYE_FRAME_MS, (unsigned)(1000 / NINO_EYE_FRAME_MS));
    if (!fb_init()) {
        ESP_LOGW(TAG, "continuing without framebuffer (row SPI may tear on camera)");
    }
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

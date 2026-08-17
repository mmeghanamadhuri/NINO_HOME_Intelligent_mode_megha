#include "tft_neutral.h"

#include "st7735.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

/* 128×128 round eye modules — sit slightly below geometric centre on glass */
#define NEU_CX          64
#define NEU_VOFFSET     7    /* ~1.5 mm down on 1.44" panel */
#define NEU_CY          (64 + NEU_VOFFSET)
#define NEU_RX          28
#define NEU_RY          32

#define NEU_WHITE       0xFFFF
#define NEU_BLACK       0x0000

#define NEU_HOLD_MS     4000
#define NEU_CLOSED_MS   180
#define NEU_FRAME_MS    50
#define NEU_LID_STEP    2

static int neu_half_width(int rx, int ry, int dy)
{
    const int dy2 = dy * dy;
    const int ry2 = ry * ry;
    if (dy2 > ry2) {
        return -1;
    }
    const int64_t target = (int64_t)rx * rx * (ry2 - dy2);
    int dx = 0;
    while ((int64_t)(dx + 1) * (dx + 1) * ry2 <= target) {
        dx++;
    }
    return dx;
}

static void neu_row(int cx, int y, int rx, int ry, int cy, uint16_t color)
{
    if (y < 0 || y >= TFT_HEIGHT) {
        return;
    }
    const int dx = neu_half_width(rx, ry, y - cy);
    if (dx < 0) {
        return;
    }
    const int x = cx - dx;
    const int w = (dx * 2) + 1;
    if (x < 0 || x + w > TFT_WIDTH) {
        return;
    }
    st7735_fill_rect(x, y, w, 1, color);
}

static void neu_oval_band(int cx, int cy, int rx, int ry, int y_top, int y_bot, uint16_t color)
{
    if (y_top > y_bot) {
        return;
    }
    for (int y = y_top; y <= y_bot; y++) {
        neu_row(cx, y, rx, ry, cy, color);
    }
}

/* Draw top + bottom lid strips interleaved so both sides move together visually. */
static void neu_lid_strips(int cx, int cy, int rx, int ry,
                           int top_y0, int top_y1, int bot_y0, int bot_y1,
                           uint16_t color)
{
    if (top_y0 > top_y1 && bot_y0 > bot_y1) {
        return;
    }
    int top_y = top_y0;
    int bot_y = bot_y1;
    while (top_y <= top_y1 || bot_y >= bot_y0) {
        if (top_y <= top_y1) {
            neu_row(cx, top_y, rx, ry, cy, color);
            top_y++;
        }
        if (bot_y >= bot_y0) {
            neu_row(cx, bot_y, rx, ry, cy, color);
            bot_y--;
        }
    }
}

static void neu_oval_full(int cx, int cy, int rx, int ry, uint16_t color)
{
    neu_oval_band(cx, cy, rx, ry, cy - ry, cy + ry, color);
}

static void neu_delay_ms(int ms, tft_neutral_should_run_fn should_run)
{
    int left = ms;
    while (left > 0) {
        if (!should_run()) {
            return;
        }
        const int slice = (left > 25) ? 25 : left;
        vTaskDelay(pdMS_TO_TICKS(slice));
        left -= slice;
    }
}

static void neu_blink_once(tft_neutral_should_run_fn should_run)
{
    int open = NEU_RY;

    neu_oval_full(NEU_CX, NEU_CY, NEU_RX, NEU_RY, NEU_BLACK);
    neu_delay_ms(NEU_HOLD_MS, should_run);
    if (!should_run()) {
        return;
    }

    /* Close lids: white-out only the new top/bottom strip each step. */
    while (open > 0) {
        if (!should_run()) {
            return;
        }
        const int prev = open;
        open -= NEU_LID_STEP;
        if (open < 0) {
            open = 0;
        }
        neu_lid_strips(NEU_CX, NEU_CY, NEU_RX, NEU_RY,
                       NEU_CY - prev, NEU_CY - open - 1,
                       NEU_CY + open + 1, NEU_CY + prev,
                       NEU_WHITE);
        neu_delay_ms(NEU_FRAME_MS, should_run);
    }

    neu_oval_full(NEU_CX, NEU_CY, NEU_RX, NEU_RY, NEU_WHITE);
    neu_delay_ms(NEU_CLOSED_MS, should_run);
    if (!should_run()) {
        return;
    }

    /* Open lids: black-in only the new top/bottom strip each step. */
    open = 0;
    while (open < NEU_RY) {
        if (!should_run()) {
            return;
        }
        const int prev = open;
        open += NEU_LID_STEP;
        if (open > NEU_RY) {
            open = NEU_RY;
        }
        neu_lid_strips(NEU_CX, NEU_CY, NEU_RX, NEU_RY,
                       NEU_CY - open, NEU_CY - prev - 1,
                       NEU_CY + prev + 1, NEU_CY + open,
                       NEU_BLACK);
        /* First open frame: top/bottom strips miss the horizontal center row. */
        if (prev == 0 && open > 0) {
            neu_row(NEU_CX, NEU_CY, NEU_RX, NEU_RY, NEU_CY, NEU_BLACK);
        }
        neu_delay_ms(NEU_FRAME_MS, should_run);
    }
}

void tft_neutral_run(tft_neutral_should_run_fn should_run)
{
    if (should_run == NULL) {
        return;
    }

    st7735_fill_screen(NEU_WHITE);

    while (should_run()) {
        neu_blink_once(should_run);
    }
}

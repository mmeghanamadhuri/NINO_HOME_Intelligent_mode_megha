#include "st7735.h"

#include <string.h>
#include "sdkconfig.h"
#include "driver/gpio.h"
#include "driver/spi_master.h"
#include "esp_check.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

static const char *TAG = "st7735";

/* Memory access control — 0x00 portrait; use 0xC0 (MX|MY) if upside-down. */
#define ST7735_MADCTL           0x00

#define ST7735_CMD_SWRESET      0x01
#define ST7735_CMD_SLPOUT       0x11
#define ST7735_CMD_NORON        0x13
#define ST7735_CMD_INVOFF       0x20
#define ST7735_CMD_DISPON       0x29
#define ST7735_CMD_CASET        0x2A
#define ST7735_CMD_RASET        0x2B
#define ST7735_CMD_RAMWR        0x2C
#define ST7735_CMD_MADCTL       0x36
#define ST7735_CMD_COLMOD       0x3A
#define ST7735_CMD_FRMCTR1      0xB1
#define ST7735_CMD_FRMCTR2      0xB2
#define ST7735_CMD_FRMCTR3      0xB3
#define ST7735_CMD_INVCTR       0xB4
#define ST7735_CMD_PWCTR1       0xC0
#define ST7735_CMD_PWCTR2       0xC1
#define ST7735_CMD_PWCTR3       0xC2
#define ST7735_CMD_PWCTR4       0xC3
#define ST7735_CMD_PWCTR5       0xC4
#define ST7735_CMD_VMCTR1       0xC5
#define ST7735_CMD_GMCTRP1      0xE0
#define ST7735_CMD_GMCTRN1      0xE1

#define SPI_HOST_ID             SPI2_HOST
#define SPI_CLOCK_HZ            (26 * 1000 * 1000)
#define CHUNK_PIXELS            2048

static spi_device_handle_t s_spi;
static int s_target = ST7735_TARGET_ALL;
static uint8_t s_color_chunk[CHUNK_PIXELS * 2];

static void cs_idle(void)
{
    gpio_set_level(TFT_PIN_CS0, 1);
    gpio_set_level(TFT_PIN_CS1, 1);
}

static void cs_begin(void)
{
    if (s_target == ST7735_TARGET_ALL) {
        gpio_set_level(TFT_PIN_CS0, 0);
        gpio_set_level(TFT_PIN_CS1, 0);
    } else if (s_target == ST7735_TARGET_LEFT) {
        gpio_set_level(TFT_PIN_CS0, 0);
        gpio_set_level(TFT_PIN_CS1, 1);
    } else {
        gpio_set_level(TFT_PIN_CS0, 1);
        gpio_set_level(TFT_PIN_CS1, 0);
    }
}

static void cs_end(void)
{
    cs_idle();
}

static void bus_tx(const void *data, size_t bits, int dc_level)
{
    gpio_set_level(TFT_PIN_DC, dc_level);
    spi_transaction_t trans = {
        .length = bits,
        .tx_buffer = data,
    };
    cs_begin();
    ESP_ERROR_CHECK(spi_device_polling_transmit(s_spi, &trans));
    cs_end();
}

static void dev_write_cmd(uint8_t cmd)
{
    bus_tx(&cmd, 8, 0);
}

static void dev_write_data(const uint8_t *data, size_t len)
{
    if (len == 0) {
        return;
    }
    bus_tx(data, len * 8, 1);
}

static void dev_write_data_byte(uint8_t value)
{
    dev_write_data(&value, 1);
}

static void dev_set_window(int x0, int y0, int x1, int y1)
{
    x0 += ST7735_XSTART;
    x1 += ST7735_XSTART;
    y0 += ST7735_YSTART;
    y1 += ST7735_YSTART;

    dev_write_cmd(ST7735_CMD_CASET);
    dev_write_data_byte(0x00);
    dev_write_data_byte((uint8_t)x0);
    dev_write_data_byte(0x00);
    dev_write_data_byte((uint8_t)x1);

    dev_write_cmd(ST7735_CMD_RASET);
    dev_write_data_byte(0x00);
    dev_write_data_byte((uint8_t)y0);
    dev_write_data_byte(0x00);
    dev_write_data_byte((uint8_t)y1);

    dev_write_cmd(ST7735_CMD_RAMWR);
}

void st7735_target(int target)
{
    if (target != ST7735_TARGET_ALL && (target < 0 || target >= TFT_COUNT)) {
        return;
    }
    s_target = target;
}

int st7735_get_target(void)
{
    return s_target;
}

static esp_err_t init_spi_bus(void)
{
    gpio_config_t cs_io = {
        .pin_bit_mask = (1ULL << TFT_PIN_CS0) | (1ULL << TFT_PIN_CS1),
        .mode = GPIO_MODE_OUTPUT,
        .pull_up_en = GPIO_PULLUP_DISABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type = GPIO_INTR_DISABLE,
    };
    ESP_RETURN_ON_ERROR(gpio_config(&cs_io), TAG, "cs gpio failed");
    cs_idle();

    spi_bus_config_t buscfg = {
        .mosi_io_num = TFT_PIN_MOSI,
        .miso_io_num = -1,
        .sclk_io_num = TFT_PIN_SCLK,
        .quadwp_io_num = -1,
        .quadhd_io_num = -1,
        .max_transfer_sz = TFT_WIDTH * TFT_HEIGHT * 2,
    };

    esp_err_t err = spi_bus_initialize(SPI_HOST_ID, &buscfg, SPI_DMA_CH_AUTO);
    if (err != ESP_OK && err != ESP_ERR_INVALID_STATE) {
        return err;
    }

    if (s_spi != NULL) {
        spi_bus_remove_device(s_spi);
        s_spi = NULL;
    }

    spi_device_interface_config_t devcfg = {
        .clock_speed_hz = SPI_CLOCK_HZ,
        .mode = 0,
        .spics_io_num = -1,
        .queue_size = 1,
        .flags = SPI_DEVICE_NO_DUMMY,
    };
    return spi_bus_add_device(SPI_HOST_ID, &devcfg, &s_spi);
}

static void hardware_reset(void)
{
    gpio_set_level(TFT_PIN_RST, 1);
    vTaskDelay(pdMS_TO_TICKS(10));
    gpio_set_level(TFT_PIN_RST, 0);
    vTaskDelay(pdMS_TO_TICKS(20));
    gpio_set_level(TFT_PIN_RST, 1);
    vTaskDelay(pdMS_TO_TICKS(120));
}

static void backlight_on(void)
{
#if !CONFIG_NINO_ST7735_BL_HARDCODED_3V3
    gpio_set_level(TFT_PIN_BL, 1);
#endif
}

static void init_panel(void)
{
    dev_write_cmd(ST7735_CMD_SWRESET);
    vTaskDelay(pdMS_TO_TICKS(150));

    dev_write_cmd(ST7735_CMD_SLPOUT);
    vTaskDelay(pdMS_TO_TICKS(120));

    dev_write_cmd(ST7735_CMD_MADCTL);
    dev_write_data_byte(ST7735_MADCTL);

    dev_write_cmd(ST7735_CMD_COLMOD);
    dev_write_data_byte(0x05);

    dev_write_cmd(ST7735_CMD_FRMCTR1);
    dev_write_data_byte(0x01);
    dev_write_data_byte(0x2C);
    dev_write_data_byte(0x2D);

    dev_write_cmd(ST7735_CMD_FRMCTR2);
    dev_write_data_byte(0x01);
    dev_write_data_byte(0x2C);
    dev_write_data_byte(0x2D);

    dev_write_cmd(ST7735_CMD_FRMCTR3);
    dev_write_data_byte(0x01);
    dev_write_data_byte(0x2C);
    dev_write_data_byte(0x2D);
    dev_write_data_byte(0x01);
    dev_write_data_byte(0x2C);
    dev_write_data_byte(0x2D);

    dev_write_cmd(ST7735_CMD_INVCTR);
    dev_write_data_byte(0x07);

    dev_write_cmd(ST7735_CMD_PWCTR1);
    dev_write_data_byte(0xA2);
    dev_write_data_byte(0x02);
    dev_write_data_byte(0x84);

    dev_write_cmd(ST7735_CMD_PWCTR2);
    dev_write_data_byte(0xC5);

    dev_write_cmd(ST7735_CMD_PWCTR3);
    dev_write_data_byte(0x0A);
    dev_write_data_byte(0x00);

    dev_write_cmd(ST7735_CMD_PWCTR4);
    dev_write_data_byte(0x8A);
    dev_write_data_byte(0x2A);

    dev_write_cmd(ST7735_CMD_PWCTR5);
    dev_write_data_byte(0x8A);
    dev_write_data_byte(0xEE);

    dev_write_cmd(ST7735_CMD_VMCTR1);
    dev_write_data_byte(0x0E);

    dev_write_cmd(ST7735_CMD_INVOFF);

    dev_write_cmd(ST7735_CMD_GMCTRP1);
    dev_write_data((const uint8_t[]){
        0x02, 0x1C, 0x07, 0x12, 0x37, 0x32, 0x29, 0x2D,
        0x29, 0x25, 0x2B, 0x39, 0x00, 0x01, 0x03, 0x10,
    }, 16);

    dev_write_cmd(ST7735_CMD_GMCTRN1);
    dev_write_data((const uint8_t[]){
        0x03, 0x1D, 0x07, 0x06, 0x2E, 0x2C, 0x29, 0x2D,
        0x2E, 0x2E, 0x37, 0x3F, 0x00, 0x00, 0x02, 0x10,
    }, 16);

    dev_write_cmd(ST7735_CMD_NORON);
    vTaskDelay(pdMS_TO_TICKS(10));

    dev_write_cmd(ST7735_CMD_DISPON);
    vTaskDelay(pdMS_TO_TICKS(100));
}

esp_err_t st7735_init(void)
{
    uint64_t gpio_mask = (1ULL << TFT_PIN_DC) | (1ULL << TFT_PIN_RST);
#if !CONFIG_NINO_ST7735_BL_HARDCODED_3V3
    gpio_mask |= (1ULL << TFT_PIN_BL);
#endif
    gpio_config_t io = {
        .pin_bit_mask = gpio_mask,
        .mode = GPIO_MODE_OUTPUT,
        .pull_up_en = GPIO_PULLUP_DISABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type = GPIO_INTR_DISABLE,
    };
    ESP_RETURN_ON_ERROR(gpio_config(&io), TAG, "gpio config failed");

    gpio_set_level(TFT_PIN_DC, 1);
    gpio_set_level(TFT_PIN_RST, 1);
#if !CONFIG_NINO_ST7735_BL_HARDCODED_3V3
    gpio_set_level(TFT_PIN_BL, 0);
#endif

    ESP_RETURN_ON_ERROR(init_spi_bus(), TAG, "spi init failed");

    hardware_reset();

    for (int i = 0; i < TFT_COUNT; i++) {
        const int saved = s_target;
        s_target = i;
        init_panel();
        s_target = saved;
        ESP_LOGI(TAG, "panel %d init done", i);
    }

    backlight_on();

    s_target = ST7735_TARGET_ALL;
    st7735_fill_screen(0x0000);

#if CONFIG_NINO_ST7735_BL_HARDCODED_3V3
    ESP_LOGI(TAG, "ST7735 ready: %d panel(s) %dx%d (BL hardwired 3.3 V, GPIO%d free for SDIO)",
             TFT_COUNT, TFT_WIDTH, TFT_HEIGHT, TFT_PIN_BL);
#else
    ESP_LOGI(TAG, "ST7735 ready: %d panel(s) %dx%d (BL GPIO%d)",
             TFT_COUNT, TFT_WIDTH, TFT_HEIGHT, TFT_PIN_BL);
#endif
    return ESP_OK;
}

void st7735_fill_rect(int x, int y, int w, int h, uint16_t color)
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
    if (x + w > TFT_WIDTH) {
        w = TFT_WIDTH - x;
    }
    if (y + h > TFT_HEIGHT) {
        h = TFT_HEIGHT - y;
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

    dev_set_window(x, y, x + w - 1, y + h - 1);

    size_t remaining = total;
    while (remaining > 0) {
        size_t batch = remaining > CHUNK_PIXELS ? CHUNK_PIXELS : remaining;
        dev_write_data(s_color_chunk, batch * 2);
        remaining -= batch;
    }
}

void st7735_fill_screen(uint16_t color)
{
    st7735_fill_rect(0, 0, TFT_WIDTH, TFT_HEIGHT, color);
}

void st7735_draw_pixel(int x, int y, uint16_t color)
{
    if (x < 0 || y < 0 || x >= TFT_WIDTH || y >= TFT_HEIGHT) {
        return;
    }

    const uint8_t bytes[2] = { (uint8_t)(color >> 8), (uint8_t)(color & 0xFF) };
    dev_set_window(x, y, x, y);
    dev_write_data(bytes, sizeof(bytes));
}

void st7735_draw_bitmap(int x, int y, int w, int h, const uint16_t *colors)
{
    if (colors == NULL || w <= 0 || h <= 0) {
        return;
    }

    if (x < 0 || y < 0 || x + w > TFT_WIDTH || y + h > TFT_HEIGHT) {
        return;
    }

    dev_set_window(x, y, x + w - 1, y + h - 1);

    for (int row = 0; row < h; row++) {
        const uint16_t *src = colors + (size_t)row * (size_t)w;
        size_t remaining = (size_t)w;
        size_t source_offset = 0;
        while (remaining > 0) {
            size_t batch = remaining > CHUNK_PIXELS ? CHUNK_PIXELS : remaining;
            for (size_t i = 0; i < batch; i++) {
                uint16_t c = src[source_offset + i];
                s_color_chunk[i * 2] = (uint8_t)(c >> 8);
                s_color_chunk[(i * 2) + 1] = (uint8_t)(c & 0xFF);
            }
            dev_write_data(s_color_chunk, batch * 2);
            source_offset += batch;
            remaining -= batch;
        }
    }
}

void st7735_draw_bitmap_stride(int x, int y, int w, int h,
                               const uint16_t *colors, int stride_px)
{
    if (colors == NULL || w <= 0 || h <= 0 || stride_px < w) {
        return;
    }

    if (x < 0 || y < 0 || x + w > TFT_WIDTH || y + h > TFT_HEIGHT) {
        return;
    }

    dev_set_window(x, y, x + w - 1, y + h - 1);

    for (int row = 0; row < h; row++) {
        const uint16_t *src = colors + (size_t)row * (size_t)stride_px;
        size_t remaining = (size_t)w;
        size_t source_offset = 0;
        while (remaining > 0) {
            size_t batch = remaining > CHUNK_PIXELS ? CHUNK_PIXELS : remaining;
            for (size_t i = 0; i < batch; i++) {
                uint16_t c = src[source_offset + i];
                s_color_chunk[i * 2] = (uint8_t)(c >> 8);
                s_color_chunk[(i * 2) + 1] = (uint8_t)(c & 0xFF);
            }
            dev_write_data(s_color_chunk, batch * 2);
            source_offset += batch;
            remaining -= batch;
        }
    }
}

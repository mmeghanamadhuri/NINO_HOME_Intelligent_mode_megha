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
    ESP_RETURN_ON_ERROR(gpio_config(&io), TAG, "gpio config failed");

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

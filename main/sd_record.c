#include "sd_record.h"

#include <inttypes.h>
#include <stdio.h>
#include <string.h>

#include "bsp/esp32_p4_function_ev_board.h"
#include "esp_log.h"

static const char *TAG = "sd_rec";

static bool s_mounted;

esp_err_t nino_sd_record_init(void) {
  if (s_mounted) {
    return ESP_OK;
  }

  static const esp_vfs_fat_sdmmc_mount_config_t mount_config = {
      .format_if_mount_failed = true,
      .max_files = 5,
      .allocation_unit_size = 16 * 1024,
  };
  bsp_sdcard_cfg_t cfg = {
      .mount = &mount_config,
  };

  esp_err_t err = bsp_sdcard_sdmmc_mount(&cfg);
  if (err != ESP_OK) {
    ESP_LOGE(TAG,
             "SD mount failed: %s — check card is in the board slot, FAT32/exFAT, "
             "and fully inserted",
             esp_err_to_name(err));
    return err;
  }

  s_mounted = true;
  sdmmc_card_t *card = bsp_sdcard_get_handle();
  if (card != NULL) {
    ESP_LOGI(TAG, "SD ready at %s (%s)", BSP_SD_MOUNT_POINT, card->cid.name);
  } else {
    ESP_LOGI(TAG, "SD ready at %s", BSP_SD_MOUNT_POINT);
  }
  return ESP_OK;
}

bool nino_sd_record_ready(void) { return s_mounted; }

esp_err_t nino_sd_record_save_wav(const uint8_t *wav, size_t len, char *out_path,
                                  size_t out_path_len) {
  if (!s_mounted || wav == NULL || len < 44 || out_path == NULL || out_path_len == 0) {
    return ESP_ERR_INVALID_ARG;
  }

  static uint32_t s_seq;
  s_seq++;
  int n = snprintf(out_path, out_path_len, BSP_SD_MOUNT_POINT "/rec_%04" PRIu32 ".wav", s_seq);
  if (n < 0 || (size_t)n >= out_path_len) {
    return ESP_ERR_INVALID_SIZE;
  }

  FILE *f = fopen(out_path, "wb");
  if (f == NULL) {
    ESP_LOGE(TAG, "fopen(%s) failed", out_path);
    return ESP_FAIL;
  }

  const size_t written = fwrite(wav, 1, len, f);
  fclose(f);
  if (written != len) {
    ESP_LOGE(TAG, "WAV write incomplete (%u / %u bytes)", (unsigned)written, (unsigned)len);
    return ESP_FAIL;
  }

  ESP_LOGI(TAG, "Saved %u-byte 16 kHz mono WAV to %s", (unsigned)len, out_path);
  return ESP_OK;
}

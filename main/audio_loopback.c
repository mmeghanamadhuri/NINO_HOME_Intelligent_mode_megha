#include "audio_loopback.h"

#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"

static const char *TAG = "loopback";

static SemaphoreHandle_t s_pause_mu;
static volatile int s_pause_count;
static volatile bool s_paused;
static volatile bool s_in_io;
static volatile bool s_ready;

static void ensure_pause_mu(void) {
  if (s_pause_mu != NULL) {
    return;
  }
  s_pause_mu = xSemaphoreCreateMutex();
}

esp_err_t nino_audio_loopback_start(void) {
  ensure_pause_mu();
  if (s_pause_mu == NULL) {
    return ESP_ERR_NO_MEM;
  }
  s_ready = true;
  ESP_LOGI(TAG, "Onboard mic loopback disabled — Aux-in is capture-only");
  return ESP_OK;
}

void nino_audio_loopback_pause(void) {
  ensure_pause_mu();
  if (s_pause_mu != NULL) {
    xSemaphoreTake(s_pause_mu, portMAX_DELAY);
    s_pause_count++;
    s_paused = true;
    xSemaphoreGive(s_pause_mu);
  } else {
    s_paused = true;
  }

  int spins = 0;
  while (s_in_io && spins++ < 50) {
    vTaskDelay(pdMS_TO_TICKS(4));
  }
}

void nino_audio_loopback_resume(void) {
  ensure_pause_mu();
  if (s_pause_mu != NULL) {
    xSemaphoreTake(s_pause_mu, portMAX_DELAY);
    if (s_pause_count > 0) {
      s_pause_count--;
    }
    if (s_pause_count == 0) {
      s_paused = false;
    }
    xSemaphoreGive(s_pause_mu);
  } else {
    s_paused = false;
  }
}

bool nino_audio_loopback_is_paused(void) { return s_paused; }

bool nino_audio_loopback_is_running(void) { return s_ready && !s_paused; }

void nino_audio_input_mark_busy(bool busy) { s_in_io = busy; }

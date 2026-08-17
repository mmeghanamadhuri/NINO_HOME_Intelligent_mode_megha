#include "audio_loopback.h"

#include <stdint.h>

#include "audio_playback.h"
#include "mic_input.h"

#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"

static const char *TAG = "loopback";

#define LOOPBACK_RATE_HZ 16000
#define LOOPBACK_FRAME_MS 20
#define LOOPBACK_FRAME_SAMPLES ((LOOPBACK_RATE_HZ * LOOPBACK_FRAME_MS) / 1000)
/* Soften playback vs mic to limit speaker→mic howling. */
#define LOOPBACK_PLAY_GAIN_NUM 3
#define LOOPBACK_PLAY_GAIN_DEN 8

static SemaphoreHandle_t s_pause_mu;
static volatile int s_pause_count;
static volatile bool s_paused;
static volatile bool s_in_io;
static volatile bool s_started;

static void attenuate_frame(int16_t *samples, int count) {
  for (int i = 0; i < count; i++) {
    samples[i] = (int16_t)(((int32_t)samples[i] * LOOPBACK_PLAY_GAIN_NUM) /
                           LOOPBACK_PLAY_GAIN_DEN);
  }
}

static void loopback_task(void *arg) {
  (void)arg;
  int16_t frame[LOOPBACK_FRAME_SAMPLES];

  ESP_LOGI(TAG, "Onboard ES8311 loopback running — speak into the mic");

  while (true) {
    if (s_paused) {
      s_in_io = false;
      vTaskDelay(pdMS_TO_TICKS(10));
      continue;
    }

    nino_audio_bus_lock();
    if (s_paused) {
      nino_audio_bus_unlock();
      continue;
    }

    s_in_io = true;
    /* Keep speaker open first so a rate reopen does not drop the ADC mid-frame. */
    esp_err_t err =
        nino_audio_write_pcm16_mono_locked(NULL, 0, LOOPBACK_RATE_HZ);
    if (err == ESP_OK) {
      err = nino_mic_read_locked(frame, LOOPBACK_FRAME_SAMPLES);
    }
    if (err == ESP_OK) {
      attenuate_frame(frame, LOOPBACK_FRAME_SAMPLES);
      err = nino_audio_write_pcm16_mono_locked(frame, LOOPBACK_FRAME_SAMPLES,
                                               LOOPBACK_RATE_HZ);
    }
    if (err != ESP_OK) {
      ESP_LOGW(TAG, "Loopback frame failed: %s", esp_err_to_name(err));
      nino_audio_bus_unlock();
      s_in_io = false;
      vTaskDelay(pdMS_TO_TICKS(20));
      continue;
    }
    nino_audio_bus_unlock();
    s_in_io = false;
  }
}

esp_err_t nino_audio_loopback_start(void) {
  if (s_started) {
    return ESP_OK;
  }

  if (s_pause_mu == NULL) {
    s_pause_mu = xSemaphoreCreateMutex();
    if (s_pause_mu == NULL) {
      return ESP_ERR_NO_MEM;
    }
  }

  nino_audio_bus_lock();
  esp_err_t err = nino_audio_write_pcm16_mono_locked(NULL, 0, LOOPBACK_RATE_HZ);
  if (err == ESP_OK) {
    int16_t silence[LOOPBACK_FRAME_SAMPLES] = {0};
    err = nino_audio_write_pcm16_mono_locked(silence, LOOPBACK_FRAME_SAMPLES,
                                             LOOPBACK_RATE_HZ);
  }
  if (err == ESP_OK) {
    err = nino_mic_read_locked(NULL, 0);
  }
  nino_audio_bus_unlock();
  if (err != ESP_OK) {
    ESP_LOGE(TAG, "Failed to open ES8311 duplex path: %s", esp_err_to_name(err));
    return err;
  }

  BaseType_t ok =
      xTaskCreate(loopback_task, "mic_loopback", 4096, NULL, 7, NULL);
  if (ok != pdPASS) {
    ESP_LOGE(TAG, "Failed to create loopback task");
    return ESP_ERR_NO_MEM;
  }

  s_started = true;
  ESP_LOGI(TAG, "ES8311 mic → speaker loopback started (16 kHz)");
  return ESP_OK;
}

void nino_audio_loopback_pause(void) {
  if (!s_started) {
    return;
  }
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
  if (!s_started) {
    return;
  }
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

bool nino_audio_loopback_is_running(void) { return s_started && !s_paused; }

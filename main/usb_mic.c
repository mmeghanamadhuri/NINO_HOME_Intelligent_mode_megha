#include "usb_mic.h"

#include <inttypes.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "sdkconfig.h"

#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/queue.h"
#include "freertos/stream_buffer.h"
#include "freertos/task.h"
#include "usb/uac_host.h"
#include "usb/usb_host.h"
#include "usb/usb_types_ch9.h"

#if CONFIG_USB4MIC_USB_PHY_ON_HEADER
#include "driver/gpio.h"
#include "esp_private/usb_phy.h"
#include "hal/usb_wrap_ll.h"
#include "soc/usb_wrap_struct.h"
#endif

static const char *TAG = "usb_mic";

#define USB_MIC_OUTPUT_RATE_HZ 16000
#define USB_MIC_RING_SECONDS 2
#define USB_MIC_RING_BYTES (USB_MIC_OUTPUT_RATE_HZ * USB_MIC_RING_SECONDS * (int)sizeof(int16_t))
#define USB_MIC_SW_GAIN_NUM 16
#define USB_MIC_SW_GAIN_DEN 1
#define USB_MIC_UAC_TASK_PRIO 5
#define USB_MIC_READ_TIMEOUT_MS 500

#define USB_MIC_MAX_BLOCKED_ADDRS 8
#define USB_MIC_OPEN_QUEUE_LEN 8
#define USB_MIC_OPEN_WORKER_STACK 4096
#define USB_MIC_OPEN_SETTLE_MS 120
#define USB_MIC_RETRY_MS 5000
#define USB_MIC_RX_POLL_MS 10
#define USB_MIC_NO_RX_CLOSE_MS 8000
#define USB_MIC_MAX_UAC_IFACES 4
#define FTDI_VID 0x0403
#define ROBOTIS_VID 0x16d0
#define RESPEAKER_VID 0x2886
#define RESPEAKER_PID 0x0018

typedef struct {
  uint8_t addr;
  uint8_t iface_num;
} usb_mic_connect_req_t;

typedef struct {
  uint8_t addr;
  uint8_t iface;
} usb_mic_fail_t;

static StreamBufferHandle_t s_pcm_ring;
static SemaphoreHandle_t s_ring_send_mutex;
static SemaphoreHandle_t s_read_mutex;
static SemaphoreHandle_t s_ready_mutex;
static SemaphoreHandle_t s_open_mutex;
static QueueHandle_t s_connect_queue;
static usb_host_client_handle_t s_uac_client;
static volatile bool s_mic_ready;
static volatile bool s_uac_started;
static uac_host_device_handle_t s_mic_dev;
static uint32_t s_stream_rate_hz;
static uint8_t s_stream_channels;
static uint8_t s_stream_subframe_size;
#if CONFIG_USB4MIC_USB_PHY_ON_HEADER
static usb_phy_handle_t s_hs_utmi_phy;
static usb_phy_handle_t s_fs_header_phy;
#endif
static uint8_t s_blocked_addrs[USB_MIC_MAX_BLOCKED_ADDRS];
static size_t s_blocked_addr_count;
static uint8_t s_active_addr;
static uint16_t s_active_vid;
static uint16_t s_active_pid;
static volatile uint32_t s_rx_chunks;
static volatile int32_t s_last_peak;
static volatile bool s_has_audio;
static int64_t s_uac_open_us;
static uint8_t s_active_iface;
static usb_mic_fail_t s_failed_opens[8];
static size_t s_failed_open_count;

static void set_mic_ready(bool ready);

static void note_rx_peak(const int16_t *samples, size_t count) {
  int32_t peak = 0;
  for (size_t i = 0; i < count; i++) {
    int32_t v = samples[i];
    if (v < 0) {
      v = -v;
    }
    if (v > peak) {
      peak = v;
    }
  }
  s_last_peak = peak;
  s_rx_chunks++;
  if (peak > 40) {
    if (!s_has_audio) {
      ESP_LOGI(TAG, "USB mic audio detected (peak=%ld)", (long)peak);
    }
    s_has_audio = true;
  }
  if (s_rx_chunks == 1 && s_mic_dev != NULL) {
    ESP_LOGI(TAG, "USB mic isochronous RX active (addr=%u iface=%u)", s_active_addr, s_active_iface);
  }
}

static void reset_mic_stream_state(void) {
  s_active_addr = 0;
  s_active_vid = 0;
  s_active_pid = 0;
  s_active_iface = 0;
  s_rx_chunks = 0;
  s_last_peak = 0;
  s_has_audio = false;
  s_uac_open_us = 0;
  set_mic_ready(false);
  if (s_pcm_ring != NULL) {
    xStreamBufferReset(s_pcm_ring);
  }
}

static uint32_t get_fallback_sample_freq(const uac_host_dev_alt_param_t *alt_params,
                                         uint32_t preferred) {
  if (alt_params->sample_freq_type > 0) {
    return alt_params->sample_freq[0];
  }

  uint32_t lower = alt_params->sample_freq_lower;
  uint32_t upper = alt_params->sample_freq_upper;

  if (lower == 0 && upper == 0) {
    return 0;
  }
  if (lower == 0) {
    return upper;
  }
  if (upper == 0) {
    return lower;
  }
  if (lower > upper) {
    const uint32_t tmp = lower;
    lower = upper;
    upper = tmp;
  }

  if (preferred == 0 || preferred < lower) {
    return lower;
  }
  if (preferred > upper) {
    return upper;
  }
  return preferred;
}

static bool find_dev_alt_params_for_freq(uac_host_device_handle_t handle, uint32_t freq,
                                         uac_host_dev_alt_param_t *out) {
  uac_host_dev_info_t info;
  if (uac_host_get_device_info(handle, &info) != ESP_OK) {
    return false;
  }
  for (uint8_t alt = 1; alt <= info.iface_alt_num; alt++) {
    uac_host_dev_alt_param_t p;
    if (uac_host_get_device_alt_param(handle, alt, &p) != ESP_OK) {
      continue;
    }
    bool match = false;
    if (p.sample_freq_type > 0) {
      for (int i = 0; i < p.sample_freq_type; i++) {
        if (p.sample_freq[i] == freq) {
          match = true;
          break;
        }
      }
    } else {
      match = (freq >= p.sample_freq_lower && freq <= p.sample_freq_upper);
    }
    if (match) {
      *out = p;
      return true;
    }
  }
  return false;
}

static bool addr_is_blocked(uint8_t addr) {
  for (size_t i = 0; i < s_blocked_addr_count; i++) {
    if (s_blocked_addrs[i] == addr) {
      return true;
    }
  }
  return false;
}

static void addr_mark_blocked(uint8_t addr) {
  if (addr == 0 || addr_is_blocked(addr)) {
    return;
  }
  if (s_blocked_addr_count >= USB_MIC_MAX_BLOCKED_ADDRS) {
    return;
  }
  s_blocked_addrs[s_blocked_addr_count++] = addr;
}

void usb_mic_block_dev_addr(uint8_t dev_addr) {
  if (dev_addr == 0) {
    return;
  }
  addr_mark_blocked(dev_addr);
  ESP_LOGI(TAG, "Blocking UAC on USB addr=%u (not the GPIO header mic)", dev_addr);
  /* Only tear down UAC if this addr is the one we opened — blocking the J18 camera
   * must not close the GPIO-header ReSpeaker on a different address. */
  if (s_mic_dev != NULL && s_active_addr == dev_addr) {
    ESP_LOGW(TAG, "Closing UAC handle on blocked addr=%u", dev_addr);
    uac_host_device_handle_t dev = s_mic_dev;
    s_mic_dev = NULL;
    s_active_addr = 0;
    s_active_iface = 0;
    set_mic_ready(false);
    (void)uac_host_device_close(dev);
  }
}

static bool device_has_uvc_interface(uint8_t addr) {
  if (s_uac_client == NULL) {
    return false;
  }
  usb_device_handle_t dev_hdl = NULL;
  if (usb_host_device_open(s_uac_client, addr, &dev_hdl) != ESP_OK) {
    return false;
  }

  const usb_config_desc_t *cfg = NULL;
  bool has_uvc = false;
  if (usb_host_get_active_config_descriptor(dev_hdl, &cfg) == ESP_OK && cfg != NULL) {
    const uint8_t *ptr = (const uint8_t *)cfg;
    const uint8_t *end = ptr + cfg->wTotalLength;
    ptr += cfg->bLength;
    while (ptr < end) {
      if (ptr + 1 >= end) {
        break;
      }
      const uint8_t desc_len = ptr[0];
      const uint8_t desc_type = ptr[1];
      if (desc_len < 2 || ptr + desc_len > end) {
        break;
      }
      if (desc_type == USB_B_DESCRIPTOR_TYPE_INTERFACE && desc_len >= 9) {
        const usb_intf_desc_t *intf = (const usb_intf_desc_t *)ptr;
        if (intf->bInterfaceClass == USB_CLASS_VIDEO) {
          has_uvc = true;
          break;
        }
      }
      ptr += desc_len;
    }
  }

  usb_host_device_close(s_uac_client, dev_hdl);
  return has_uvc;
}

static bool read_usb_ids(usb_device_handle_t hdl, uint16_t *vid, uint16_t *pid,
                         uint8_t *dev_class) {
  if (hdl == NULL) {
    return false;
  }
  const usb_device_desc_t *desc = NULL;
  if (usb_host_get_device_descriptor(hdl, &desc) != ESP_OK || desc == NULL) {
    return false;
  }
  if (vid) {
    *vid = desc->idVendor;
  }
  if (pid) {
    *pid = desc->idProduct;
  }
  if (dev_class) {
    *dev_class = desc->bDeviceClass;
  }
  return true;
}

static bool peek_usb_ids(uint8_t addr, uint16_t *vid, uint16_t *pid, uint8_t *dev_class) {
  if (s_uac_client == NULL) {
    return false;
  }
  usb_device_handle_t hdl = NULL;
  if (usb_host_device_open(s_uac_client, addr, &hdl) != ESP_OK) {
    return false;
  }
  const bool ok = read_usb_ids(hdl, vid, pid, dev_class);
  usb_host_device_close(s_uac_client, hdl);
  return ok;
}

static bool device_is_known_non_mic(uint8_t addr) {
  if (addr_is_blocked(addr)) {
    return true;
  }

  uint16_t vid = 0;
  uint16_t pid = 0;
  uint8_t dev_class = 0;
  if (!peek_usb_ids(addr, &vid, &pid, &dev_class)) {
    return false;
  }

  if (dev_class == USB_CLASS_HUB) {
    ESP_LOGI(TAG, "Skip UAC addr=%u — USB hub (%04x:%04x)", addr, vid, pid);
    return true;
  }

  /* J18 hub peripherals — not the GPIO-header mic. */
  if (vid == FTDI_VID || vid == ROBOTIS_VID || vid == 0x046d || vid == 0x1a40 || vid == 0x05e3 ||
      vid == 0x03eb) {
    ESP_LOGI(TAG, "Skip UAC addr=%u — hub device %04x:%04x (not header mic)", addr, vid, pid);
    return true;
  }

  if (device_has_uvc_interface(addr)) {
    ESP_LOGI(TAG, "Skip UAC addr=%u — UVC camera %04x:%04x", addr, vid, pid);
    return true;
  }

  return false;
}

static bool open_config_is_failed(uint8_t addr, uint8_t iface) {
  for (size_t i = 0; i < s_failed_open_count; i++) {
    if (s_failed_opens[i].addr == addr && s_failed_opens[i].iface == iface) {
      return true;
    }
  }
  return false;
}

static void mark_open_config_failed(uint8_t addr, uint8_t iface) {
  if (open_config_is_failed(addr, iface)) {
    return;
  }
  if (s_failed_open_count >= sizeof(s_failed_opens) / sizeof(s_failed_opens[0])) {
    return;
  }
  s_failed_opens[s_failed_open_count++] = (usb_mic_fail_t){.addr = addr, .iface = iface};
  ESP_LOGW(TAG, "Mark UAC addr=%u iface=%u failed (will try other interfaces)", addr, iface);
}

static int list_uac_input_ifaces(uint8_t addr, uint8_t *ifaces, int max_ifaces) {
  if (s_uac_client == NULL || ifaces == NULL || max_ifaces <= 0) {
    return 0;
  }
  usb_device_handle_t dev_hdl = NULL;
  if (usb_host_device_open(s_uac_client, addr, &dev_hdl) != ESP_OK) {
    return 0;
  }

  int count = 0;
  const usb_config_desc_t *cfg = NULL;
  if (usb_host_get_active_config_descriptor(dev_hdl, &cfg) == ESP_OK && cfg != NULL) {
    const uint8_t *ptr = (const uint8_t *)cfg;
    const uint8_t *end = ptr + cfg->wTotalLength;
    ptr += cfg->bLength;
    while (ptr < end) {
      if (ptr + 1 >= end) {
        break;
      }
      const uint8_t desc_len = ptr[0];
      const uint8_t desc_type = ptr[1];
      if (desc_len < 2 || ptr + desc_len > end) {
        break;
      }
      if (desc_type == USB_B_DESCRIPTOR_TYPE_INTERFACE && desc_len >= 9) {
        const usb_intf_desc_t *intf = (const usb_intf_desc_t *)ptr;
        if (intf->bInterfaceClass == USB_CLASS_AUDIO && intf->bInterfaceSubClass == 0x02) {
          bool dup = false;
          for (int i = 0; i < count; i++) {
            if (ifaces[i] == intf->bInterfaceNumber) {
              dup = true;
              break;
            }
          }
          if (!dup && count < max_ifaces) {
            ifaces[count++] = intf->bInterfaceNumber;
          }
        }
      }
      ptr += desc_len;
    }
  }

  usb_host_device_close(s_uac_client, dev_hdl);
  return count;
}

static bool find_uac_input_iface(uint8_t addr, uint8_t *iface_out) {
  uint8_t ifaces[USB_MIC_MAX_UAC_IFACES];
  const int n = list_uac_input_ifaces(addr, ifaces, USB_MIC_MAX_UAC_IFACES);
  if (n <= 0 || iface_out == NULL) {
    return false;
  }
  for (int i = n - 1; i >= 0; i--) {
    if (!open_config_is_failed(addr, ifaces[i])) {
      *iface_out = ifaces[i];
      return true;
    }
  }
  return false;
}

static esp_err_t uac_client_init(void) {
  if (s_uac_client != NULL) {
    return ESP_OK;
  }
  const usb_host_client_config_t client_config = {
      .is_synchronous = true,
      .max_num_event_msg = 8,
      .async = {
          .client_event_callback = NULL,
          .callback_arg = NULL,
      },
  };
  return usb_host_client_register(&client_config, &s_uac_client);
}

static void close_mic_device(uac_host_device_handle_t mic) {
  if (mic == NULL) {
    return;
  }
  esp_err_t err = uac_host_device_close(mic);
  if (err != ESP_OK) {
    ESP_LOGW(TAG, "uac_host_device_close: %s", esp_err_to_name(err));
  }
  vTaskDelay(pdMS_TO_TICKS(20));
}

static bool try_start_mic(uac_host_device_handle_t mic, uac_host_dev_alt_param_t *alt_out,
                          uint32_t *freq_out) {
  uac_host_dev_info_t info;
  if (uac_host_get_device_info(mic, &info) != ESP_OK || info.iface_alt_num == 0) {
    return false;
  }

  for (uint8_t a = 1; a <= info.iface_alt_num; a++) {
    uac_host_dev_alt_param_t alt = {};
    if (uac_host_get_device_alt_param(mic, a, &alt) != ESP_OK) {
      continue;
    }
    if (alt.channels == 0 || alt.subframe_size == 0) {
      continue;
    }

    uint32_t mic_freq = USB_MIC_OUTPUT_RATE_HZ;
    bool rate_ok = false;
    if (alt.sample_freq_type > 0) {
      for (int fi = 0; fi < alt.sample_freq_type; fi++) {
        if (alt.sample_freq[fi] == USB_MIC_OUTPUT_RATE_HZ) {
          rate_ok = true;
          break;
        }
      }
      if (!rate_ok) {
        mic_freq = alt.sample_freq[0];
      }
    } else if (alt.sample_freq_lower <= USB_MIC_OUTPUT_RATE_HZ &&
               USB_MIC_OUTPUT_RATE_HZ <= alt.sample_freq_upper) {
      rate_ok = true;
    } else {
      mic_freq = get_fallback_sample_freq(&alt, USB_MIC_OUTPUT_RATE_HZ);
      rate_ok = mic_freq != 0;
    }
    if (!rate_ok && mic_freq == 0) {
      continue;
    }

    const uac_host_stream_config_t stream_cfg = {
        .channels = alt.channels,
        .bit_resolution = alt.bit_resolution,
        .sample_freq = mic_freq,
    };

    if (uac_host_device_start(mic, &stream_cfg) != ESP_OK) {
      ESP_LOGW(TAG, "UAC alt %u start failed (%u ch @ %" PRIu32 " Hz)", a, alt.channels, mic_freq);
      continue;
    }

    *alt_out = alt;
    *freq_out = mic_freq;
    return true;
  }

  return false;
}

static void set_mic_ready(bool ready) {
  if (s_ready_mutex != NULL) {
    xSemaphoreTake(s_ready_mutex, portMAX_DELAY);
  }
  s_mic_ready = ready;
  if (s_ready_mutex != NULL) {
    xSemaphoreGive(s_ready_mutex);
  }
}

static void apply_sw_gain(int16_t *samples, size_t count) {
  for (size_t i = 0; i < count; i++) {
    int32_t v = ((int32_t)samples[i] * USB_MIC_SW_GAIN_NUM) / USB_MIC_SW_GAIN_DEN;
    if (v > 32767) {
      v = 32767;
    } else if (v < -32768) {
      v = -32768;
    }
    samples[i] = (int16_t)v;
  }
}

static void push_output_samples(const int16_t *samples, size_t count) {
  if (s_pcm_ring == NULL || samples == NULL || count == 0) {
    return;
  }
  int16_t *tmp = (int16_t *)malloc(count * sizeof(int16_t));
  if (tmp == NULL) {
    return;
  }
  memcpy(tmp, samples, count * sizeof(int16_t));
  apply_sw_gain(tmp, count);
  note_rx_peak(tmp, count);
  const size_t bytes = count * sizeof(int16_t);
  if (s_ring_send_mutex != NULL) {
    xSemaphoreTake(s_ring_send_mutex, portMAX_DELAY);
  }
  const size_t sent = xStreamBufferSend(s_pcm_ring, tmp, bytes, 0);
  if (s_ring_send_mutex != NULL) {
    xSemaphoreGive(s_ring_send_mutex);
  }
  if (sent != bytes) {
    static uint32_t drop_log;
    drop_log++;
    if ((drop_log % 200U) == 1U) {
      ESP_LOGW(TAG, "PCM ring full — dropped %u bytes (consumer slow?)", (unsigned)(bytes - sent));
    }
  }
  free(tmp);
}

static void resample_to_16k_mono(const int16_t *in, size_t in_frames, uint8_t in_channels,
                                 uint32_t in_rate, int16_t *out, size_t *out_frames) {
  if (in_frames == 0 || in_channels == 0 || in_rate == 0 || out == NULL || out_frames == NULL) {
    *out_frames = 0;
    return;
  }

  size_t mono_frames = in_frames;
  int16_t *mono = (int16_t *)in;
  int16_t *mono_heap = NULL;

  if (in_channels > 1) {
    mono_heap = (int16_t *)malloc(in_frames * sizeof(int16_t));
    if (mono_heap == NULL) {
      *out_frames = 0;
      return;
    }
    /* ReSpeaker 6-ch firmware: ch0 = beamformed ASR audio; do not average all mics. */
    const uint8_t pick_ch =
        (in_channels == 6 && s_active_vid == RESPEAKER_VID && s_active_pid == RESPEAKER_PID) ? 0
                                                                                              : UINT8_MAX;
    for (size_t i = 0; i < in_frames; i++) {
      if (pick_ch != UINT8_MAX) {
        mono_heap[i] = in[i * in_channels + pick_ch];
      } else {
        int32_t sum = 0;
        for (uint8_t ch = 0; ch < in_channels; ch++) {
          sum += in[i * in_channels + ch];
        }
        mono_heap[i] = (int16_t)(sum / (int32_t)in_channels);
      }
    }
    mono = mono_heap;
  }

  if (in_rate == USB_MIC_OUTPUT_RATE_HZ) {
    memcpy(out, mono, mono_frames * sizeof(int16_t));
    *out_frames = mono_frames;
    free(mono_heap);
    return;
  }

  const size_t out_cap = (mono_frames * USB_MIC_OUTPUT_RATE_HZ / in_rate) + 2;
  size_t produced = 0;
  for (size_t o = 0; o < out_cap; o++) {
    const size_t src = (o * in_rate) / USB_MIC_OUTPUT_RATE_HZ;
    if (src >= mono_frames) {
      break;
    }
    out[produced++] = mono[src];
  }
  if (produced == 0 && mono_frames > 0) {
    out[0] = mono[0];
    produced = 1;
  }
  *out_frames = produced;
  free(mono_heap);
}

static void process_uac_rx_chunk(const uint8_t *data, size_t data_bytes, uint8_t channels,
                                 uint8_t subframe_size, uint32_t sample_rate) {
  if (data == NULL || data_bytes == 0 || subframe_size == 0 || channels == 0) {
    return;
  }

  const size_t frame_bytes = (size_t)channels * subframe_size;
  const size_t in_frames = data_bytes / frame_bytes;
  if (in_frames == 0) {
    return;
  }

  if (subframe_size != 2) {
    ESP_LOGW(TAG, "Unsupported subframe size %u (expected 16-bit)", subframe_size);
    return;
  }

  const int16_t *in_samples = (const int16_t *)data;
  int16_t *out_samples =
      (int16_t *)heap_caps_malloc(in_frames * sizeof(int16_t), MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
  if (out_samples == NULL) {
    out_samples = (int16_t *)malloc(in_frames * sizeof(int16_t));
  }
  if (out_samples == NULL) {
    return;
  }

  size_t out_frames = 0;
  resample_to_16k_mono(in_samples, in_frames, channels, sample_rate, out_samples, &out_frames);
  if (out_frames > 0) {
    push_output_samples(out_samples, out_frames);
  }
  free(out_samples);
}

static void drain_uac_rx_once(uac_host_device_handle_t mic) {
  if (mic == NULL || mic != s_mic_dev) {
    return;
  }
  static uint8_t s_rx_buf[9600];
  uint32_t rx_size = 0;
  if (uac_host_device_read(mic, s_rx_buf, sizeof(s_rx_buf), &rx_size, 0) == ESP_OK && rx_size > 0) {
    process_uac_rx_chunk(s_rx_buf, rx_size, s_stream_channels, s_stream_subframe_size,
                         s_stream_rate_hz);
  }
}

static void uac_device_callback(uac_host_device_handle_t uac_device_handle,
                                const uac_host_device_event_t event, void *arg) {
  (void)arg;

  switch (event) {
  case UAC_HOST_DEVICE_EVENT_RX_DONE:
    drain_uac_rx_once(uac_device_handle);
    break;
  case UAC_HOST_DRIVER_EVENT_DISCONNECTED:
    if (uac_device_handle != s_mic_dev) {
      break;
    }
    ESP_LOGW(TAG, "USB mic disconnected");
    s_mic_dev = NULL;
    reset_mic_stream_state();
    break;
  default:
    break;
  }
}

static void uac_host_lib_callback(uint8_t addr, uint8_t iface_num,
                                  const uac_host_driver_event_t event, void *arg) {
  (void)arg;

  if (event != UAC_HOST_DRIVER_EVENT_RX_CONNECTED) {
    return;
  }

  if (s_mic_dev != NULL || addr_is_blocked(addr)) {
    return;
  }

  if (s_connect_queue == NULL) {
    return;
  }

  const usb_mic_connect_req_t req = {.addr = addr, .iface_num = iface_num};
  ESP_LOGI(TAG, "UAC RX addr=%u iface=%u — queued (worker opens GPIO-header FS only)", addr,
           iface_num);
  if (xQueueSend(s_connect_queue, &req, 0) != pdTRUE) {
    ESP_LOGW(TAG, "UAC connect queue full — drop addr=%u iface=%u", addr, iface_num);
  }
}

static void process_uac_connect_request(const usb_mic_connect_req_t *req) {
  if (req == NULL || s_mic_dev != NULL || addr_is_blocked(req->addr)) {
    return;
  }

  if (device_is_known_non_mic(req->addr)) {
    return;
  }

  uint16_t vid = 0;
  uint16_t pid = 0;
  (void)peek_usb_ids(req->addr, &vid, &pid, NULL);

  if (vid == RESPEAKER_VID && pid == RESPEAKER_PID && s_uac_client != NULL) {
    usb_device_handle_t hdl = NULL;
    if (usb_host_device_open(s_uac_client, req->addr, &hdl) == ESP_OK) {
      usb_device_info_t info = {};
      if (usb_host_device_info(hdl, &info) == ESP_OK) {
        const char *spd = info.speed == USB_SPEED_FULL    ? "FS"
                          : info.speed == USB_SPEED_HIGH ? "HS"
                          : info.speed == USB_SPEED_LOW  ? "LS"
                                                         : "?";
        ESP_LOGI(TAG, "ReSpeaker 4-mic: addr=%u speed=%s parent=%s", req->addr, spd,
                 info.parent.dev_hdl ? "hub (J18?)" : "root (GPIO header OK)");
        if (info.parent.dev_hdl != NULL) {
          ESP_LOGW(TAG, "ReSpeaker is on a USB hub — for GPIO wiring use 5V/GND + D-/D+ on pins 24/25 only");
        }
      }
      usb_host_device_close(s_uac_client, hdl);
    }
  }

  if (s_open_mutex != NULL &&
      xSemaphoreTake(s_open_mutex, pdMS_TO_TICKS(5000)) != pdTRUE) {
    ESP_LOGW(TAG, "UAC open busy — skip addr=%u", req->addr);
    return;
  }

  ESP_LOGI(TAG, "Opening USB mic addr=%u iface=%u (%04x:%04x)", req->addr, req->iface_num, vid,
           pid);

  uac_host_device_handle_t mic = NULL;
  const uac_host_device_config_t dev_cfg = {
      .addr = req->addr,
      .iface_num = req->iface_num,
      .buffer_size = 9600,
      .buffer_threshold = 2400,
      .callback = uac_device_callback,
      .callback_arg = NULL,
  };

  if (uac_host_device_open(&dev_cfg, &mic) != ESP_OK) {
    ESP_LOGE(TAG, "UAC mic open failed addr=%u", req->addr);
    goto out_unlock;
  }

  uac_host_dev_alt_param_t alt = {};
  uint32_t mic_freq = USB_MIC_OUTPUT_RATE_HZ;
  if (!try_start_mic(mic, &alt, &mic_freq)) {
    ESP_LOGE(TAG, "UAC mic start failed addr=%u iface=%u (check 5V/GND/GPIO24/25)", req->addr,
             req->iface_num);
    close_mic_device(mic);
    goto out_unlock;
  }

  s_mic_dev = mic;
  s_stream_rate_hz = mic_freq;
  s_stream_channels = alt.channels;
  s_stream_subframe_size = alt.subframe_size ? alt.subframe_size : 2;
  s_active_addr = req->addr;
  s_active_vid = vid;
  s_active_pid = pid;
  s_active_iface = req->iface_num;
  s_has_audio = false;
  s_rx_chunks = 0;
  s_last_peak = 0;
  s_uac_open_us = esp_timer_get_time();
  set_mic_ready(true);
  if (s_pcm_ring != NULL) {
    xStreamBufferReset(s_pcm_ring);
  }
  ESP_LOGI(TAG, "USB mic UAC started addr=%u iface=%u %04x:%04x: %" PRIu32 " Hz, %u ch",
           req->addr, req->iface_num, vid, pid, mic_freq, alt.channels);
  drain_uac_rx_once(mic);

out_unlock:
  if (s_open_mutex != NULL) {
    xSemaphoreGive(s_open_mutex);
  }
}

static void uac_connect_worker(void *arg) {
  (void)arg;
  usb_mic_connect_req_t req;

  for (;;) {
    if (xQueueReceive(s_connect_queue, &req, portMAX_DELAY) != pdTRUE) {
      continue;
    }
    vTaskDelay(pdMS_TO_TICKS(USB_MIC_OPEN_SETTLE_MS));
    process_uac_connect_request(&req);
  }
}

static void usb_mic_rx_poll_task(void *arg) {
  (void)arg;
  for (;;) {
    vTaskDelay(pdMS_TO_TICKS(USB_MIC_RX_POLL_MS));
    if (s_mic_dev != NULL) {
      drain_uac_rx_once(s_mic_dev);
    }
  }
}

static void usb_mic_retry_task(void *arg) {
  (void)arg;

  for (;;) {
    vTaskDelay(pdMS_TO_TICKS(USB_MIC_RETRY_MS));
    if (s_uac_client == NULL) {
      continue;
    }

    /* UAC started but isochronous RX never arrived — try another interface. */
    if (s_mic_dev != NULL && s_rx_chunks == 0 && s_uac_open_us != 0) {
      const int64_t open_ms = (esp_timer_get_time() - s_uac_open_us) / 1000LL;
      if (open_ms >= USB_MIC_NO_RX_CLOSE_MS) {
        ESP_LOGW(TAG, "USB mic UAC open but no RX — closing addr=%u iface=%u %04x:%04x",
                 s_active_addr, s_active_iface, s_active_vid, s_active_pid);
        mark_open_config_failed(s_active_addr, s_active_iface);
        uac_host_device_handle_t dev = s_mic_dev;
        s_mic_dev = NULL;
        reset_mic_stream_state();
        close_mic_device(dev);
      }
    }

    if (usb_mic_ready()) {
      continue;
    }

    uint8_t addrs[8];
    int num = 0;
    if (usb_host_device_addr_list_fill((int)sizeof(addrs), addrs, &num) != ESP_OK || num <= 0) {
      continue;
    }

    ESP_LOGI(TAG, "USB mic scan: %d device(s)", num);
    for (int i = 0; i < num && !usb_mic_ready(); i++) {
      if (device_is_known_non_mic(addrs[i])) {
        continue;
      }
      uint16_t vid = 0;
      uint16_t pid = 0;
      (void)peek_usb_ids(addrs[i], &vid, &pid, NULL);
      /* Prefer ReSpeaker 4-mic when present. */
      if (vid != RESPEAKER_VID || pid != RESPEAKER_PID) {
        bool has_respeaker = false;
        for (int j = 0; j < num; j++) {
          uint16_t v2 = 0;
          uint16_t p2 = 0;
          (void)peek_usb_ids(addrs[j], &v2, &p2, NULL);
          if (v2 == RESPEAKER_VID && p2 == RESPEAKER_PID) {
            has_respeaker = true;
            break;
          }
        }
        if (has_respeaker) {
          continue;
        }
      }
      uint8_t iface = 0;
      if (!find_uac_input_iface(addrs[i], &iface)) {
        if (vid == RESPEAKER_VID && pid == RESPEAKER_PID) {
          ESP_LOGW(TAG, "All ReSpeaker UAC interfaces failed — clearing retry list for addr=%u",
                   addrs[i]);
          for (size_t k = 0; k < s_failed_open_count;) {
            if (s_failed_opens[k].addr == addrs[i]) {
              s_failed_open_count--;
              s_failed_opens[k] = s_failed_opens[s_failed_open_count];
            } else {
              k++;
            }
          }
        }
        continue;
      }
      const usb_mic_connect_req_t req = {.addr = addrs[i], .iface_num = iface};
      process_uac_connect_request(&req);
    }
  }
}

esp_err_t usb_mic_phy_init_for_header(void) {
#if CONFIG_USB4MIC_USB_PHY_ON_HEADER
  const usb_phy_config_t hs_phy_config = {
      .controller = USB_PHY_CTRL_OTG,
      .target = USB_PHY_TARGET_UTMI,
      .otg_mode = USB_OTG_MODE_HOST,
      .otg_speed = USB_PHY_SPEED_HIGH,
      .ext_io_conf = NULL,
      .otg_io_conf = NULL,
  };
  esp_err_t hs_err = usb_new_phy(&hs_phy_config, &s_hs_utmi_phy);
  if (hs_err != ESP_OK && hs_err != ESP_ERR_INVALID_STATE) {
    ESP_LOGE(TAG, "J18 HS UTMI phy init failed: %s", esp_err_to_name(hs_err));
    return hs_err;
  }

  usb_wrap_ll_phy_select(&USB_WRAP, 0);
  gpio_set_drive_capability(CONFIG_USB4MIC_USB_DM_GPIO, GPIO_DRIVE_CAP_3);
  gpio_set_drive_capability(CONFIG_USB4MIC_USB_DP_GPIO, GPIO_DRIVE_CAP_3);

  const usb_phy_config_t fs_phy_config = {
      .controller = USB_PHY_CTRL_OTG,
      .target = USB_PHY_TARGET_INT,
      .otg_mode = USB_OTG_MODE_HOST,
      .otg_speed = USB_PHY_SPEED_UNDEFINED,
      .ext_io_conf = NULL,
      .otg_io_conf = NULL,
  };
  esp_err_t fs_err = usb_new_phy(&fs_phy_config, &s_fs_header_phy);
  if (fs_err != ESP_OK) {
    ESP_LOGE(TAG, "GPIO header FS phy init failed: %s", esp_err_to_name(fs_err));
    return fs_err;
  }

  ESP_LOGI(TAG, "USB phys: HS UTMI (J18) + FS INT header D-=GPIO%d D+=GPIO%d (5V+GND)",
           CONFIG_USB4MIC_USB_DM_GPIO, CONFIG_USB4MIC_USB_DP_GPIO);
  return ESP_OK;
#else
  return ESP_ERR_NOT_SUPPORTED;
#endif
}

esp_err_t usb_mic_start(void) {
  if (s_uac_started) {
    return ESP_OK;
  }

  if (s_ready_mutex == NULL) {
    s_ready_mutex = xSemaphoreCreateMutex();
    if (s_ready_mutex == NULL) {
      return ESP_ERR_NO_MEM;
    }
  }

  if (s_open_mutex == NULL) {
    s_open_mutex = xSemaphoreCreateMutex();
    if (s_open_mutex == NULL) {
      return ESP_ERR_NO_MEM;
    }
  }

  esp_err_t client_err = uac_client_init();
  if (client_err != ESP_OK) {
    ESP_LOGE(TAG, "usb_host_client_register failed: %s", esp_err_to_name(client_err));
    return client_err;
  }

  if (s_pcm_ring == NULL) {
    s_pcm_ring = xStreamBufferCreate(USB_MIC_RING_BYTES, sizeof(int16_t));
    if (s_pcm_ring == NULL) {
      return ESP_ERR_NO_MEM;
    }
  }

  if (s_ring_send_mutex == NULL) {
    s_ring_send_mutex = xSemaphoreCreateMutex();
    if (s_ring_send_mutex == NULL) {
      return ESP_ERR_NO_MEM;
    }
  }

  if (s_read_mutex == NULL) {
    s_read_mutex = xSemaphoreCreateMutex();
    if (s_read_mutex == NULL) {
      return ESP_ERR_NO_MEM;
    }
  }

  if (s_connect_queue == NULL) {
    s_connect_queue = xQueueCreate(USB_MIC_OPEN_QUEUE_LEN, sizeof(usb_mic_connect_req_t));
    if (s_connect_queue == NULL) {
      return ESP_ERR_NO_MEM;
    }
    BaseType_t worker_ok = xTaskCreatePinnedToCore(
        uac_connect_worker, "usb_mic_open", USB_MIC_OPEN_WORKER_STACK, NULL, 8, NULL, 0);
    if (worker_ok != pdPASS) {
      vQueueDelete(s_connect_queue);
      s_connect_queue = NULL;
      return ESP_ERR_NO_MEM;
    }
    static bool retry_started;
    static bool poll_started;
    if (!retry_started) {
      retry_started = true;
      (void)xTaskCreatePinnedToCore(usb_mic_retry_task, "usb_mic_retry", 4096, NULL, 7, NULL, 0);
    }
    if (!poll_started) {
      poll_started = true;
      (void)xTaskCreatePinnedToCore(usb_mic_rx_poll_task, "usb_mic_rx", 4096, NULL, 8, NULL, 0);
    }
  }

  const uac_host_driver_config_t uac_config = {
      .create_background_task = true,
      .task_priority = USB_MIC_UAC_TASK_PRIO,
      .stack_size = 6144,
      .core_id = 0,
      .callback = uac_host_lib_callback,
      .callback_arg = NULL,
  };
  esp_err_t err = uac_host_install(&uac_config);
  if (err != ESP_OK) {
    ESP_LOGE(TAG, "uac_host_install failed: %s", esp_err_to_name(err));
    return err;
  }

  s_uac_started = true;
  ESP_LOGI(TAG, "UAC driver installed — waiting for USB mic on GPIO header");
  return ESP_OK;
}

bool usb_mic_ready(void) {
  bool ready = false;
  if (s_ready_mutex != NULL) {
    xSemaphoreTake(s_ready_mutex, portMAX_DELAY);
    ready = s_mic_ready && s_mic_dev != NULL;
    xSemaphoreGive(s_ready_mutex);
  }
  return ready;
}

bool usb_mic_uac_open(void) {
  return s_mic_dev != NULL;
}

void usb_mic_print_status(void) {
  if (s_mic_dev == NULL) {
    printf("usb mic: UAC not open\n");
    return;
  }
  printf("usb mic: addr=%u iface=%u %04x:%04x rx_chunks=%" PRIu32 " peak=%ld %s\n", s_active_addr,
         s_active_iface, s_active_vid, s_active_pid, s_rx_chunks, (long)s_last_peak,
         s_has_audio ? "signal ok" : (s_rx_chunks > 0 ? "streaming (quiet)" : "waiting for RX"));
}

esp_err_t usb_mic_read(int16_t *samples, int sample_count) {
  if (samples == NULL || sample_count <= 0) {
    return ESP_ERR_INVALID_ARG;
  }
  if (s_pcm_ring == NULL || s_read_mutex == NULL) {
    return ESP_ERR_INVALID_STATE;
  }

  xSemaphoreTake(s_read_mutex, portMAX_DELAY);

  const size_t need_bytes = (size_t)sample_count * sizeof(int16_t);
  size_t got = 0;
  esp_err_t result = ESP_OK;
  if (!usb_mic_ready()) {
    result = ESP_ERR_INVALID_STATE;
    goto out;
  }
  while (got < need_bytes) {
    if (!usb_mic_ready()) {
      result = ESP_ERR_INVALID_STATE;
      break;
    }
    const size_t n =
        xStreamBufferReceive(s_pcm_ring, ((uint8_t *)samples) + got, need_bytes - got,
                             pdMS_TO_TICKS(USB_MIC_READ_TIMEOUT_MS));
    if (n == 0) {
      result = ESP_ERR_TIMEOUT;
      break;
    }
    got += n;
  }

out:
  xSemaphoreGive(s_read_mutex);
  return result;
}

void usb_mic_flush(void) {
  if (s_pcm_ring == NULL || s_read_mutex == NULL) {
    return;
  }
  xSemaphoreTake(s_read_mutex, portMAX_DELAY);
  xStreamBufferReset(s_pcm_ring);
  xSemaphoreGive(s_read_mutex);
}

void usb_mic_print_usb_devices(void) {
  if (s_uac_client == NULL) {
    printf("usb devices: (UAC not started yet)\n");
    return;
  }

  uint8_t addrs[8];
  int num = 0;
  if (usb_host_device_addr_list_fill((int)sizeof(addrs), addrs, &num) != ESP_OK || num <= 0) {
    printf("usb devices: none enumerated\n");
    return;
  }

  printf("usb devices (%d):\n", num);
  for (int i = 0; i < num; i++) {
    usb_device_handle_t hdl = NULL;
    if (usb_host_device_open(s_uac_client, addrs[i], &hdl) != ESP_OK) {
      printf("  addr=%u (open failed)\n", addrs[i]);
      continue;
    }
    usb_device_info_t info = {};
    uint16_t vid = 0;
    uint16_t pid = 0;
    (void)read_usb_ids(hdl, &vid, &pid, NULL);
    if (usb_host_device_info(hdl, &info) == ESP_OK) {
      const char *spd = info.speed == USB_SPEED_HIGH    ? "HS"
                        : info.speed == USB_SPEED_FULL ? "FS"
                        : info.speed == USB_SPEED_LOW  ? "LS"
                                                       : "?";
      printf("  addr=%u %04x:%04x speed=%s parent=%s\n", addrs[i], vid, pid, spd,
             info.parent.dev_hdl ? "hub" : "root");
    } else {
      printf("  addr=%u\n", addrs[i]);
    }
    usb_host_device_close(s_uac_client, hdl);
  }
}

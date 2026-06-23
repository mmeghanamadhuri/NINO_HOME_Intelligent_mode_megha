#include "voice_ws_client.h"

#include <stdlib.h>
#include <string.h>

#include "esp_log.h"
#include "esp_websocket_client.h"
#include "nino_eye.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"

static const char *TAG = "nino_vws";

/* Long TTS replies at 16 kHz mono: ~4 min max; must exceed largest WS binary frame reassembly. */
#define MAX_VOICE_WAV (4 * 1024 * 1024)

#define EYE_EXPR_MAX 16

typedef struct {
  SemaphoreHandle_t done;
  esp_websocket_client_handle_t client;
  uint8_t *buf;
  size_t len;
  size_t cap;
  bool complete;
  bool error;
  bool prompt_medical_ack;
  char eye_expr[EYE_EXPR_MAX];
} vws_ctx_t;

static bool chunk_contains(const char *hay, size_t hay_len, const char *needle) {
  const size_t needle_len = strlen(needle);
  if (needle_len == 0 || hay_len < needle_len) {
    return false;
  }
  for (size_t i = 0; i + needle_len <= hay_len; ++i) {
    if (memcmp(hay + i, needle, needle_len) == 0) {
      return true;
    }
  }
  return false;
}

/*
 * Pull the quoted string value of "eye_expression" out of the JSON metadata,
 * matching the server format e.g. {"prompt_medical_ack": false, "eye_expression": "sad"}.
 * Tolerant of spaces around ':' and the key being anywhere in the object.
 */
static void parse_eye_expression(vws_ctx_t *ctx, const char *text, size_t len) {
  static const char key[] = "\"eye_expression\"";
  const size_t key_len = sizeof(key) - 1;
  if (len < key_len) {
    return;
  }
  size_t i = 0;
  for (; i + key_len <= len; ++i) {
    if (memcmp(text + i, key, key_len) == 0) {
      break;
    }
  }
  if (i + key_len > len) {
    return;
  }
  size_t p = i + key_len;
  /* Skip whitespace and the ':' separator. */
  while (p < len && (text[p] == ' ' || text[p] == '\t' || text[p] == ':')) {
    p++;
  }
  if (p >= len || text[p] != '"') {
    return;
  }
  p++;
  size_t out = 0;
  while (p < len && text[p] != '"' && out < sizeof(ctx->eye_expr) - 1) {
    ctx->eye_expr[out++] = text[p++];
  }
  ctx->eye_expr[out] = '\0';
}

static void parse_metadata_text(vws_ctx_t *ctx, const char *text, size_t len) {
  if (ctx == NULL || text == NULL || len == 0) {
    return;
  }
  parse_eye_expression(ctx, text, len);
  /* Show the expression the instant the tag arrives (the reply WAV is still
   * downloading); it then holds through playback and the audio queue reverts to
   * idle once the reply finishes. */
  if (ctx->eye_expr[0] != '\0') {
    nino_eye_apply_expression(ctx->eye_expr);
  }
  if (!chunk_contains(text, len, "prompt_medical_ack")) {
    return;
  }
  if (chunk_contains(text, len, "true")) {
    ctx->prompt_medical_ack = true;
  }
}

static void append_chunk(vws_ctx_t *ctx, const void *data, size_t len) {
  if (len == 0) {
    return;
  }
  size_t need = ctx->len + len;
  if (need > MAX_VOICE_WAV) {
    ctx->error = true;
    return;
  }
  if (need > ctx->cap) {
    size_t ncap = ctx->cap ? ctx->cap * 2 : 8192;
    while (ncap < need) {
      ncap *= 2;
    }
    uint8_t *nb = realloc(ctx->buf, ncap);
    if (nb == NULL) {
      ctx->error = true;
      return;
    }
    ctx->buf = nb;
    ctx->cap = ncap;
  }
  memcpy(ctx->buf + ctx->len, data, len);
  ctx->len += len;
}

static void signal_done(vws_ctx_t *ctx) {
  if (ctx->complete) {
    return;
  }
  ctx->complete = true;
  xSemaphoreGive(ctx->done);
}

static void on_event(void *handler_args, esp_event_base_t base, int32_t event_id,
                     void *event_data) {
  (void)base;
  vws_ctx_t *ctx = (vws_ctx_t *)handler_args;
  esp_websocket_event_data_t *ws = (esp_websocket_event_data_t *)event_data;

  switch (event_id) {
  case WEBSOCKET_EVENT_CONNECTED:
    ESP_LOGI(TAG, "WS connected");
    break;
  case WEBSOCKET_EVENT_DATA:
    /* esp_websocket passes one transport slice per event; data_ptr is that slice only.
     * payload_offset is the offset in the full WS message — do NOT subtract it from data_len
     * or continuation frames append 0 bytes and the WAV is truncated (~buffer_size). */
    if (ws->data_ptr == NULL || ws->data_len <= 0) {
      break;
    }
    if (ws->op_code != 0x02 && ws->op_code != 0x00) {
      if (ws->op_code == 0x01 && ws->data_ptr != NULL && ws->data_len > 0) {
        parse_metadata_text(ctx, (const char *)ws->data_ptr, (size_t)ws->data_len);
      }
      break;
    }
    append_chunk(ctx, (const uint8_t *)ws->data_ptr, (size_t)ws->data_len);
    if (ws->fin) {
      signal_done(ctx);
    }
    break;
  case WEBSOCKET_EVENT_DISCONNECTED:
    ESP_LOGI(TAG, "WS disconnected, collected %u bytes", (unsigned)ctx->len);
    signal_done(ctx);
    break;
  case WEBSOCKET_EVENT_ERROR:
    ESP_LOGW(TAG, "WS error type=%d sock_errno=%d msg=%.*s", (int)ws->error_handle.error_type,
             ws->error_handle.esp_transport_sock_errno, ws->data_len > 0 ? ws->data_len : 0,
             ws->data_ptr ? ws->data_ptr : "");
    ctx->error = true;
    signal_done(ctx);
    break;
  default:
    break;
  }
}

esp_err_t nino_voice_ws_exchange(const char *ws_uri, const uint8_t *wav_in,
                                   size_t wav_in_len, uint8_t **wav_out,
                                   size_t *wav_out_len, int timeout_ms,
                                   bool *prompt_medical_ack_out,
                                   char *eye_expr_out, size_t eye_expr_cap) {
  if (ws_uri == NULL || wav_in == NULL || wav_out == NULL || wav_out_len == NULL ||
      wav_in_len == 0) {
    return ESP_ERR_INVALID_ARG;
  }
  *wav_out = NULL;
  *wav_out_len = 0;
  if (prompt_medical_ack_out != NULL) {
    *prompt_medical_ack_out = false;
  }
  if (eye_expr_out != NULL && eye_expr_cap > 0) {
    eye_expr_out[0] = '\0';
  }

  vws_ctx_t ctx = {.done = xSemaphoreCreateBinary(),
                   .client = NULL,
                   .buf = NULL,
                   .len = 0,
                   .cap = 0,
                   .complete = false,
                   .error = false,
                   .prompt_medical_ack = false,
                   .eye_expr = {0}};
  if (ctx.done == NULL) {
    return ESP_ERR_NO_MEM;
  }

  esp_websocket_client_config_t ws_cfg = {
      .uri = ws_uri,
      .buffer_size = 65536,
      .network_timeout_ms = 300000,
      .task_stack = 12288,
      .task_prio = 5,
      .disable_pingpong_discon = true,
      .ping_interval_sec = 30,
  };

  ctx.client = esp_websocket_client_init(&ws_cfg);
  if (ctx.client == NULL) {
    vSemaphoreDelete(ctx.done);
    return ESP_ERR_NO_MEM;
  }

  esp_websocket_register_events(ctx.client, WEBSOCKET_EVENT_ANY, on_event, &ctx);

  esp_err_t err = esp_websocket_client_start(ctx.client);
  if (err != ESP_OK) {
    ESP_LOGE(TAG, "ws start failed: %s", esp_err_to_name(err));
    esp_websocket_client_destroy(ctx.client);
    vSemaphoreDelete(ctx.done);
    return err;
  }

  /* Wait until connected then send (poll start is async). */
  for (int i = 0; i < 200; i++) {
    if (esp_websocket_client_is_connected(ctx.client)) {
      break;
    }
    vTaskDelay(pdMS_TO_TICKS(50));
  }
  if (!esp_websocket_client_is_connected(ctx.client)) {
    ESP_LOGE(TAG, "WS connect timeout");
    ctx.error = true;
    goto cleanup;
  }

  int sent = esp_websocket_client_send_bin(ctx.client, (const char *)wav_in,
                                           (int)wav_in_len, pdMS_TO_TICKS(30000));
  if (sent < 0 || (size_t)sent != wav_in_len) {
    ESP_LOGE(TAG, "send_bin failed: %d", sent);
    ctx.error = true;
    goto cleanup;
  }
  ESP_LOGI(TAG, "Sent %u bytes to voice WS", (unsigned)wav_in_len);
  /* Voice has reached the server: listening ends, ponder until the reply. */
  nino_eye_thinking();

  if (xSemaphoreTake(ctx.done, pdMS_TO_TICKS(timeout_ms)) != pdTRUE) {
    ESP_LOGE(TAG, "Response timeout");
    ctx.error = true;
  }

cleanup:
  esp_websocket_client_stop(ctx.client);
  esp_websocket_client_destroy(ctx.client);
  vSemaphoreDelete(ctx.done);

  if (ctx.error || ctx.len == 0 || ctx.buf == NULL) {
    /* Failed/empty: nothing will play to revert the eyes, so clear any emotion
     * (or stuck listening/thinking) back to idle now. */
    nino_eye_idle();
    if (ctx.len > 0 && ctx.buf != NULL) {
      ESP_LOGW(TAG, "WS done but error=%d len=%u (reply dropped)", (int)ctx.error, (unsigned)ctx.len);
    }
    free(ctx.buf);
    return ctx.error ? ESP_FAIL : ESP_ERR_NOT_FOUND;
  }

  /* Success: if the server sent an expression it is already showing and must
   * persist until the reply finishes (the audio queue reverts to idle then).
   * With no tag, drop the thinking face to idle for this reply. */
  if (ctx.eye_expr[0] == '\0') {
    nino_eye_idle();
  }

  *wav_out = ctx.buf;
  *wav_out_len = ctx.len;
  if (prompt_medical_ack_out != NULL) {
    *prompt_medical_ack_out = ctx.prompt_medical_ack;
  }
  if (eye_expr_out != NULL && eye_expr_cap > 0) {
    strncpy(eye_expr_out, ctx.eye_expr, eye_expr_cap - 1);
    eye_expr_out[eye_expr_cap - 1] = '\0';
  }
  return ESP_OK;
}

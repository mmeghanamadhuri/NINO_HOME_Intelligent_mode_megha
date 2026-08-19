#include "voice_ws_client.h"

#include <inttypes.h>
#include <stdlib.h>
#include <string.h>

#include "esp_log.h"
#include "esp_timer.h"
#include "esp_websocket_client.h"
#include "nino_eye.h"
#include "audio_playback.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"

static const char *TAG = "nino_vws";

#define MAX_VOICE_WAV (4 * 1024 * 1024)
#define EYE_EXPR_MAX 16
#define MOTION_JSON_MAX 192
/* write-0 can race EOS JSON still in the WS event task. Wait, then retry
 * while the socket is still up — do not auto-reconnect (that hung query_busy). */
#define STREAM_WRITE_FAIL_WAIT_MS 1000
#define STREAM_WRITE_RETRIES 2

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
  bool wav_msg_open;
} vws_ctx_t;

static esp_websocket_client_config_t make_ws_cfg(const char *uri, bool stream_session) {
  esp_websocket_client_config_t ws_cfg = {
      .uri = uri,
      .buffer_size = 65536,
      /* Keep the stream socket alive through long STT/LLM/TTS turns. */
      .network_timeout_ms = 300000,
      .task_stack = 12288,
      .task_prio = 5,
      .disable_pingpong_discon = true,
      .ping_interval_sec = 30,
      /* Stream sessions must fail closed. Auto-reconnect leaves s_query_busy set
       * while stop() waits on "Reconnect after 10000 ms". */
      .disable_auto_reconnect = stream_session,
  };
  if (!stream_session) {
    ws_cfg.reconnect_timeout_ms = 10000;
  }
  return ws_cfg;
}

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

static size_t copy_json_array(const char *text, size_t len, size_t start, char *out,
                              size_t out_cap) {
  if (out == NULL || out_cap == 0 || start >= len) {
    return 0;
  }
  while (start < len && text[start] != '[') {
    start++;
  }
  if (start >= len || text[start] != '[') {
    return 0;
  }
  int depth = 0;
  size_t n = 0;
  for (size_t i = start; i < len && n + 1 < out_cap; i++) {
    char c = text[i];
    out[n++] = c;
    if (c == '[') {
      depth++;
    } else if (c == ']') {
      depth--;
      if (depth == 0) {
        out[n] = '\0';
        return n;
      }
    }
  }
  out[0] = '\0';
  return 0;
}

static void parse_motion_into(char *dst, size_t dst_cap, const char *text, size_t len) {
  static const char key[] = "\"motion\"";
  const size_t key_len = sizeof(key) - 1;
  if (dst == NULL || dst_cap == 0 || text == NULL || len < key_len) {
    return;
  }
  for (size_t i = 0; i + key_len <= len; ++i) {
    if (memcmp(text + i, key, key_len) != 0) {
      continue;
    }
    if (copy_json_array(text, len, i + key_len, dst, dst_cap) > 0) {
      return;
    }
  }
}

static void parse_metadata_text(vws_ctx_t *ctx, const char *text, size_t len) {
  if (ctx == NULL || text == NULL || len == 0) {
    return;
  }
  parse_eye_expression(ctx, text, len);
  if (ctx->eye_expr[0] != '\0') {
    nino_eye_apply_expression(ctx->eye_expr);
  }

  /* Require an explicit true value — a bare "true" anywhere in the JSON
   * (or matching inside unrelated fields) used to force a second listen. */
  static const char key[] = "\"prompt_medical_ack\"";
  const size_t key_len = sizeof(key) - 1;
  for (size_t i = 0; i + key_len < len; ++i) {
    if (memcmp(text + i, key, key_len) != 0) {
      continue;
    }
    size_t j = i + key_len;
    while (j < len && (text[j] == ' ' || text[j] == '\t' || text[j] == '\n' ||
                       text[j] == '\r')) {
      ++j;
    }
    if (j >= len || text[j] != ':') {
      continue;
    }
    ++j;
    while (j < len && (text[j] == ' ' || text[j] == '\t' || text[j] == '\n' ||
                       text[j] == '\r')) {
      ++j;
    }
    if (j + 4 <= len && memcmp(text + j, "true", 4) == 0) {
      ctx->prompt_medical_ack = true;
    }
    break;
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
  if (ctx == NULL || ctx->complete) {
    return;
  }
  ctx->complete = true;
  xSemaphoreGive(ctx->done);
}

static void try_finish_wav_reply(vws_ctx_t *ctx) {
  if (ctx == NULL || ctx->complete || !ctx->wav_msg_open) {
    return;
  }
  if (!nino_audio_wav_bytes_valid(ctx->buf, ctx->len)) {
    return;
  }
  signal_done(ctx);
}

static void on_event(void *handler_args, esp_event_base_t base, int32_t event_id,
                     void *event_data) {
  (void)base;
  vws_ctx_t *ctx = (vws_ctx_t *)handler_args;
  if (ctx == NULL) {
    return;
  }
  esp_websocket_event_data_t *ws = (esp_websocket_event_data_t *)event_data;

  switch (event_id) {
  case WEBSOCKET_EVENT_CONNECTED:
    ESP_LOGI(TAG, "WS connected");
    break;
  case WEBSOCKET_EVENT_DATA:
    if (ws->data_ptr == NULL || ws->data_len <= 0) {
      break;
    }
    if (ws->op_code == 0x01) {
      parse_metadata_text(ctx, (const char *)ws->data_ptr, (size_t)ws->data_len);
      break;
    }
    if (ws->op_code != 0x02 && ws->op_code != 0x00) {
      break;
    }
    if (ws->op_code == 0x02) {
      ctx->wav_msg_open = true;
    }
    append_chunk(ctx, (const uint8_t *)ws->data_ptr, (size_t)ws->data_len);
    if (ws->fin) {
      try_finish_wav_reply(ctx);
    }
    break;
  case WEBSOCKET_EVENT_DISCONNECTED:
    ESP_LOGI(TAG, "WS disconnected, collected %u bytes", (unsigned)ctx->len);
    if (!ctx->complete) {
      if (!nino_audio_wav_bytes_valid(ctx->buf, ctx->len)) {
        ESP_LOGW(TAG, "WS closed before full WAV (%u bytes)", (unsigned)ctx->len);
        ctx->error = true;
      }
      signal_done(ctx);
    }
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

static void ws_client_shutdown(vws_ctx_t *ctx, bool force_destroy) {
  if (ctx == NULL || ctx->client == NULL) {
    return;
  }
  esp_websocket_client_handle_t client = ctx->client;
  ctx->client = NULL;
  esp_websocket_unregister_events(client, WEBSOCKET_EVENT_ANY, on_event);
  if (force_destroy) {
    /* Connect/response errors: avoid blocking wake recovery on a hung stop(). */
    esp_websocket_client_destroy(client);
    return;
  }
  esp_websocket_client_stop(client);
  esp_websocket_client_destroy(client);
}

void nino_voice_ws_preconnect_cancel(void) {}

esp_err_t nino_voice_ws_preconnect(const char *ws_uri) {
  (void)ws_uri;
  return ESP_OK;
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

  const int64_t t_exchange = esp_timer_get_time();

  vws_ctx_t *ctx = (vws_ctx_t *)calloc(1, sizeof(vws_ctx_t));
  if (ctx == NULL) {
    return ESP_ERR_NO_MEM;
  }
  ctx->done = xSemaphoreCreateBinary();
  if (ctx->done == NULL) {
    free(ctx);
    return ESP_ERR_NO_MEM;
  }

  esp_websocket_client_config_t ws_cfg = make_ws_cfg(ws_uri, false);
  ctx->client = esp_websocket_client_init(&ws_cfg);
  if (ctx->client == NULL) {
    vSemaphoreDelete(ctx->done);
    free(ctx);
    return ESP_ERR_NO_MEM;
  }

  esp_websocket_register_events(ctx->client, WEBSOCKET_EVENT_ANY, on_event, ctx);

  esp_err_t err = esp_websocket_client_start(ctx->client);
  if (err != ESP_OK) {
    ESP_LOGE(TAG, "ws start failed: %s", esp_err_to_name(err));
    ws_client_shutdown(ctx, true);
    vSemaphoreDelete(ctx->done);
    free(ctx);
    return err;
  }

  const int64_t t_connect = esp_timer_get_time();
  for (int i = 0; i < 200; i++) {
    if (esp_websocket_client_is_connected(ctx->client)) {
      break;
    }
    vTaskDelay(pdMS_TO_TICKS(50));
  }
  if (!esp_websocket_client_is_connected(ctx->client)) {
    ESP_LOGE(TAG, "WS connect timeout");
    ctx->error = true;
    goto cleanup;
  }
  ESP_LOGI(TAG, "WS connect took %" PRId64 " ms", (esp_timer_get_time() - t_connect) / 1000LL);

  int sent = esp_websocket_client_send_bin(ctx->client, (const char *)wav_in,
                                           (int)wav_in_len, pdMS_TO_TICKS(30000));
  if (sent < 0 || (size_t)sent != wav_in_len) {
    ESP_LOGE(TAG, "send_bin failed: %d", sent);
    ctx->error = true;
    goto cleanup;
  }
  ESP_LOGI(TAG, "Sent %u bytes to voice WS", (unsigned)wav_in_len);
  nino_eye_thinking();

  const int64_t t_server = esp_timer_get_time();
  if (xSemaphoreTake(ctx->done, pdMS_TO_TICKS(timeout_ms)) != pdTRUE) {
    ESP_LOGE(TAG, "Response timeout");
    ctx->error = true;
  } else {
    ESP_LOGI(TAG, "Server reply took %" PRId64 " ms", (esp_timer_get_time() - t_server) / 1000LL);
  }

cleanup:
  ESP_LOGI(TAG, "WS cleanup (error=%d)", ctx->error ? 1 : 0);
  ws_client_shutdown(ctx, ctx->error);
  vSemaphoreDelete(ctx->done);

  if (ctx->error || ctx->len == 0 || ctx->buf == NULL ||
      !nino_audio_wav_bytes_valid(ctx->buf, ctx->len)) {
    nino_eye_idle();
    if (ctx->len > 0 && ctx->buf != NULL) {
      ESP_LOGE(TAG, "WS reply invalid WAV (%u bytes, magic=%.4s)", (unsigned)ctx->len,
               ctx->buf);
    }
    const bool failed = ctx->error || ctx->len > 0;
    free(ctx->buf);
    free(ctx);
    return failed ? ESP_FAIL : ESP_ERR_NOT_FOUND;
  }

  if (ctx->eye_expr[0] == '\0') {
    nino_eye_idle();
  }

  *wav_out = ctx->buf;
  *wav_out_len = ctx->len;
  ESP_LOGI(TAG, "WS reply WAV ok (%u bytes)", (unsigned)ctx->len);
  if (prompt_medical_ack_out != NULL) {
    *prompt_medical_ack_out = ctx->prompt_medical_ack;
  }
  if (eye_expr_out != NULL && eye_expr_cap > 0) {
    strncpy(eye_expr_out, ctx->eye_expr, eye_expr_cap - 1);
    eye_expr_out[eye_expr_cap - 1] = '\0';
  }
  ESP_LOGI(TAG, "WS exchange total %" PRId64 " ms", (esp_timer_get_time() - t_exchange) / 1000LL);
  free(ctx);
  return ESP_OK;
}

struct nino_voice_ws_session {
  SemaphoreHandle_t evt;
  SemaphoreHandle_t mu;
  esp_websocket_client_handle_t client;
  uint8_t *buf;
  size_t len;
  size_t cap;
  bool connected;
  bool error;
  bool eos;
  bool skip;
  bool end_session;
  bool wav_msg_open;
  bool reply_ready;
  bool greet_pending;
  char eye_expr[EYE_EXPR_MAX];
  char motion_json[MOTION_JSON_MAX];
};

static bool json_has_key_true(const char *text, size_t len, const char *key) {
  const size_t key_len = strlen(key);
  if (text == NULL || key_len == 0 || len < key_len) {
    return false;
  }
  for (size_t i = 0; i + key_len < len; ++i) {
    if (memcmp(text + i, key, key_len) != 0) {
      continue;
    }
    size_t j = i + key_len;
    while (j < len && (text[j] == ' ' || text[j] == '\t' || text[j] == '\n' ||
                       text[j] == '\r')) {
      ++j;
    }
    if (j >= len || text[j] != ':') {
      continue;
    }
    ++j;
    while (j < len && (text[j] == ' ' || text[j] == '\t' || text[j] == '\n' ||
                       text[j] == '\r')) {
      ++j;
    }
    return j + 4 <= len && memcmp(text + j, "true", 4) == 0;
  }
  return false;
}

static bool json_type_is(const char *text, size_t len, const char *want) {
  static const char key[] = "\"type\"";
  const size_t key_len = sizeof(key) - 1;
  const size_t want_len = strlen(want);
  if (text == NULL || want_len == 0 || len < key_len + want_len) {
    return false;
  }
  for (size_t i = 0; i + key_len < len; ++i) {
    if (memcmp(text + i, key, key_len) != 0) {
      continue;
    }
    size_t j = i + key_len;
    while (j < len && (text[j] == ' ' || text[j] == '\t' || text[j] == ':')) {
      ++j;
    }
    if (j >= len || text[j] != '"') {
      continue;
    }
    ++j;
    if (j + want_len < len && memcmp(text + j, want, want_len) == 0 &&
        text[j + want_len] == '"') {
      return true;
    }
  }
  return false;
}

static void stream_signal(nino_voice_ws_session_t *s) {
  if (s != NULL && s->evt != NULL) {
    xSemaphoreGive(s->evt);
  }
}

static void stream_on_event(void *handler_args, esp_event_base_t base, int32_t event_id,
                            void *event_data) {
  (void)base;
  nino_voice_ws_session_t *s = (nino_voice_ws_session_t *)handler_args;
  if (s == NULL) {
    return;
  }
  esp_websocket_event_data_t *ws = (esp_websocket_event_data_t *)event_data;

  switch (event_id) {
  case WEBSOCKET_EVENT_CONNECTED:
    s->connected = true;
    ESP_LOGI(TAG, "stream WS connected");
    stream_signal(s);
    break;
  case WEBSOCKET_EVENT_DATA:
    if (ws->data_ptr == NULL || ws->data_len <= 0) {
      break;
    }
    if (ws->op_code == 0x01) {
      const char *text = (const char *)ws->data_ptr;
      const size_t tlen = (size_t)ws->data_len;
      if (json_type_is(text, tlen, "end_of_speech")) {
        s->eos = true;
        stream_signal(s);
      }
      if (json_type_is(text, tlen, "skip") || json_has_key_true(text, tlen, "\"skip\"")) {
        s->skip = true;
        s->eos = true;
        s->reply_ready = true;
        stream_signal(s);
      }
      if (json_type_is(text, tlen, "reply") ||
          json_has_key_true(text, tlen, "\"end_session\"") ||
          json_has_key_true(text, tlen, "\"prompt_medical_ack\"")) {
        if (json_has_key_true(text, tlen, "\"end_session\"")) {
          s->end_session = true;
        }
        if (json_has_key_true(text, tlen, "\"skip\"")) {
          s->skip = true;
          s->reply_ready = true;
          stream_signal(s);
        }
      }
      {
        static const char key[] = "\"eye_expression\"";
        const size_t key_len = sizeof(key) - 1;
        for (size_t i = 0; i + key_len <= tlen; ++i) {
          if (memcmp(text + i, key, key_len) != 0) {
            continue;
          }
          size_t p = i + key_len;
          while (p < tlen && (text[p] == ' ' || text[p] == '\t' || text[p] == ':')) {
            p++;
          }
          if (p >= tlen || text[p] != '"') {
            break;
          }
          p++;
          size_t out = 0;
          while (p < tlen && text[p] != '"' && out < sizeof(s->eye_expr) - 1) {
            s->eye_expr[out++] = text[p++];
          }
          s->eye_expr[out] = '\0';
          if (s->eye_expr[0] != '\0') {
            nino_eye_state_t st = nino_eye_state_from_name(s->eye_expr);
            if (s->greet_pending && st != NINO_EYE_HAPPY) {
              /* Session-open hunt / register: ignore random curious/tired/etc. */
              s->eye_expr[0] = '\0';
            } else {
              nino_eye_apply_expression(s->eye_expr);
            }
          }
          break;
        }
      }
      parse_motion_into(s->motion_json, sizeof(s->motion_json), text, tlen);
      if (json_type_is(text, tlen, "motion") && s->motion_json[0] != '\0') {
        /* Side-channel motion frame — still played with the next WAV. */
      }
      break;
    }
    if (ws->op_code != 0x02 && ws->op_code != 0x00) {
      break;
    }
    if (ws->op_code == 0x02) {
      s->wav_msg_open = true;
    }
    {
      vws_ctx_t tmp = {
          .buf = s->buf,
          .len = s->len,
          .cap = s->cap,
      };
      append_chunk(&tmp, (const uint8_t *)ws->data_ptr, (size_t)ws->data_len);
      s->buf = tmp.buf;
      s->len = tmp.len;
      s->cap = tmp.cap;
      s->error = tmp.error;
    }
    if (ws->fin && s->wav_msg_open && nino_audio_wav_bytes_valid(s->buf, s->len)) {
      s->reply_ready = true;
      s->eos = true;
      stream_signal(s);
    }
    break;
  case WEBSOCKET_EVENT_DISCONNECTED:
  case WEBSOCKET_EVENT_CLOSED:
  case WEBSOCKET_EVENT_ERROR:
    ESP_LOGW(TAG, "stream WS %s — failing session (no auto-reconnect)",
             event_id == WEBSOCKET_EVENT_ERROR ? "error" : "disconnected");
    s->error = true;
    s->connected = false;
    stream_signal(s);
    break;
  default:
    break;
  }
}

esp_err_t nino_voice_ws_session_open(const char *ws_uri,
                                     nino_voice_ws_session_t **out) {
  if (ws_uri == NULL || out == NULL) {
    return ESP_ERR_INVALID_ARG;
  }
  *out = NULL;
  nino_voice_ws_session_t *s = (nino_voice_ws_session_t *)calloc(1, sizeof(*s));
  if (s == NULL) {
    return ESP_ERR_NO_MEM;
  }
  s->greet_pending = true;
  s->evt = xSemaphoreCreateBinary();
  s->mu = xSemaphoreCreateMutex();
  if (s->evt == NULL || s->mu == NULL) {
    if (s->evt) {
      vSemaphoreDelete(s->evt);
    }
    if (s->mu) {
      vSemaphoreDelete(s->mu);
    }
    free(s);
    return ESP_ERR_NO_MEM;
  }
  esp_websocket_client_config_t ws_cfg = make_ws_cfg(ws_uri, true);
  s->client = esp_websocket_client_init(&ws_cfg);
  if (s->client == NULL) {
    vSemaphoreDelete(s->evt);
    vSemaphoreDelete(s->mu);
    free(s);
    return ESP_ERR_NO_MEM;
  }
  esp_websocket_register_events(s->client, WEBSOCKET_EVENT_ANY, stream_on_event, s);
  ESP_LOGI(TAG, "connecting %s", ws_uri);
  esp_err_t err = esp_websocket_client_start(s->client);
  if (err != ESP_OK) {
    esp_websocket_unregister_events(s->client, WEBSOCKET_EVENT_ANY, stream_on_event);
    esp_websocket_client_destroy(s->client);
    vSemaphoreDelete(s->evt);
    vSemaphoreDelete(s->mu);
    free(s);
    return err;
  }
  for (int i = 0; i < 200; i++) {
    if (esp_websocket_client_is_connected(s->client)) {
      s->connected = true;
      break;
    }
    vTaskDelay(pdMS_TO_TICKS(50));
  }
  if (!s->connected) {
    nino_voice_ws_session_close(s);
    return ESP_ERR_TIMEOUT;
  }
  *out = s;
  return ESP_OK;
}

bool nino_voice_ws_session_is_open(nino_voice_ws_session_t *session) {
  return session != NULL && session->connected && !session->error &&
         session->client != NULL &&
         esp_websocket_client_is_connected(session->client);
}

static bool stream_pause_flags(const nino_voice_ws_session_t *session) {
  return session != NULL && (session->eos || session->skip || session->reply_ready);
}

static void stream_wait_pause_flags(nino_voice_ws_session_t *session, int timeout_ms) {
  if (session == NULL || timeout_ms <= 0) {
    return;
  }
  const TickType_t deadline = xTaskGetTickCount() + pdMS_TO_TICKS(timeout_ms);
  while (!stream_pause_flags(session) && !session->error) {
    TickType_t now = xTaskGetTickCount();
    if (now >= deadline) {
      break;
    }
    TickType_t wait = deadline - now;
    if (session->evt != NULL) {
      xSemaphoreTake(session->evt, wait);
    } else {
      vTaskDelay(wait);
    }
  }
}

esp_err_t nino_voice_ws_session_send_pcm(nino_voice_ws_session_t *session,
                                         const void *pcm, size_t len) {
  if (session == NULL || pcm == NULL || len == 0 || session->client == NULL) {
    return ESP_ERR_INVALID_ARG;
  }
  /* EOS/skip/reply means pause TX for WAIT_PC — not a dead session. */
  if (stream_pause_flags(session)) {
    return ESP_ERR_INVALID_STATE;
  }
  if (session->error || !session->connected) {
    return ESP_ERR_INVALID_STATE;
  }

  for (int attempt = 0; attempt <= STREAM_WRITE_RETRIES; ++attempt) {
    if (stream_pause_flags(session)) {
      return ESP_ERR_INVALID_STATE;
    }
    if (session->error || !session->connected ||
        !esp_websocket_client_is_connected(session->client)) {
      break;
    }
    int sent = esp_websocket_client_send_bin(session->client, (const char *)pcm,
                                             (int)len, pdMS_TO_TICKS(1000));
    if (sent >= 0 && (size_t)sent == len) {
      return ESP_OK;
    }
    /* Server may stop reading after VAD EOS while send_bin is in flight. */
    if (stream_pause_flags(session)) {
      return ESP_ERR_INVALID_STATE;
    }
    ESP_LOGW(TAG, "send_bin incomplete sent=%d want=%u attempt=%d/%d", sent,
             (unsigned)len, attempt + 1, STREAM_WRITE_RETRIES + 1);
    stream_wait_pause_flags(session, STREAM_WRITE_FAIL_WAIT_MS);
    if (stream_pause_flags(session)) {
      return ESP_ERR_INVALID_STATE;
    }
  }

  session->error = true;
  session->connected = false;
  return ESP_FAIL;
}

bool nino_voice_ws_session_should_pause(nino_voice_ws_session_t *session) {
  return session == NULL || session->eos || session->error || session->skip ||
         session->reply_ready;
}

void nino_voice_ws_session_begin_turn(nino_voice_ws_session_t *session) {
  if (session == NULL) {
    return;
  }
  session->greet_pending = false;
  session->eos = false;
  session->skip = false;
  session->end_session = false;
  session->wav_msg_open = false;
  session->reply_ready = false;
  session->len = 0;
  session->eye_expr[0] = '\0';
  session->motion_json[0] = '\0';
  while (xSemaphoreTake(session->evt, 0) == pdTRUE) {
  }
}

esp_err_t nino_voice_ws_session_wait_reply(nino_voice_ws_session_t *session,
                                           int timeout_ms, uint8_t **wav_out,
                                           size_t *wav_out_len, bool *skip,
                                           bool *end_session, char *eye_expr_out,
                                           size_t eye_expr_cap, char *motion_out,
                                           size_t motion_cap) {
  if (session == NULL || wav_out == NULL || wav_out_len == NULL) {
    return ESP_ERR_INVALID_ARG;
  }
  *wav_out = NULL;
  *wav_out_len = 0;
  if (skip) {
    *skip = false;
  }
  if (end_session) {
    *end_session = false;
  }
  const TickType_t deadline = xTaskGetTickCount() + pdMS_TO_TICKS(timeout_ms);
  while (!session->reply_ready && !session->skip && !session->error) {
    TickType_t now = xTaskGetTickCount();
    if (now >= deadline) {
      return ESP_ERR_TIMEOUT;
    }
    TickType_t wait = deadline - now;
    if (wait > pdMS_TO_TICKS(200)) {
      wait = pdMS_TO_TICKS(200);
    }
    xSemaphoreTake(session->evt, wait);
  }
  if (session->error) {
    return ESP_FAIL;
  }
  if (skip) {
    *skip = session->skip;
  }
  if (end_session) {
    *end_session = session->end_session;
  }
  if (eye_expr_out != NULL && eye_expr_cap > 0) {
    strncpy(eye_expr_out, session->eye_expr, eye_expr_cap - 1);
    eye_expr_out[eye_expr_cap - 1] = '\0';
  }
  if (motion_out != NULL && motion_cap > 0) {
    strncpy(motion_out, session->motion_json, motion_cap - 1);
    motion_out[motion_cap - 1] = '\0';
  }
  if (session->skip || session->len == 0) {
    return ESP_OK;
  }
  if (!nino_audio_wav_bytes_valid(session->buf, session->len)) {
    return ESP_FAIL;
  }
  uint8_t *copy = (uint8_t *)malloc(session->len);
  if (copy == NULL) {
    return ESP_ERR_NO_MEM;
  }
  memcpy(copy, session->buf, session->len);
  *wav_out = copy;
  *wav_out_len = session->len;
  return ESP_OK;
}

void nino_voice_ws_session_close(nino_voice_ws_session_t *session) {
  if (session == NULL) {
    return;
  }
  if (session->client != NULL) {
    esp_websocket_client_handle_t client = session->client;
    session->client = NULL;
    session->connected = false;
    esp_websocket_unregister_events(client, WEBSOCKET_EVENT_ANY, stream_on_event);
    (void)esp_websocket_client_close(client, pdMS_TO_TICKS(250));
    /* destroy() without stop(): stop() can block on a reconnect wait. */
    esp_websocket_client_destroy(client);
  }
  free(session->buf);
  if (session->evt) {
    vSemaphoreDelete(session->evt);
  }
  if (session->mu) {
    vSemaphoreDelete(session->mu);
  }
  free(session);
}

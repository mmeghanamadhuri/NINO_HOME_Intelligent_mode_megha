#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

typedef struct {
  bool found;
  int cx;
  int cy;
  int x1;
  int y1;
  int x2;
  int y2;
  int frame_w;
  int frame_h;
  float score;
} nino_face_detect_result_t;

esp_err_t nino_face_detect_init(void);
bool nino_face_detect_is_ready(void);
esp_err_t nino_face_detect_process(const uint8_t *jpeg, size_t len,
                                   nino_face_detect_result_t *out);

#ifdef __cplusplus
}
#endif

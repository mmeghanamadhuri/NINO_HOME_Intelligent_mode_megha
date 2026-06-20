#include "face_detect.hpp"

#include <list>

#include "sdkconfig.h"
#include "esp_heap_caps.h"
#include "esp_log.h"

#include "dl_image_jpeg.hpp"
#include "human_face_detect.hpp"

static const char *TAG = "face_detect";
static HumanFaceDetect *s_detector = nullptr;

esp_err_t nino_face_detect_init(void) {
  if (s_detector != nullptr) {
    return ESP_OK;
  }

#if CONFIG_HUMAN_FACE_DETECT_MSRMNP_S8_V1
  HumanFaceDetect::model_type_t model_type = HumanFaceDetect::MSRMNP_S8_V1;
  const char *model_name = "MSRMNP_S8_V1";
#elif CONFIG_ESPDET_PICO_224_224_FACE
  HumanFaceDetect::model_type_t model_type =
      HumanFaceDetect::ESPDET_PICO_224_224_FACE;
  const char *model_name = "ESPDET_PICO_224_224_FACE";
#elif CONFIG_ESPDET_PICO_416_416_FACE
  HumanFaceDetect::model_type_t model_type =
      HumanFaceDetect::ESPDET_PICO_416_416_FACE;
  const char *model_name = "ESPDET_PICO_416_416_FACE";
#else
  ESP_LOGE(TAG, "No human face detect model is enabled in sdkconfig");
  return ESP_ERR_NOT_SUPPORTED;
#endif

  s_detector = new HumanFaceDetect(model_type, false);
  if (s_detector == nullptr) {
    return ESP_ERR_NO_MEM;
  }

  ESP_LOGI(TAG, "ESP-DL face detector ready with model %s", model_name);
  return ESP_OK;
}

bool nino_face_detect_is_ready(void) { return s_detector != nullptr; }

esp_err_t nino_face_detect_process(const uint8_t *jpeg, size_t len,
                                   nino_face_detect_result_t *out) {
  if (out == nullptr) {
    return ESP_ERR_INVALID_ARG;
  }
  *out = {};

  if (s_detector == nullptr || jpeg == nullptr || len == 0) {
    return ESP_ERR_INVALID_STATE;
  }

  dl::image::jpeg_img_t jpeg_img = {
      .data = const_cast<uint8_t *>(jpeg),
      .data_len = len,
  };

#if CONFIG_SOC_JPEG_CODEC_SUPPORTED
  dl::image::img_t img = dl::image::hw_decode_jpeg(
      jpeg_img, dl::image::DL_IMAGE_PIX_TYPE_RGB888, 120);
#else
  dl::image::img_t img = dl::image::sw_decode_jpeg(
      jpeg_img, dl::image::DL_IMAGE_PIX_TYPE_RGB888);
#endif
  if (img.data == nullptr) {
    return ESP_FAIL;
  }

  out->frame_w = img.width;
  out->frame_h = img.height;

  std::list<dl::detect::result_t> &results = s_detector->run(img);
  float best_score = -1.0f;

  for (const auto &res : results) {
    if (res.box.size() < 4) {
      continue;
    }
    if (res.score <= best_score) {
      continue;
    }

    best_score = res.score;
    out->found = true;
    out->score = res.score;
    out->x1 = res.box[0];
    out->y1 = res.box[1];
    out->x2 = res.box[2];
    out->y2 = res.box[3];
    out->cx = (res.box[0] + res.box[2]) / 2;
    out->cy = (res.box[1] + res.box[3]) / 2;
  }

  heap_caps_free(img.data);
  return ESP_OK;
}

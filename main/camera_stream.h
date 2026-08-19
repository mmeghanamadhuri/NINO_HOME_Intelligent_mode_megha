#pragma once

#include <stdbool.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/**
 * USB UVC stays enumerated; frames are grabbed/logged/streamed for the whole
 * voice conversation (hunt, greet, listen, TTS) until true session end.
 * /stream waits for the next session rather than tearing down the USB host.
 */
void nino_camera_set_session_active(bool active);

bool nino_camera_session_is_active(void);

/** True after uvc_host_stream_start for the current voice session. */
bool nino_camera_is_streaming(void);

/** Wait until UVC is streaming, or @p timeout_ms elapses. */
bool nino_camera_wait_streaming(uint32_t timeout_ms);

#ifdef __cplusplus
}
#endif

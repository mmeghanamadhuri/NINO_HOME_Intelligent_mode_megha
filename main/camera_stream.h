#pragma once

#include <stdbool.h>

#ifdef __cplusplus
extern "C" {
#endif

/**
 * USB UVC stays enumerated; frames are grabbed/logged/streamed only while a
 * voice session is open. /stream waits for the next session rather than
 * tearing down the USB host.
 */
void nino_camera_set_session_active(bool active);

bool nino_camera_session_is_active(void);

#ifdef __cplusplus
}
#endif

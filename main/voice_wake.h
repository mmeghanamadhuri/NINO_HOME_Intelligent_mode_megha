#pragma once

#include <stdbool.h>

#ifdef __cplusplus
extern "C" {
#endif

/** Start AFE + WakeNet feed/fetch tasks (after NVS, audio init, voice URI mutex). */
void nino_voice_wake_init(void);

void nino_voice_wake_set_enabled(bool on);
bool nino_voice_wake_is_enabled(void);

/** True if WakeNet/AFE tasks were created (model partition flashed). */
bool nino_voice_wake_hw_ready(void);

/**
 * Kept for API compatibility after switching to USB header mic.
 * Wake feed pauses during after-wake via s_after_wake_busy instead of closing a codec mic.
 */
void nino_voice_wake_drop_mic_locked(void);

/** Pause wake_feed USB reads while VAD or other capture owns the mic ring. */
void nino_voice_wake_set_mic_capture_hold(bool hold);

#ifdef __cplusplus
}
#endif

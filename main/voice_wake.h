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
 * Call while holding `nino_audio_bus_lock()` after any other code path has opened/closed
 * the ES8311 (e.g. WAV playback at 22.05 kHz or VAD mic). Drops the wake task's mic handle
 * so the next read re-opens at 16 kHz. Prevents "i2s_channel_read: channel is not enabled".
 */
void nino_voice_wake_drop_mic_locked(void);

#ifdef __cplusplus
}
#endif

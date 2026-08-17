#include "voice_wake.h"

#include "mic_input.h"

/* ESP-SR/WakeNet is intentionally not part of this fixed-duration recorder.
 * These no-op compatibility hooks keep the speaker/mic ownership interface
 * stable for code that shares the ES8311 duplex I2S bus. */
void nino_voice_wake_init(void) {}
void nino_voice_wake_set_enabled(bool on) { (void)on; }
bool nino_voice_wake_is_enabled(void) { return false; }
bool nino_voice_wake_hw_ready(void) { return false; }
void nino_voice_wake_drop_mic_locked(void) { nino_mic_drop_es8311_locked(); }
void nino_voice_wake_set_mic_capture_hold(bool hold) { (void)hold; }
void nino_voice_wake_release_after_wake(void) {}

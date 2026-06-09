#pragma once

#include <stdbool.h>

#ifdef __cplusplus
extern "C" {
#endif

/*
 * NINO eye animation engine (idle piece).
 *
 * Currently only the idle emotion is integrated: a neutral black eye on a
 * white background with a slow eyelid blink, mirrored on both OLEDs. More
 * emotions (happy/sad/listening/...) slot in as extra states later.
 *
 * Integration:
 *   1) ssd1351_init();       // bring up the displays (once)
 *   2) nino_eye_begin();     // spawns the animator task, returns immediately
 *   3) (later) call per-emotion triggers from any task; the switch is
 *      instant and non-blocking.
 */
typedef enum {
    NINO_EYE_IDLE = 0,
    NINO_EYE_LISTENING,
    NINO_EYE_THINKING,
    NINO_EYE_STATE_COUNT,
} nino_eye_state_t;

void nino_eye_begin(void);

void nino_eye_set_state(nino_eye_state_t state);
nino_eye_state_t nino_eye_get_state(void);

/** Parse a console token: "0"-"2", or idle/listening/thinking. Returns false if unknown. */
bool nino_eye_apply_command(const char *line);

/* ---- Per-emotion triggers ---- */
void nino_eye_idle(void);
void nino_eye_listening(void);
void nino_eye_thinking(void);

#ifdef __cplusplus
}
#endif

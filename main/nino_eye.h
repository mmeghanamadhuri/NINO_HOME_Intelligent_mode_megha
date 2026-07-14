#pragma once

#include <stdbool.h>

#ifdef __cplusplus
extern "C" {
#endif

/*
 * NINO eye animation engine.
 *
 * Ten states rendered on the dual SSD1351 OLEDs (idle + 6 emotions + the two
 * functional states listening/thinking + the med capsule for medical reminders).
 * State changes are instant and non-blocking: the running animation switches on
 * its next frame.
 *
 * Integration:
 *   1) ssd1351_init();       // bring up the displays (once)
 *   2) nino_eye_begin();     // spawns the animator task, returns immediately
 *   3) call any per-emotion trigger (or nino_eye_apply_expression) from any task.
 */
typedef enum {
    NINO_EYE_IDLE = 0,
    NINO_EYE_HAPPY,
    NINO_EYE_TIRED,
    NINO_EYE_THINKING,
    NINO_EYE_CURIOUS_QUIZ,
    NINO_EYE_SAD,
    NINO_EYE_SURPRISED,
    NINO_EYE_LISTENING,
    NINO_EYE_RECALLING,
    NINO_EYE_MED,
    NINO_EYE_STATE_COUNT,
} nino_eye_state_t;

void nino_eye_begin(void);

/** Break out of the current animation loop and redraw (e.g. after SPI CS reclaim). */
void nino_eye_restart_current(void);

void nino_eye_set_state(nino_eye_state_t state);
nino_eye_state_t nino_eye_get_state(void);

/** Parse a console token: "0"-"9", or idle/happy/tired/.../recalling/med. Returns false if unknown. */
bool nino_eye_apply_command(const char *line);

/**
 * Map a lowercase expression name (as sent by the PC server, e.g. "sad",
 * "happy", "curious", "recalling", ...) to a state. Returns NINO_EYE_STATE_COUNT
 * if the name is unknown / NULL / empty.
 */
nino_eye_state_t nino_eye_state_from_name(const char *name);

/**
 * Apply a server expression tag: shows the matching emotion, or returns the
 * eyes to idle when @p name is NULL/empty/unknown. Mirrors the server contract
 * where a missing eye_expression key means "stay idle for this reply".
 */
void nino_eye_apply_expression(const char *name);

/* ---- Per-emotion triggers ---- */
void nino_eye_idle(void);
void nino_eye_happy(void);
void nino_eye_tired(void);
void nino_eye_thinking(void);
void nino_eye_curious(void);
void nino_eye_sad(void);
void nino_eye_surprised(void);
void nino_eye_listening(void);
void nino_eye_recalling(void);
/** Static slanted red/white capsule pill — shown while a medical reminder plays. */
void nino_eye_med(void);

#ifdef __cplusplus
}
#endif

#pragma once

#include <stdbool.h>

#ifdef __cplusplus
extern "C" {
#endif

/*
 * NINO eye animation engine.
 *
 * States rendered on dual eye panels (SSD1351 OLED or ST7735 TFT): idle +
 * animated emotions + med capsule + static RGB565 emoji bitmaps
 * (jai_bhalaiah … bigsmile). State changes are instant and non-blocking: the
 * running animation switches on its next frame.
 *
 * Integration:
 *   1) nino_display_init();  // bring up the displays (once; Kconfig selects panel)
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
    NINO_EYE_MAD,
    NINO_EYE_MED,
    NINO_EYE_JAI_BHALAIAH,
    NINO_EYE_SMILE,
    NINO_EYE_SPARKLE,
    NINO_EYE_PENCIL,
    NINO_EYE_RADIO,
    NINO_EYE_TV,
    NINO_EYE_BULB,
    NINO_EYE_ROBOT,
    NINO_EYE_BIGSMILE,
    NINO_EYE_STATE_COUNT,
} nino_eye_state_t;

/** Alias kept for older call sites / server tags. */
#define NINO_EYE_TWINKLE NINO_EYE_SPARKLE

void nino_eye_begin(void);

/** Break out of the current animation loop and redraw (e.g. after SPI CS reclaim). */
void nino_eye_restart_current(void);

void nino_eye_set_state(nino_eye_state_t state);
nino_eye_state_t nino_eye_get_state(void);

/** Parse a console token / line: state names (prefer names over digits). */
bool nino_eye_apply_command(const char *line);

/**
 * Map a lowercase expression name (as sent by the PC server, e.g. "sad",
 * "happy", "curious", "recalling", "sparkle", "heart", …) to a state. Returns
 * NINO_EYE_STATE_COUNT if the name is unknown / NULL / empty.
 * "heart" is the red heart (identify). "happy" uses the coloured smile emoji.
 */
nino_eye_state_t nino_eye_state_from_name(const char *name);

/** Reverse lookup for logs / console (returns "?" if unknown). */
const char *nino_eye_state_to_name(nino_eye_state_t state);

/**
 * Apply a server expression tag: shows the matching emotion, or returns the
 * eyes to idle when @p name is NULL/empty/unknown. Mirrors the server contract
 * where a missing eye_expression key means "stay idle for this reply".
 */
void nino_eye_apply_expression(const char *name);

/**
 * Demo-only faster idle blink pace (shorter open-hold before blink).
 * Normal idle (~5.6 s/cycle) is unchanged when disabled. Enable around the
 * UK IFA demo cue timeline so short idle gaps can still show a blink.
 */
void nino_eye_set_demo_idle_pace(bool enabled);

/* ---- Per-emotion triggers ---- */
void nino_eye_idle(void);
void nino_eye_happy(void);
/** Red heart — same as nino_eye_happy(); used when a person is identified. */
void nino_eye_heart(void);
void nino_eye_tired(void);
void nino_eye_thinking(void);
void nino_eye_curious(void);
void nino_eye_sad(void);
void nino_eye_surprised(void);
void nino_eye_listening(void);
void nino_eye_recalling(void);
void nino_eye_mad(void);
/** Static slanted red/white capsule pill — shown while a medical reminder plays. */
void nino_eye_med(void);
/** 🔥 fire emoji bitmap (jai Bhalaiah). */
void nino_eye_jai_bhalaiah(void);
/** 😊 WhatsApp-style smile bitmap. */
void nino_eye_smile(void);
/** ✨ sparkle bitmap. */
void nino_eye_sparkle(void);
/** Alias for nino_eye_sparkle(). */
void nino_eye_twinkle(void);
/** ✏️ pencil emoji bitmap. */
void nino_eye_pencil(void);
/** 📻 radio emoji bitmap. */
void nino_eye_radio(void);
/** 📺 TV emoji bitmap. */
void nino_eye_tv(void);
/** 💡 bulb emoji bitmap. */
void nino_eye_bulb(void);
/** 🤖 robot emoji bitmap. */
void nino_eye_robot(void);
/** 😄 open-mouth big smile bitmap. */
void nino_eye_bigsmile(void);

#ifdef __cplusplus
}
#endif

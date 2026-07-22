"""Drive the device eyes from the camera's facial emotion.

Behavior (lowest priority on the device — see priority notes below):
  * Watch the primary (largest) face's emotion every frame.
  * When the same mapped emotion holds continuously for EMOTION_EYE_STABLE_SECONDS
    (~1.5-2 s), commit it to the eyes and LATCH the display for
    EMOTION_EYE_DISPLAY_SECONDS (4-5 s). During the latch, new emotions are
    ignored — frames still flow, but the eyes hold the committed emotion.
  * When the stable reading is neutral / no clear emotion / no face, return the
    eyes to idle (after a short debounce).

Priority (highest wins; all of these preempt and abandon an emotion latch):
  P0  active voice query          -> voice_active_fn()
  P0/P1 alarm / medical reminder  -> device_busy_speaking() (all fire via /play_wav)
        + any vision greeting      -> speaking_fn()
  P2  this emotion driver
  P3  idle
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Callable

from esp_playback import device_busy_speaking, post_eye_expression_to_esp

logger = logging.getLogger(__name__)

# Camera emotion label (lowercased) -> device eye state. Anything not listed
# (disgust, uncertain, unknown, missing) falls back to idle.
EMOTION_TO_EYE: dict[str, str] = {
    "happy": "happy",
    "sad": "sad",
    "angry": "sad",
    "surprise": "surprised",
    "fear": "surprised",
    "neutral": "idle",
}

_ACTIVE_EMOTIONS = frozenset({"happy", "sad", "surprised"})


def _env_flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float, *, minimum: float = 0.0) -> float:
    try:
        return max(minimum, float(os.environ.get(name, default)))
    except (TypeError, ValueError):
        return default


class VisionEyeDriver:
    """Turn per-frame emotion labels into debounced, latched eye commands."""

    def __init__(
        self,
        *,
        voice_active_fn: Callable[[], bool] | None = None,
        speaking_fn: Callable[[], bool] | None = None,
        push_fn: Callable[[str], bool] | None = None,
    ) -> None:
        self._enabled = _env_flag("EMOTION_EYES_ENABLED", True)
        self._stable_s = _env_float("EMOTION_EYE_STABLE_SECONDS", 1.75, minimum=0.5)
        self._display_s = _env_float("EMOTION_EYE_DISPLAY_SECONDS", 4.5, minimum=1.0)
        self._idle_debounce_s = _env_float(
            "EMOTION_EYE_IDLE_DEBOUNCE_SECONDS", 1.5, minimum=0.0
        )
        self._suppress_after_voice_s = _env_float(
            "EMOTION_EYE_SUPPRESS_AFTER_VOICE_SECONDS", 8.0, minimum=0.0
        )

        self._voice_active_fn = voice_active_fn
        self._speaking_fn = speaking_fn
        self._push_fn = push_fn
        self._device_id: str | None = None

        self._candidate: str | None = None
        self._candidate_since = 0.0
        self._display_until = 0.0
        self._last_pushed: str | None = None
        self._last_high_priority_at = 0.0

        if self._enabled:
            logger.info(
                "Vision eye driver on (stable=%.1fs display=%.1fs idle_debounce=%.1fs "
                "voice_cooldown=%.1fs)",
                self._stable_s,
                self._display_s,
                self._idle_debounce_s,
                self._suppress_after_voice_s,
            )

    @property
    def enabled(self) -> bool:
        return self._enabled

    def update(
        self,
        results: list[dict[str, Any]] | None,
        *,
        device_id: str | None = None,
        now: float | None = None,
    ) -> None:
        """Feed one frame's face results. Safe to call every frame."""
        if not self._enabled:
            return
        self._device_id = device_id
        now = time.time() if now is None else now

        # P0/P1: a voice query, alarm/medical reminder, or greeting owns the eyes.
        if self._high_priority_active():
            self._abandon_latch()
            # Speech ends on idle (firmware reverts after /play_wav), so track that.
            self._last_pushed = "idle"
            self._last_high_priority_at = now
            return

        # Short cooldown after speech so the eyes don't snap to emotion mid-tail.
        if now - self._last_high_priority_at < self._suppress_after_voice_s:
            return

        # During the display latch, ignore emotion entirely — hold what we committed.
        if now < self._display_until:
            return

        target = self._target_from_results(results)
        if target != self._candidate:
            self._candidate = target
            self._candidate_since = now
        held = now - self._candidate_since

        if target in _ACTIVE_EMOTIONS:
            if held >= self._stable_s and self._last_pushed != target:
                if self._push(target):
                    self._last_pushed = target
                    self._display_until = now + self._display_s
        else:  # idle (neutral / no face / unclear)
            if held >= self._idle_debounce_s and self._last_pushed != "idle":
                if self._push("idle"):
                    self._last_pushed = "idle"

    def _abandon_latch(self) -> None:
        self._candidate = None
        self._candidate_since = 0.0
        self._display_until = 0.0

    def _high_priority_active(self) -> bool:
        if self._voice_active_fn is not None and self._voice_active_fn():
            return True
        if self._speaking_fn is not None and self._speaking_fn():
            return True
        return device_busy_speaking()

    def _target_from_results(self, results: list[dict[str, Any]] | None) -> str:
        label = self._primary_emotion(results)
        if not label:
            return "idle"
        return EMOTION_TO_EYE.get(label, "idle")

    @staticmethod
    def _primary_emotion(results: list[dict[str, Any]] | None) -> str | None:
        """Emotion label of the largest face (primary flag wins ties)."""
        best_label: str | None = None
        best_weight = -1.0
        for result in results or []:
            box = result.get("box")
            if not isinstance(box, dict):
                continue
            emotion = str(result.get("emotion") or "").strip().lower()
            if not emotion:
                continue
            area = float(int(box.get("w", 0)) * int(box.get("h", 0)))
            weight = area + (1e12 if result.get("primary") else 0.0)
            if weight > best_weight:
                best_weight = weight
                best_label = emotion
        return best_label

    def _push(self, name: str) -> bool:
        if self._push_fn is not None:
            ok = self._push_fn(name)
        else:
            ok = post_eye_expression_to_esp(name, device_id=self._device_id)
        if ok:
            logger.info("Vision emotion -> eye %s (device=%s)", name, self._device_id)
        else:
            logger.warning("Vision eye push failed (%s, device=%s)", name, self._device_id)
        return ok

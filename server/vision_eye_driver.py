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
from dataclasses import dataclass
from typing import Any, Callable

from eye_expression import BITMAP_EYE_EXPRESSIONS, spatial_eye_from_text
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

_ACTIVE_EMOTIONS = frozenset({"happy", "sad", "surprised"}) | frozenset(BITMAP_EYE_EXPRESSIONS)


@dataclass
class _EyeLatch:
    candidate: str | None = None
    candidate_since: float = 0.0
    display_until: float = 0.0
    last_pushed: str | None = None
    last_high_priority_at: float = 0.0


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
        self._latches: dict[str, _EyeLatch] = {}

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
        scene_context: str = "",
        now: float | None = None,
    ) -> None:
        """Feed one frame's face results. Safe to call every frame."""
        if not self._enabled:
            return
        latch = self._latch(device_id)
        now = time.time() if now is None else now

        # P0/P1: a voice query, alarm/medical reminder, or greeting owns the eyes.
        if self._high_priority_active(device_id):
            self._abandon_latch(latch)
            # Speech ends on idle (firmware reverts after /play_wav), so track that.
            latch.last_pushed = "idle"
            latch.last_high_priority_at = now
            return

        # Short cooldown after speech so the eyes don't snap to emotion mid-tail.
        if now - latch.last_high_priority_at < self._suppress_after_voice_s:
            return

        # During the display latch, ignore emotion entirely — hold what we committed.
        if now < latch.display_until:
            return

        # A latched expression must explicitly return to idle when its display
        # window ends. Reset the candidate timer too, so a continuing face must
        # be stable again before it can claim the eyes for another window.
        if latch.display_until > 0.0:
            latch.display_until = 0.0
            latch.candidate = None
            latch.candidate_since = now
            if latch.last_pushed in _ACTIVE_EMOTIONS:
                latch.last_pushed = "idle" if self._push("idle", device_id) else None

        target = self._target_from_results(results, scene_context=scene_context)
        if target != latch.candidate:
            latch.candidate = target
            latch.candidate_since = now
        held = now - latch.candidate_since

        if target in _ACTIVE_EMOTIONS:
            if held >= self._stable_s and latch.last_pushed != target:
                if self._push(target, device_id):
                    latch.last_pushed = target
                    latch.display_until = now + self._display_s
        else:  # idle (neutral / no face / unclear)
            if held >= self._idle_debounce_s and latch.last_pushed != "idle":
                if self._push("idle", device_id):
                    latch.last_pushed = "idle"

    def _latch(self, device_id: str | None) -> _EyeLatch:
        key = str(device_id or "").strip()
        found = self._latches.get(key)
        if found is None:
            found = _EyeLatch()
            self._latches[key] = found
        return found

    def _abandon_latch(self, latch: _EyeLatch) -> None:
        latch.candidate = None
        latch.candidate_since = 0.0
        latch.display_until = 0.0

    def _call_flag(self, fn: Callable[..., bool] | None, device_id: str | None) -> bool:
        if fn is None:
            return False
        try:
            return bool(fn(device_id) if device_id else fn())
        except TypeError:
            return bool(fn())

    def _high_priority_active(self, device_id: str | None = None) -> bool:
        if self._call_flag(self._voice_active_fn, device_id):
            return True
        if self._call_flag(self._speaking_fn, device_id):
            return True
        return device_busy_speaking(device_id)

    def _target_from_results(
        self,
        results: list[dict[str, Any]] | None,
        *,
        scene_context: str = "",
    ) -> str:
        label = self._primary_emotion(results)
        if label and label not in {"neutral", "uncertain", "unknown"}:
            mapped = EMOTION_TO_EYE.get(label, "idle")
            if mapped != "idle":
                return mapped
        if scene_context:
            spatial = spatial_eye_from_text(scene_context)
            if spatial:
                return spatial
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

    def _push(self, name: str, device_id: str | None = None) -> bool:
        if self._push_fn is not None:
            ok = self._push_fn(name)
        else:
            ok = post_eye_expression_to_esp(name, device_id=device_id)
        if ok:
            logger.info("Vision emotion -> eye %s (device=%s)", name, device_id)
        else:
            logger.warning("Vision eye push failed (%s, device=%s)", name, device_id)
        return ok

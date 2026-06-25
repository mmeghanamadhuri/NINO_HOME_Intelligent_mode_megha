"""Coordinate voice (P0) vs vision-emotion (P1) so they never collide."""

from __future__ import annotations

import os
import threading
import time

_lock = threading.Lock()
_voice_depth = 0
_suppress_vision_until: float = 0.0


def _after_voice_cooldown_seconds() -> float:
    return float(os.environ.get("VISION_EMOTION_AFTER_VOICE_SECONDS", "90"))


def begin_voice_query() -> None:
    """Call when a voice WebSocket utterance starts processing (P0)."""
    global _voice_depth
    with _lock:
        _voice_depth += 1


def end_voice_query() -> None:
    """Call when voice reply has been sent (or failed) — starts vision cooldown."""
    global _voice_depth, _suppress_vision_until
    with _lock:
        _voice_depth = max(0, _voice_depth - 1)
        if _voice_depth == 0:
            _suppress_vision_until = time.time() + _after_voice_cooldown_seconds()


def voice_pipeline_active() -> bool:
    with _lock:
        return _voice_depth > 0


def vision_emotion_blocked() -> bool:
    """True when vision emotion must not accumulate or speak (voice wins)."""
    with _lock:
        if _voice_depth > 0:
            return True
        return time.time() < _suppress_vision_until


def notify_voice_interaction() -> None:
    """Align with TTS vision suppress after a completed voice turn."""
    global _suppress_vision_until
    with _lock:
        _suppress_vision_until = time.time() + _after_voice_cooldown_seconds()


def status() -> dict[str, object]:
    with _lock:
        return {
            "voice_active": _voice_depth > 0,
            "voice_depth": _voice_depth,
            "vision_blocked_until": _suppress_vision_until,
            "vision_blocked": time.time() < _suppress_vision_until or _voice_depth > 0,
        }

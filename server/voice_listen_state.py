"""In-memory voice session flags for post-TTS listen grace."""

from __future__ import annotations

import os
import threading
import time

_lock = threading.Lock()
_state: dict[str, dict[str, float | bool]] = {}


def _session_key(session_id: str = "", device_id: str = "") -> str:
    sid = str(session_id or "").strip()
    if sid:
        return sid
    did = str(device_id or "").strip()
    return did or "__global__"


def post_tts_grace_seconds() -> float:
    return max(0.0, float(os.environ.get("VOICE_POST_TTS_GRACE_SECONDS", "4.0")))


def post_tts_grace_tts_factor() -> float:
    return max(0.0, float(os.environ.get("VOICE_POST_TTS_GRACE_TTS_FACTOR", "0.35")))


def mark_session_open(session_id: str = "", device_id: str = "") -> None:
    key = _session_key(session_id, device_id)
    with _lock:
        entry = _state.setdefault(key, {})
        entry["continue_active"] = True


def mark_session_closed(session_id: str = "", device_id: str = "") -> None:
    key = _session_key(session_id, device_id)
    with _lock:
        _state.pop(key, None)


def mark_tts_playback(
    session_id: str = "",
    device_id: str = "",
    *,
    tts_seconds: float = 0.0,
    audio_out_seconds: float = 0.0,
) -> None:
    """Extend post-TTS grace after a spoken reply is sent to the device."""
    duration = max(float(tts_seconds), float(audio_out_seconds), 0.0)
    # Cover full playback plus a fixed echo buffer; the old factor-based formula
    # expired before long spatial greetings finished and the mic captured TTS echo.
    grace_until = time.time() + duration + post_tts_grace_seconds()
    grace_until += duration * post_tts_grace_tts_factor()
    key = _session_key(session_id, device_id)
    with _lock:
        entry = _state.setdefault(key, {})
        entry["continue_active"] = True
        entry["grace_until"] = max(float(entry.get("grace_until", 0.0)), grace_until)


def in_post_tts_grace(session_id: str = "", device_id: str = "") -> bool:
    key = _session_key(session_id, device_id)
    now = time.time()
    with _lock:
        entry = _state.get(key)
        if not entry:
            return False
        return now < float(entry.get("grace_until", 0.0))


def session_continue_active(session_id: str = "", device_id: str = "") -> bool:
    key = _session_key(session_id, device_id)
    with _lock:
        entry = _state.get(key)
        return bool(entry and entry.get("continue_active"))


def should_preserve_continue_listen(
    session_id: str = "",
    device_id: str = "",
    *,
    session_kind: str = "",
) -> bool:
    if str(session_kind or "").strip().lower() not in {
        "continue",
        "conv",
        "followup",
        "ack",
    }:
        return False
    return session_continue_active(session_id, device_id) or in_post_tts_grace(
        session_id, device_id
    )

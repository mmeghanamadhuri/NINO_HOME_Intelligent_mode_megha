"""Session-gated camera helpers for Intelligent Mode.

NiNO firmware only streams UVC/MJPEG during active voice sessions. Idle bots
return HTTP 503 on /snapshot.jpg by design — not a fault.
"""

from __future__ import annotations

import urllib.error
import urllib.request
from typing import Any, Callable

VoiceActiveFn = Callable[[str | None], bool] | None

# HTTP codes that mean "camera idle" when no voice session is active.
_IDLE_SNAPSHOT_CODES = frozenset({503})


def bot_runtime_for(snapshot: dict[str, Any], device_id: str) -> dict[str, Any]:
    runtime = snapshot.get("bot_runtime") or {}
    if not isinstance(runtime, dict):
        return {}
    row = runtime.get(device_id)
    return dict(row) if isinstance(row, dict) else {}


def camera_session_active(snapshot: dict[str, Any], device_id: str) -> bool:
    runtime = bot_runtime_for(snapshot, device_id)
    return bool(runtime.get("session_active"))


def camera_expects_stream(
    snapshot: dict[str, Any],
    device_id: str,
    *,
    voice_active_fn: VoiceActiveFn = None,
) -> bool:
    """True when the architecture expects live MJPEG from this bot."""
    if voice_active_fn is not None:
        try:
            if voice_active_fn(device_id):
                return True
        except Exception:
            pass
    return camera_session_active(snapshot, device_id)


def probe_bot_snapshot(
    base_url: str, *, timeout: float = 3.0
) -> tuple[bool, str, int | None]:
    """GET /snapshot.jpg — returns (ok, message, http_status_code)."""
    url = base_url.rstrip("/") + "/snapshot.jpg"
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if 200 <= resp.status < 300:
                return True, "", resp.status
            return False, f"HTTP {resp.status}", resp.status
    except urllib.error.HTTPError as exc:
        return False, f"HTTP {exc.code}", exc.code
    except Exception as exc:
        return False, str(exc), None


def snapshot_acceptable(
    ok: bool,
    err: str,
    http_code: int | None,
    *,
    expects_stream: bool,
) -> bool:
    if ok:
        return True
    if not expects_stream and http_code in _IDLE_SNAPSHOT_CODES:
        return True
    return False


def camera_stream_fault(
    snapshot: dict[str, Any],
    device_id: str,
    cam: dict[str, Any],
    *,
    voice_active_fn: VoiceActiveFn = None,
) -> bool:
    """True when camera should be live but server-side MJPEG is not connected."""
    if not camera_expects_stream(snapshot, device_id, voice_active_fn=voice_active_fn):
        return False
    return not bool(cam.get("connected"))


def classify_camera_state(
    snapshot: dict[str, Any],
    device_id: str,
    cam: dict[str, Any],
    *,
    voice_active_fn: VoiceActiveFn = None,
) -> str:
    """Return idle | in_session | live | fault | unknown."""
    expects = camera_expects_stream(snapshot, device_id, voice_active_fn=voice_active_fn)
    connected = bool(cam.get("connected"))
    runtime = bot_runtime_for(snapshot, device_id)
    streaming = bool(runtime.get("streaming"))

    if connected:
        return "live"
    if expects:
        err = str(cam.get("last_error") or "").strip()
        if err:
            return "fault"
        if streaming or runtime.get("session_active"):
            return "in_session"
        return "fault"
    if runtime.get("session_active") is False or runtime:
        return "idle"
    return "idle"

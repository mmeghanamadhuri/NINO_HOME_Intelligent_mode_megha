"""Shared hook so vision backends can skip work during voice/spatial scans."""

from __future__ import annotations

from collections.abc import Callable

_inference_paused: Callable[[str | None], bool] | None = None


def configure_vision_inference_paused(
    fn: Callable[[str | None], bool] | None,
) -> None:
    global _inference_paused
    _inference_paused = fn


def vision_inference_paused(device_id: str | None = None) -> bool:
    if _inference_paused is None:
        return False
    try:
        return bool(_inference_paused(device_id))
    except Exception:
        return False

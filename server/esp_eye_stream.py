"""Push eye expression updates to ESP firmware independently from WAV playback."""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from eye_expression import normalize_eye_expression

logger = logging.getLogger(__name__)


def _enabled() -> bool:
    raw = os.environ.get("VISION_EYE_STREAM_ENABLED", "1").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _expression_url() -> str | None:
    play_wav_url = os.environ.get("ESP_PLAY_WAV_URL", "").strip()
    if not play_wav_url:
        return None
    parsed = urllib.parse.urlparse(play_wav_url)
    if not parsed.scheme or not parsed.netloc:
        return None
    return f"{parsed.scheme}://{parsed.netloc}/eye/expression"


class EspEyeStream:
    """Low-rate state sync from server vision to ESP eye expression."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last_sent = ""
        self._last_sent_at = 0.0
        self._last_error_at = 0.0
        self._min_send_gap_s = float(os.environ.get("VISION_EYE_STREAM_MIN_GAP_S", "0.25"))

    def publish(self, expression: str | None) -> None:
        if not _enabled():
            return
        url = _expression_url()
        if not url:
            return

        normalized = normalize_eye_expression(expression) or "idle"
        now = time.time()

        with self._lock:
            if normalized == self._last_sent and (now - self._last_sent_at) < self._min_send_gap_s:
                return

        payload = json.dumps({"expression": normalized}).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=payload,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=1.5) as resp:
                if resp.status != 200:
                    raise RuntimeError(f"ESP eye expression HTTP {resp.status}")
                _ = resp.read()
            with self._lock:
                self._last_sent = normalized
                self._last_sent_at = now
        except (urllib.error.URLError, urllib.error.HTTPError, RuntimeError) as exc:
            with self._lock:
                if (now - self._last_error_at) >= 5.0:
                    logger.warning("ESP eye stream failed: %s", exc)
                    self._last_error_at = now

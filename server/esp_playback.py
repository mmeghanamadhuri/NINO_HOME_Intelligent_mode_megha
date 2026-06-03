"""POST WAV audio to the ESP32 /play_wav endpoint."""

from __future__ import annotations

import logging
import os
import urllib.error
import urllib.request

logger = logging.getLogger(__name__)


def esp_play_wav_url() -> str | None:
    url = os.environ.get("ESP_PLAY_WAV_URL", "").strip()
    return url if url else None


def post_wav_to_esp(wav: bytes, *, timeout: float = 60.0) -> None:
    """Queue raw WAV bytes on the board speaker via POST /play_wav."""
    url = esp_play_wav_url()
    if not url:
        raise RuntimeError("ESP_PLAY_WAV_URL is not set")

    req = urllib.request.Request(
        url,
        data=wav,
        method="POST",
        headers={"Content-Type": "audio/wav"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status != 200:
                raise RuntimeError(f"ESP play_wav HTTP {resp.status}")
            body = resp.read()
            if b'"ok":true' not in body and b'"ok": true' not in body:
                if b'"ok":false' in body or b'"ok": false' in body:
                    raise RuntimeError(body.decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace") if exc.fp else str(exc)
        raise RuntimeError(f"ESP HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"ESP URL error: {exc}") from exc

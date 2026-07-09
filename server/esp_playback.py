"""POST WAV audio to the ESP32 /play_wav endpoint."""

from __future__ import annotations

import logging
import os
import urllib.error
import urllib.parse
import urllib.request

logger = logging.getLogger(__name__)

# ESP32 main.c MAX_PLAY_WAV_BYTES — keep a little under for safety
ESP_MAX_PLAY_WAV_BYTES = int(os.environ.get("ESP_MAX_PLAY_WAV_BYTES", str(380 * 1024)))


def derive_esp_base_url(camera_source: str) -> str | None:
    """Extract http://host from CAMERA_SOURCE (e.g. http://192.168.0.89/stream)."""
    raw = (camera_source or "").strip()
    if not raw.lower().startswith(("http://", "https://")):
        return None
    parsed = urllib.parse.urlparse(raw)
    if parsed.scheme and parsed.netloc:
        return f"{parsed.scheme}://{parsed.netloc}"
    return None


def ensure_esp_play_wav_url_configured(*, camera_source: str | None = None) -> str | None:
    """Set ESP_PLAY_WAV_URL from CAMERA_SOURCE when unset."""
    existing = os.environ.get("ESP_PLAY_WAV_URL", "").strip()
    if existing:
        return existing
    cam = camera_source if camera_source is not None else os.environ.get("CAMERA_SOURCE", "")
    base = derive_esp_base_url(cam)
    if not base:
        return None
    url = f"{base}/play_wav"
    os.environ["ESP_PLAY_WAV_URL"] = url
    logger.info("ESP_PLAY_WAV_URL derived from camera source: %s", url)
    return url


def esp_play_wav_url() -> str | None:
    url = os.environ.get("ESP_PLAY_WAV_URL", "").strip()
    if url:
        return url
    return ensure_esp_play_wav_url_configured()


def post_wav_to_esp(
    wav: bytes,
    *,
    timeout: float = 60.0,
    prompt_ack: bool = False,
    eye_expression: str | None = None,
) -> None:
    """Queue raw WAV bytes on the board speaker via POST /play_wav."""
    url = esp_play_wav_url()
    if not url:
        raise RuntimeError(
            "ESP_PLAY_WAV_URL is not set (set it or use CAMERA_SOURCE=http://<ESP_IP>/stream)"
        )
    if not wav:
        raise RuntimeError("WAV payload is empty")
    if len(wav) > ESP_MAX_PLAY_WAV_BYTES:
        raise RuntimeError(
            f"WAV too large for ESP ({len(wav)} bytes; max {ESP_MAX_PLAY_WAV_BYTES})"
        )

    headers = {"Content-Type": "audio/wav"}
    if prompt_ack:
        headers["X-Nino-Prompt-Ack"] = "1"
    if eye_expression:
        headers["X-Nino-Eye-Expression"] = eye_expression.strip().lower()
    req = urllib.request.Request(
        url,
        data=wav,
        method="POST",
        headers=headers,
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

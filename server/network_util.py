"""LAN / URL helpers for ESP ↔ PC voice WebSocket pairing."""

from __future__ import annotations

import logging
import os
import socket

logger = logging.getLogger(__name__)


def public_http_base() -> str:
    """http://<lan-host>:<port> that robots can reach this server on."""
    host = os.environ.get("NINO_SERVER_LAN_HOST", "").strip() or guess_lan_ipv4()
    if not host:
        return ""
    port = int(os.environ.get("NINO_SERVER_PORT", "8000"))
    return f"http://{host}:{port}"


def guess_lan_ipv4() -> str:
    """Best-effort local IPv4 used to reach the internet (not 127.0.0.1)."""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.connect(("8.8.8.8", 80))
            return str(sock.getsockname()[0])
        finally:
            sock.close()
    except OSError:
        return ""


def voice_ws_url_for_esp(
    *,
    port: int | None = None,
    device_id: str | None = None,
) -> str | None:
    """
    WebSocket URL the ESP should use to reach this PC's voice pipeline.

    Prefer VOICE_WS_URL; else NINO_SERVER_LAN_HOST + port; else guessed LAN IP.
    When device_id is set, appends ?device_id= (or replaces an existing one).
    """
    explicit = os.environ.get("VOICE_WS_URL", "").strip()
    if explicit:
        url = explicit
    else:
        host = os.environ.get("NINO_SERVER_LAN_HOST", "").strip()
        if not host:
            host = guess_lan_ipv4()
        if not host:
            return None

        if port is None:
            port = int(os.environ.get("NINO_SERVER_PORT", "8000"))
        path = os.environ.get("VOICE_WS_PATH", "/voice-query").strip() or "/voice-query"
        if not path.startswith("/"):
            path = "/" + path
        url = f"ws://{host}:{port}{path}"

    cleaned = (device_id or "").strip()
    if cleaned:
        base, _, _ = url.partition("?")
        url = f"{base}?device_id={cleaned}"

    logger.debug("Voice WS URL for ESP: %s", url)
    return url

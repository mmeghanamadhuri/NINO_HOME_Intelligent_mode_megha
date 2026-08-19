"""Persist Sirena Aux-in WAVs the P4 uploads to this voice server."""

from __future__ import annotations

import os
import re
from datetime import datetime
from pathlib import Path

DEFAULT_RECORDINGS_DIR = Path(__file__).resolve().parent / "recordings"
_DEVICE_RE = re.compile(r"[^A-Za-z0-9._-]+")


def recordings_dir() -> Path:
    raw = (os.environ.get("AUX_RECORDINGS_DIR") or "").strip()
    return Path(raw).expanduser() if raw else DEFAULT_RECORDINGS_DIR


def safe_device_id(device_id: str | None) -> str:
    cleaned = _DEVICE_RE.sub("-", (device_id or "").strip())[:64].strip(".-_")
    return cleaned or "unknown"


def save_aux_wav(
    wav: bytes,
    *,
    device_id: str = "",
    turn: int | None = None,
    energy: int | None = None,
    session: str = "",
    source: str = "aux",
) -> Path:
    if not wav:
        raise ValueError("empty audio")
    device = safe_device_id(device_id)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    parts = [stamp]
    if turn is not None:
        parts.append(f"t{max(0, int(turn))}")
    if energy is not None:
        parts.append(f"e{max(0, int(energy))}")
    session_tag = _DEVICE_RE.sub("-", (session or "").strip().lower())[:16]
    if session_tag:
        parts.append(session_tag)
    source_tag = _DEVICE_RE.sub("-", (source or "aux").strip().lower())[:16] or "aux"
    parts.append(source_tag)
    name = "_".join(parts) + ".wav"
    dest_dir = recordings_dir() / device
    dest_dir.mkdir(parents=True, exist_ok=True)
    path = dest_dir / name
    path.write_bytes(wav)
    return path


def list_aux_recordings(limit: int = 50) -> list[dict[str, object]]:
    root = recordings_dir()
    if not root.is_dir():
        return []
    files = sorted(root.glob("*/*.wav"), key=lambda p: p.stat().st_mtime, reverse=True)
    out: list[dict[str, object]] = []
    for path in files[: max(1, min(limit, 200))]:
        rel = path.relative_to(root).as_posix()
        out.append(
            {
                "path": rel,
                "device_id": path.parent.name,
                "name": path.name,
                "bytes": path.stat().st_size,
                "mtime": datetime.fromtimestamp(path.stat().st_mtime).isoformat(
                    timespec="seconds"
                ),
                "url": f"/recordings/{rel}",
            }
        )
    return out

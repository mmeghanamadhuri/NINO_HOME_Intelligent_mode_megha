"""Map users to one or more device MAC ids (many devices per person)."""

from __future__ import annotations

import json
import logging
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_PATH = BASE_DIR / "data" / "user_devices.json"

_MAC_HEX_RE = re.compile(r"[^0-9a-fA-F]")
_lock = threading.Lock()


def normalize_device_mac(raw: str | None) -> str:
    """Return 12 lowercase hex digits, or '' if raw is not a MAC."""
    hexdigits = _MAC_HEX_RE.sub("", str(raw or ""))
    if len(hexdigits) != 12:
        return ""
    return hexdigits.lower()


def format_device_mac(raw: str | None) -> str:
    hexdigits = normalize_device_mac(raw)
    if not hexdigits:
        return ""
    return ":".join(hexdigits[i : i + 2] for i in range(0, 12, 2)).upper()


def canonical_device_id(raw: str | None) -> str:
    """Return 12 lowercase hex, or '' if raw is not a MAC."""
    return normalize_device_mac(raw)


def _path() -> Path:
    return DEFAULT_PATH


def _empty_store() -> dict[str, Any]:
    return {"users": {}}


def _load_locked() -> dict[str, Any]:
    path = _path()
    if not path.is_file():
        return _empty_store()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        logger.exception("Could not read %s", path)
        return _empty_store()
    if not isinstance(raw, dict):
        return _empty_store()
    users = raw.get("users")
    if not isinstance(users, dict):
        users = {}
    return {"users": users}


def _write_locked(store: dict[str, Any]) -> None:
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(store, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def link_user_device(user_name: str, device_id: str) -> None:
    """Remember that this person uses this MAC / device id (many devices OK)."""
    name = str(user_name or "").strip()
    device = canonical_device_id(device_id)
    if not name or not device or name.lower() in {"unknown", "face"}:
        return
    if name.lower().startswith("guest"):
        return
    with _lock:
        store = _load_locked()
        users: dict[str, Any] = store.setdefault("users", {})
        entry = users.get(name)
        if not isinstance(entry, dict):
            entry = {"macs": []}
        macs = [
            canonical_device_id(item)
            for item in (entry.get("macs") or [])
            if canonical_device_id(item)
        ]
        if device not in macs:
            macs.append(device)
            logger.info("User %s linked to device %s", name, device)
        entry["macs"] = macs
        entry["updated_at"] = datetime.now(timezone.utc).isoformat()
        users[name] = entry
        _write_locked(store)


def devices_for_user(user_name: str) -> list[str]:
    name = str(user_name or "").strip()
    if not name:
        return []
    with _lock:
        store = _load_locked()
        entry = store.get("users", {}).get(name) or {}
        if not isinstance(entry, dict):
            return []
        out: list[str] = []
        for item in entry.get("macs") or []:
            key = canonical_device_id(item)
            if key and key not in out:
                out.append(key)
        return out


def users_for_device(device_id: str) -> list[str]:
    device = canonical_device_id(device_id)
    if not device:
        return []
    with _lock:
        store = _load_locked()
        names: list[str] = []
        for name, entry in (store.get("users") or {}).items():
            if not isinstance(entry, dict):
                continue
            macs = [canonical_device_id(item) for item in (entry.get("macs") or [])]
            if device in macs:
                names.append(str(name))
        return names

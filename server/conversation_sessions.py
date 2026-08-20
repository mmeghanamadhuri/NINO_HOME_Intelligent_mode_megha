"""Persist voice conversation sessions per user (JSON on disk)."""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_SESSIONS_DIR = Path(__file__).resolve().parent / "sessions"
_SLUG_RE = re.compile(r"[^a-zA-Z0-9._-]+")
_lock = threading.Lock()


def sessions_dir() -> Path:
    raw = (os.environ.get("VOICE_SESSIONS_DIR") or "").strip()
    return Path(raw).expanduser() if raw else DEFAULT_SESSIONS_DIR


def new_session_id() -> str:
    return uuid.uuid4().hex


def safe_slug(value: str | None, fallback: str = "unknown") -> str:
    cleaned = _SLUG_RE.sub("-", (value or "").strip().lower())[:64].strip(".-_")
    return cleaned or fallback


def _candidate_keys(device_id: str, user_name: str | None) -> list[str]:
    keys: list[str] = []
    if user_name and user_name.strip():
        keys.append(safe_slug(user_name))
    keys.append(safe_slug(device_id, "device"))
    seen: set[str] = set()
    out: list[str] = []
    for key in keys:
        if key not in seen:
            seen.add(key)
            out.append(key)
    return out


def _find_session(
    session_id: str, *, device_id: str = "", user_name: str | None = None
) -> tuple[str, dict[str, Any] | None]:
    for key in _candidate_keys(device_id, user_name):
        payload = load_session(key, session_id)
        if payload is not None:
            return key, payload
    write_key = _candidate_keys(device_id, user_name)[0]
    return write_key, None


def _session_path(user_key: str, session_id: str) -> Path:
    dest = sessions_dir() / safe_slug(user_key)
    dest.mkdir(parents=True, exist_ok=True)
    return dest / f"{safe_slug(session_id, 'session')}.json"


def _empty_session(
    session_id: str,
    *,
    device_id: str = "",
    user_name: str | None = None,
) -> dict[str, Any]:
    now = datetime.now().isoformat(timespec="seconds")
    return {
        "session_id": session_id,
        "device_id": device_id or "",
        "user_name": (user_name or "").strip(),
        "started_at": now,
        "ended_at": None,
        "turns": [],
    }


def load_session(user_key: str, session_id: str) -> dict[str, Any] | None:
    path = _session_path(user_key, session_id)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        logger.exception("Failed to read session %s / %s", user_key, session_id)
        return None


def _write_session(user_key: str, payload: dict[str, Any]) -> Path:
    session_id = str(payload.get("session_id") or new_session_id())
    payload["session_id"] = session_id
    path = _session_path(user_key, session_id)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)
    return path


def begin_session(
    session_id: str,
    *,
    device_id: str = "",
    user_name: str | None = None,
) -> dict[str, Any]:
    """Create or return the on-disk session record."""
    sid = (session_id or "").strip() or new_session_id()
    with _lock:
        write_key, existing = _find_session(sid, device_id=device_id, user_name=user_name)
        if existing:
            if user_name and not existing.get("user_name"):
                existing["user_name"] = user_name.strip()
                _write_session(write_key, existing)
            return existing
        payload = _empty_session(sid, device_id=device_id, user_name=user_name)
        _write_session(write_key, payload)
        logger.info("Voice session start id=%s user=%s device=%s", sid, write_key, device_id)
        return payload


def bind_session_user(
    session_id: str,
    *,
    device_id: str = "",
    user_name: str,
) -> dict[str, Any] | None:
    """Move/rewrite a session under the given user once they are identified."""
    name = (user_name or "").strip()
    sid = (session_id or "").strip()
    if not sid or not name:
        return None
    with _lock:
        write_key, payload = _find_session(sid, device_id=device_id, user_name=name)
        if payload is None:
            payload = _empty_session(sid, device_id=device_id, user_name=name)
        old_key = write_key
        payload["user_name"] = name
        new_key = safe_slug(name)
        path = _write_session(new_key, payload)
        if old_key != new_key:
            old_path = _session_path(old_key, sid)
            if old_path.is_file() and old_path.resolve() != path.resolve():
                try:
                    old_path.unlink()
                except OSError:
                    pass
        logger.info("Voice session bound id=%s user=%s", sid, new_key)
    try:
        from user_devices import link_user_device

        link_user_device(name, device_id)
    except Exception:
        logger.exception("Could not link user %s to device %s", name, device_id)
    return payload


def append_session_turn(
    session_id: str,
    *,
    device_id: str = "",
    user_name: str | None = None,
    user_text: str = "",
    assistant_text: str = "",
    reply_path: str = "",
) -> None:
    user = str(user_text or "").strip()
    assistant = str(assistant_text or "").strip()
    if not user or not assistant:
        return
    sid = (session_id or "").strip()
    if not sid:
        return
    with _lock:
        write_key, payload = _find_session(sid, device_id=device_id, user_name=user_name)
        if payload is None:
            payload = _empty_session(sid, device_id=device_id, user_name=user_name)
        if user_name:
            payload["user_name"] = user_name.strip()
            write_key = safe_slug(user_name)
        payload.setdefault("turns", []).append(
            {
                "t": datetime.now().isoformat(timespec="seconds"),
                "user": user,
                "assistant": assistant,
                "path": reply_path or "",
            }
        )
        _write_session(write_key, payload)


def end_session(
    session_id: str,
    *,
    device_id: str = "",
    user_name: str | None = None,
    reason: str = "goodbye",
) -> dict[str, Any] | None:
    sid = (session_id or "").strip()
    if not sid:
        return None
    with _lock:
        write_key, payload = _find_session(sid, device_id=device_id, user_name=user_name)
        if payload is None:
            payload = _empty_session(sid, device_id=device_id, user_name=user_name)
        payload["ended_at"] = datetime.now().isoformat(timespec="seconds")
        payload["end_reason"] = reason
        if user_name:
            payload["user_name"] = user_name.strip()
            write_key = safe_slug(user_name)
        _write_session(write_key, payload)
        logger.info(
            "Voice session end id=%s user=%s turns=%d reason=%s",
            sid,
            write_key,
            len(payload.get("turns") or []),
            reason,
        )
        return payload


def list_sessions_for_user(user_name: str | None, *, limit: int = 50) -> list[dict[str, Any]]:
    """Newest-first session summaries for a user (folder slug or user_name field)."""
    want = safe_slug(user_name)
    root = sessions_dir()
    if not root.is_dir() or not want:
        return []
    files = sorted(root.glob("*/*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    out: list[dict[str, Any]] = []
    for path in files:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        owner = safe_slug(str(payload.get("user_name") or path.parent.name))
        if owner != want:
            continue
        turns = payload.get("turns") or []
        out.append(
            {
                "session_id": payload.get("session_id") or path.stem,
                "user_name": payload.get("user_name") or user_name,
                "device_id": payload.get("device_id") or "",
                "started_at": payload.get("started_at"),
                "ended_at": payload.get("ended_at"),
                "turns": len(turns),
                "path": path.relative_to(root).as_posix(),
            }
        )
        if len(out) >= max(1, min(limit, 200)):
            break
    return out


def list_recent_sessions(limit: int = 50) -> list[dict[str, Any]]:
    root = sessions_dir()
    if not root.is_dir():
        return []
    files = sorted(root.glob("*/*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    out: list[dict[str, Any]] = []
    for path in files[: max(1, min(limit, 200))]:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        turns = payload.get("turns") or []
        out.append(
            {
                "session_id": payload.get("session_id") or path.stem,
                "user_name": payload.get("user_name") or path.parent.name,
                "device_id": payload.get("device_id") or "",
                "started_at": payload.get("started_at"),
                "ended_at": payload.get("ended_at"),
                "turns": len(turns),
                "path": path.relative_to(root).as_posix(),
            }
        )
    return out

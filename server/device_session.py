"""In-memory voice session context per device — until the user says goodbye."""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass, field

from memory_service import truncate_context_text

logger = logging.getLogger(__name__)

DEFAULT_MAX_TURNS = 20
DEFAULT_TTL_SECONDS = 1800.0


@dataclass
class _DeviceSession:
    turns: list[tuple[str, str]] = field(default_factory=list)
    updated_at: float = field(default_factory=time.time)


_lock = threading.Lock()
_sessions: dict[str, _DeviceSession] = {}


def _max_turns() -> int:
    raw = os.environ.get("DEVICE_SESSION_MAX_TURNS", str(DEFAULT_MAX_TURNS)).strip()
    try:
        return max(1, int(raw))
    except ValueError:
        return DEFAULT_MAX_TURNS


def _ttl_seconds() -> float:
    raw = os.environ.get("DEVICE_SESSION_TTL_SECONDS", str(DEFAULT_TTL_SECONDS)).strip()
    try:
        value = float(raw)
        return max(0.0, value)
    except ValueError:
        return DEFAULT_TTL_SECONDS


def _normalize_device_id(device_id: str | None) -> str:
    """Session key is the MAC when present. Empty ids are not shared."""
    from user_devices import normalize_device_mac

    mac = normalize_device_mac(device_id)
    if mac:
        return mac
    return str(device_id or "").strip()


def get_device_session_turns(device_id: str | None) -> list[tuple[str, str]]:
    """Return a copy of the active session turns for this device."""
    key = _normalize_device_id(device_id)
    if not key:
        return []
    ttl = _ttl_seconds()
    with _lock:
        session = _sessions.get(key)
        if not session:
            return []
        if ttl > 0 and time.time() - session.updated_at > ttl:
            del _sessions[key]
            return []
        return list(session.turns)


def append_device_session_turn(
    device_id: str | None,
    user_text: str,
    assistant_text: str,
) -> None:
    """Append one exchange to the device session."""
    user = str(user_text or "").strip()
    assistant = str(assistant_text or "").strip()
    if not user or not assistant:
        return
    key = _normalize_device_id(device_id)
    if not key:
        return
    with _lock:
        session = _sessions.setdefault(key, _DeviceSession())
        session.turns.append((user, assistant))
        max_turns = _max_turns()
        if len(session.turns) > max_turns:
            session.turns = session.turns[-max_turns:]
        session.updated_at = time.time()
        turn_count = len(session.turns)
    logger.debug(
        "Device session append device=%s turns=%d heard=%s",
        key,
        turn_count,
        user[:80],
    )


def clear_device_session(device_id: str | None) -> None:
    """End the device session (after goodbye)."""
    key = _normalize_device_id(device_id)
    if not key:
        return
    with _lock:
        if key in _sessions:
            del _sessions[key]
            logger.info("Device session cleared device=%s", key)


def format_device_session_prompt(
    turns: list[tuple[str, str]],
    *,
    viewer_name: str | None = None,
) -> str:
    """Format active session turns for the LLM prompt."""
    if not turns:
        return ""

    name = (viewer_name or "").strip()
    parts: list[str] = []
    if name:
        parts.append(
            f"You are speaking directly to {name}. Always use second person (you/we). "
            f"Never refer to {name} in third person."
        )
    else:
        parts.append(
            "This is an ongoing voice session on the home robot. "
            "Continue the current topic from the turns below."
        )

    lines: list[str] = []
    for user_text, assistant_text in turns[-5:]:
        cleaned_user = truncate_context_text(user_text)
        if not cleaned_user:
            continue
        lines.append(f"- User: {cleaned_user}")
        cleaned_reply = truncate_context_text(assistant_text, 140)
        if cleaned_reply:
            lines.append(f"  NiNO: {cleaned_reply}")

    if not lines:
        return ""

    last_user = truncate_context_text(turns[-1][0])
    latest = f"Latest user topic to continue: {last_user}\n" if last_user else ""
    parts.append(
        "Current voice session (oldest first). Continue the LAST user topic, "
        "not older ones:\n"
        + "\n".join(lines)
        + f"\n{latest}"
        "Give a real spoken answer. Do not ask which topic they meant."
    )
    parts.append(
        "The session continues until the user says goodbye. "
        "Short answers like numbers or yes/no refer to the most recent assistant question. "
        "If the user asked you to quiz them or give them numbers, ask one real problem "
        "with two concrete numbers — never say insert, placeholder, or use brackets. "
        "If the user gives a math expression or two numbers during a math drill, compute "
        "and state the result — never ask for numbers they already provided."
    )
    return "\n\n".join(parts)


def merge_prompt_blocks(*blocks: str | None) -> str | None:
    """Join non-empty prompt blocks."""
    cleaned = [block.strip() for block in blocks if block and block.strip()]
    if not cleaned:
        return None
    return "\n\n".join(cleaned)

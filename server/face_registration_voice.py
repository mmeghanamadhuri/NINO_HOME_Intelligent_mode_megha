"""Extract a person's name from spoken registration phrases."""

from __future__ import annotations

import re

# Ordered — first match wins.
_NAME_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\b(?:my|the)\s+name\s+is\s+([a-z][a-z0-9' -]{0,62})\b",
        r"\b(?:i\s*am|i'?m|im)\s+([a-z][a-z0-9' -]{0,62})\b",
        r"\b(?:call|calling)\s+me\s+([a-z][a-z0-9' -]{0,62})\b",
        r"\b(?:this|that)\s+is\s+([a-z][a-z0-9' -]{0,62})\b",
        r"\b(?:it'?s|its)\s+([a-z][a-z0-9' -]{0,62})\b",
        r"\bname\s+is\s+([a-z][a-z0-9' -]{0,62})\b",
        r"^([a-z][a-z0-9' -]{1,63})$",
    )
)

_REJECT_NAMES: frozenset[str] = frozenset(
    {
        "unknown",
        "face",
        "hello",
        "hi",
        "hey",
        "yes",
        "no",
        "yeah",
        "yep",
        "nope",
        "ok",
        "okay",
        "register",
        "registration",
        "name",
        "nino",
        "esp",
        "robot",
        "bot",
        "please",
        "thanks",
        "thank",
        "you",
        "me",
        "my",
        "the",
        "a",
        "an",
    }
)

_TRAILING_NOISE = re.compile(
    r"\s+(?:please|thanks|thank you|here|sir|ma'am|maam)\s*$",
    re.IGNORECASE,
)


def _clean_candidate(raw: str) -> str:
    text = raw.strip().strip(".,!?…")
    text = _TRAILING_NOISE.sub("", text).strip()
    # Title-case each word for display; slugging happens in FaceService._person_id.
    parts = [p for p in re.split(r"\s+", text) if p]
    if not parts:
        return ""
    if len(parts) > 4:
        parts = parts[:4]
    return " ".join(p[:1].upper() + p[1:] if p else "" for p in parts)


def _is_valid_name(candidate: str) -> bool:
    lower = candidate.lower()
    if lower in _REJECT_NAMES:
        return False
    words = lower.split()
    if not words:
        return False
    if any(w in _REJECT_NAMES for w in words):
        return False
    if "register" in lower or "face" in lower:
        return False
    return bool(re.search(r"[a-zA-Z]", candidate))


def extract_registration_name(user_text: str) -> str | None:
    """Return a display name from STT text, or None if no confident name."""
    text = (user_text or "").strip()
    if not text or len(text) > 120:
        return None

    for pattern in _NAME_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        candidate = _clean_candidate(match.group(1))
        if not candidate or not _is_valid_name(candidate):
            continue
        return candidate

    return None

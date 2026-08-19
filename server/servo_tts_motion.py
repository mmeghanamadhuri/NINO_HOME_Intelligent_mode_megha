"""Build a bounded Dynamixel action list from assistant TTS text."""

from __future__ import annotations

import re

# Names must match firmware nino_servo_recplay_play_motion_json().
_QUESTION_RE = re.compile(r"\?")
_NEG_RE = re.compile(
    r"\b(?:no|nope|don't|do not|not|never|can't|cannot|won't)\b", re.I
)
_YES_RE = re.compile(r"\b(?:yes|yeah|yep|sure|okay|ok|absolutely|of course)\b", re.I)
_GREET_RE = re.compile(
    r"\b(?:hey|hello|hi|good morning|good afternoon|good evening|welcome)\b", re.I
)
_BYE_RE = re.compile(r"\b(?:goodbye|good bye|bye|see you|take care)\b", re.I)


def motion_actions_for_reply(
    reply_text: str,
    *,
    reply_path: str = "",
) -> list[str]:
    """Return named servo actions within firmware pan/tilt limits."""
    path = (reply_path or "").strip().lower()
    text = str(reply_text or "").strip()
    if path in {"goodbye"} or _BYE_RE.search(text):
        return ["nod"]
    if path in {"session_greet", "greeting"} or _GREET_RE.search(text):
        return ["greet"]
    if path in {
        "session_register_offer",
        "session_ask_name",
        "session_spell",
        "session_confirm",
        "face_registration",
    }:
        return []
    if _QUESTION_RE.search(text):
        return ["look_left", "look_right", "nod"]
    if _NEG_RE.search(text) and not _YES_RE.search(text):
        return ["shake"]
    if _YES_RE.search(text):
        return ["nod"]
    if path in {"joke", "joke_and_time", "football_joke"}:
        return ["nod", "look_left", "look_right"]
    return ["talk"]

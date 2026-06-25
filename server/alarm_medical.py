"""Medical (P0) alarm classification and voice acknowledgment parsing."""

from __future__ import annotations

import os
import re
from datetime import datetime

# P0 = medical / medication; P1 = everything else
PRIORITY_MEDICAL = 0
PRIORITY_NORMAL = 1

CATEGORY_MEDICAL = "medical"
CATEGORY_GENERAL = "general"

ACK_NONE = "none"
ACK_AWAITING = "awaiting"
ACK_RESCHEDULE_PROMPT = "reschedule_prompt"
ACK_CONFIRMED = "confirmed"

_MEDICAL_KEYWORDS: tuple[str, ...] = (
    "medicine",
    "medicines",
    "medication",
    "medications",
    "meds",
    "pill",
    "pills",
    "tablet",
    "tablets",
    "capsule",
    "dose",
    "dosage",
    "insulin",
    "inhaler",
    "prescription",
    "antibiotic",
    "vitamin",
    "supplement",
    "eye drop",
    "eyedrop",
    "injection",
    "shot",
    "blood pressure",
    "blood sugar",
    "glucose",
)

_POSITIVE_ACK = re.compile(
    r"\b("
    r"yes|yeah|yep|yup|sure|ok|okay|okey|"
    r"i did|i have|i've|ive|already|done|taken|took|"
    r"got it|all set|completed|finished|affirmative"
    r")\b",
    re.IGNORECASE,
)

_NEGATIVE_ACK = re.compile(
    r"\b("
    r"no|nope|nah|not yet|didn't|didnt|haven't|havent|"
    r"forget|forgot|missed|unable|can't|cant"
    r")\b",
    re.IGNORECASE,
)

_RESCHEDULE_WORD = re.compile(
    r"\b(reschedule|re-schedule|later|delay|postpone|move it|push it)\b",
    re.IGNORECASE,
)

_CANCEL_WORD = re.compile(
    r"\b(cancel|delete|remove|stop|never mind|nevermind)\b",
    re.IGNORECASE,
)

_MEDICAL_SET_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\b(?:medication|medicine|meds?)\s+reminder\s+(?:at|for)\s+(.+)",
        r"\bremind\s+me\s+to\s+(?:take|use)\s+(?:my\s+)?(?:meds?|medicines?|medication|pills?)(?:\s+at\s+(.+))?",
        r"\b(?:set|create|make)\s+(?:an?\s+)?(?:alarm|reminder)\s+.+?\b(?:take|use)\s+(?:my\s+)?(?:meds?|medicines?|medication|pills?)\b",
        # Whisper often drops "re-" or hears "find" instead of "remind".
        r"\b(?:find|mind)\s+me\s+to\s+(?:take|use)\s+(?:my\s+)?(?:meds?|medicines?|medication|pills?)",
        r"\b(?:take|use)\s+(?:my\s+)?(?:meds?|medicines?|medication|pills?)\s+at\s+(.+)",
        r"^(?:me\s+)?to\s+(?:take|use)\s+(?:my\s+)?(?:meds?|medicines?|medication|pills?)\b",
    )
)

_MEDICINE_REMINDER_HINT_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\b(?:find|mind)\s+me\s+to\s+.*\b(?:meds?|medicines?|medication|pills?)\b",
        r"\bme\s+to\s+take\s+(?:my\s+)?(?:meds?|medicines?|medication|pills?)\b",
        r"^to\s+take\s+(?:my\s+)?(?:meds?|medicines?|medication|pills?)\b",
        r"\b(?:take|use)\s+(?:my\s+)?(?:meds?|medicines?|medication|pills?)\s+at\b",
    )
)


def medical_repeat_minutes() -> int:
    return max(1, int(os.environ.get("ALARM_MEDICAL_REPEAT_MINUTES", "3")))


def is_medical_set_command(user_text: str) -> bool:
    text = user_text.strip()
    if not text:
        return False
    return any(p.search(text) for p in _MEDICAL_SET_PATTERNS)


def looks_like_medicine_reminder_set(user_text: str) -> bool:
    """Catch garbled Whisper transcripts for medication reminders."""
    text = user_text.strip()
    if not text:
        return False
    if is_medical_set_command(text):
        return True
    return any(p.search(text) for p in _MEDICINE_REMINDER_HINT_PATTERNS)


def classify_alarm_text(*parts: str) -> tuple[int, str, bool]:
    """Return (priority, category, requires_ack)."""
    blob = " ".join(p.strip() for p in parts if p and p.strip()).lower()
    if not blob:
        return PRIORITY_NORMAL, CATEGORY_GENERAL, False
    if any(kw in blob for kw in _MEDICAL_KEYWORDS):
        return PRIORITY_MEDICAL, CATEGORY_MEDICAL, True
    return PRIORITY_NORMAL, CATEGORY_GENERAL, False


def is_positive_ack(user_text: str) -> bool:
    text = user_text.strip()
    if not text:
        return False
    if _NEGATIVE_ACK.search(text):
        return False
    return bool(_POSITIVE_ACK.search(text))


def is_negative_ack(user_text: str) -> bool:
    text = user_text.strip()
    if not text:
        return False
    return bool(_NEGATIVE_ACK.search(text))


def wants_reschedule(user_text: str) -> bool:
    return bool(_RESCHEDULE_WORD.search(user_text.strip()))


def wants_cancel(user_text: str) -> bool:
    return bool(_CANCEL_WORD.search(user_text.strip()))


def ack_prompt_suffix() -> str:
    return " Say yes or no."


def repeat_prompt_suffix() -> str:
    return " Yes or no?"

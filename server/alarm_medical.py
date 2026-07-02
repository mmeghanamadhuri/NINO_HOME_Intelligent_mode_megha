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

# Shared medication nouns for set-command and label extraction.
MED_OBJECT_RE = (
    r"(?:meds?|medicines?|medication|medications|pills?|tablets?|capsules?|"
    r"insulin|inhalers?|vitamins?|supplements?|antibiotics?|prescriptions?|"
    r"eye\s*drops?|eyedrops?|doses?|dosages?|injections?|shots?|"
    r"blood\s+pressure\s+medicine|blood\s+sugar\s+medicine|glucose\s+medicine)"
)
MY_MED_RE = rf"(?:my\s+)?{MED_OBJECT_RE}"
TAKE_MED_RE = rf"(?:take|use|have)\s+{MY_MED_RE}"
TIME_AFTER_RE = r"(?:at|for|by)\s+(.+)"

_MEDICAL_SET_PATTERN_STRINGS: tuple[str, ...] = (
    rf"\b(?:medication|medicine|meds?)\s+reminder\s+(?:at|for)\s+(.+)",
    rf"\bremind\s+me\s+to\s+{TAKE_MED_RE}(?:\s+{TIME_AFTER_RE})?",
    rf"\bremind\s+me\s+about\s+{MY_MED_RE}\s+{TIME_AFTER_RE}",
    rf"\b(?:set|create|make)\s+(?:an?\s+)?(?:alarm|reminder)\s+.+?\b(?:take|use)\s+{MY_MED_RE}\b",
    rf"\b(?:set|schedule)\s+(?:my\s+)?(?:medicine|medication|meds?|pills?)\s+{TIME_AFTER_RE}",
    # Whisper often drops "re-" or hears "find" instead of "remind".
    rf"\b(?:find|mind)\s+me\s+to\s+{TAKE_MED_RE}",
    rf"\b{TAKE_MED_RE}\s+{TIME_AFTER_RE}",
    rf"^(?:me\s+)?to\s+{TAKE_MED_RE}\b",
    rf"\bi\s+need\s+to\s+{TAKE_MED_RE}\s+{TIME_AFTER_RE}",
    rf"\bi\s+have\s+to\s+{TAKE_MED_RE}\s+{TIME_AFTER_RE}",
    rf"\b(?:don't|do not)\s+(?:let\s+me\s+)?forget\s+(?:to\s+)?(?:(?:take|use)\s+)?{MY_MED_RE}\s+{TIME_AFTER_RE}",
    rf"\b(?:don't|do not)\s+forget\s+{MY_MED_RE}\s+{TIME_AFTER_RE}",
    rf"\bremember\s+(?:to\s+)?(?:(?:take|use)\s+)?{MY_MED_RE}\s+{TIME_AFTER_RE}",
    rf"\bwake\s+me\s+(?:up\s+)?to\s+{TAKE_MED_RE}\s+{TIME_AFTER_RE}",
    rf"\b(?:pill|medicine|medication|meds?)\s+(?:alarm|reminder)\s+(?:at|for)\s+(.+)",
)

_MEDICAL_SET_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE) for p in _MEDICAL_SET_PATTERN_STRINGS
)

_MEDICINE_REMINDER_HINT_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        rf"\b(?:find|mind)\s+me\s+to\s+.*\b{MED_OBJECT_RE}\b",
        rf"\bme\s+to\s+take\s+{MY_MED_RE}\b",
        rf"^to\s+take\s+{MY_MED_RE}\b",
        rf"\b{TAKE_MED_RE}\s+(?:at|for|by)\b",
        rf"\b(?:don't|do not)\s+forget\s+.*\b{MED_OBJECT_RE}\b",
        rf"\bremember\s+.*\b{MED_OBJECT_RE}\b",
        rf"\bi\s+need\s+to\s+take\s+.*\b{MED_OBJECT_RE}\b",
        rf"\bi\s+have\s+to\s+take\s+.*\b{MED_OBJECT_RE}\b",
        rf"\bremind\s+me\s+about\s+.*\b{MED_OBJECT_RE}\b",
        rf"\b(?:pill|medicine|medication|meds?)\s+(?:alarm|reminder)\b",
        rf"\bwake\s+me\s+.*\b{MED_OBJECT_RE}\b",
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


def normalize_label_for_user(label: str) -> str:
    """Rewrite a user-spoken task so the bot addresses them (my → your, me → you)."""
    text = label.strip()
    if not text:
        return text
    replacements: tuple[tuple[re.Pattern[str], str], ...] = (
        (re.compile(r"\bmyself\b", re.IGNORECASE), "yourself"),
        (re.compile(r"\bmine\b", re.IGNORECASE), "yours"),
        (re.compile(r"\bmy\b", re.IGNORECASE), "your"),
        (re.compile(r"\bme\b", re.IGNORECASE), "you"),
        (re.compile(r"\bI'm\b", re.IGNORECASE), "you're"),
        (re.compile(r"\bI've\b", re.IGNORECASE), "you've"),
        (re.compile(r"\bI'll\b", re.IGNORECASE), "you'll"),
        (re.compile(r"\bI\b"), "you"),
    )
    for pattern, replacement in replacements:
        text = pattern.sub(replacement, text)
    return re.sub(r"\s+", " ", text).strip()


def ack_prompt_suffix() -> str:
    return " Say yes or no."


def medical_ack_prompt_suffix() -> str:
    return " Please confirm if you have taken it or not."


def repeat_prompt_suffix() -> str:
    return " Yes or no?"


_MED_WITHOUT_YOUR = re.compile(
    rf"^(take|use|have)\s+(?!your\b)({MED_OBJECT_RE})\b",
    re.IGNORECASE,
)

_LEADING_TASK_VERB = re.compile(
    r"^(?:to\s+)?(?:take|use|have|do|complete|finish)\s+(?:your\s+)?",
    re.IGNORECASE,
)


def medical_label_object(label: str) -> str:
    """Extract the task object for acknowledgment sentences.

    'take medicines' -> 'medicines', 'take my medicines' -> 'medicines'.
    Used so replies read 'your medicines' instead of 'your take medicines'.
    """
    text = normalize_label_for_user((label or "").strip())
    text = _LEADING_TASK_VERB.sub("", text).strip()
    return text or "medication"


def format_medical_fire_phrase(label: str) -> str:
    """Turn a stored task label into natural speech, e.g. 'take medicines' → 'it's time to take your medicines'."""
    text = normalize_label_for_user((label or "").strip())
    text = re.sub(r"^to\s+", "", text, flags=re.IGNORECASE)
    if not text:
        text = "take your medication"
    text = _MED_WITHOUT_YOUR.sub(r"\1 your \2", text)
    if re.match(r"^(take|use|have)\b", text, re.IGNORECASE):
        return f"it's time to {text}"
    return f"it's time for {text}"


def format_medical_fire_message(
    *,
    label: str = "",
    person_name: str = "",
    repeat: bool = False,
) -> str:
    """Spoken medical alert addressing the user (not echoing their original command)."""
    name = (person_name or "").strip()
    phrase = format_medical_fire_phrase(label)
    suffix = medical_ack_prompt_suffix()
    if repeat:
        if name:
            return f"{name}, {phrase}.{suffix}"
        return f"{phrase[0].upper()}{phrase[1:]}.{suffix}"
    if name:
        return f"{name}, {phrase}.{suffix}"
    return f"{phrase[0].upper()}{phrase[1:]}.{suffix}"

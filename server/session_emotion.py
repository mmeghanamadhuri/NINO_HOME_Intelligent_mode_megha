"""Speak and track camera emotion for greetings, look-scan, and mid-session care."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

SPEAKABLE_EMOTIONS = frozenset(
    {"happy", "sad", "angry", "fear", "surprise", "disgust"}
)
_SKIP_EMOTIONS = frozenset({"", "uncertain", "unknown", "none"})

_LOOKING = {
    "happy": "happy",
    "sad": "a bit down",
    "angry": "upset",
    "fear": "worried",
    "surprise": "surprised",
    "disgust": "uncomfortable",
}

_CHANGE_QUESTIONS = {
    ("happy", "sad"): (
        "You looked happy earlier — want to talk about what's on your mind?"
    ),
    ("happy", "angry"): "You look upset. Did something just happen?",
    ("happy", "fear"): "You look worried. Everything okay?",
    ("happy", "disgust"): "You look uncomfortable. Want to tell me about it?",
    ("sad", "happy"): "You look brighter than a bit ago. Feeling better?",
    ("angry", "happy"): "You look calmer now. Did things work out?",
    ("fear", "happy"): "You look more at ease. Feeling better?",
    ("disgust", "happy"): "You look more comfortable now. Feeling better?",
    ("sad", "angry"): "You look frustrated. Want to tell me about it?",
    ("angry", "sad"): "You look a bit down now. I'm here if you want to talk.",
    ("neutral", "sad"): "You look a bit down. Everything okay?",
    ("neutral", "angry"): "You look upset. What happened?",
    ("neutral", "fear"): "You look worried. Want to talk it through?",
    ("neutral", "disgust"): "You look uncomfortable. Everything okay?",
    ("happy", "surprise"): "You look surprised. What caught your eye?",
    ("neutral", "surprise"): "You look surprised. What's going on?",
    ("sad", "surprise"): "You look surprised. Something unexpected?",
}

_ASK_COOLDOWN_S = 45.0
_CHANGE_HITS = 2


def normalize_emotion(label: str | None) -> str | None:
    key = str(label or "").strip().lower()
    if not key or key in _SKIP_EMOTIONS:
        return None
    if key == "neutral":
        return "neutral"
    if key in SPEAKABLE_EMOTIONS:
        return key
    return None


def looking_phrase(emotion: str | None) -> str | None:
    key = normalize_emotion(emotion)
    if key is None:
        return None
    return _LOOKING.get(key)


def phrase_person_looking(name: str, emotion: str | None) -> str:
    """'Hari' or 'Hari, who looks happy'."""
    cleaned = str(name or "").strip()
    if not cleaned:
        return ""
    look = looking_phrase(emotion)
    if not look:
        return cleaned
    return f"{cleaned}, who looks {look}"


def greet_with_emotion(name: str, emotion: str | None = None) -> str:
    cleaned = str(name or "").strip()
    if not cleaned:
        return "Hey."
    look = looking_phrase(emotion)
    if not look:
        return f"Hey {cleaned}."
    return f"Hey {cleaned}, you look {look}."


def question_for_emotion_change(previous: str | None, current: str | None) -> str | None:
    prev = normalize_emotion(previous)
    cur = normalize_emotion(current)
    if prev is None or cur is None or prev == cur:
        return None
    specific = _CHANGE_QUESTIONS.get((prev, cur))
    if specific:
        return specific
    look = looking_phrase(cur)
    if not look or cur == "neutral" or prev not in SPEAKABLE_EMOTIONS:
        return None
    return f"You look {look} now. Want to tell me about it?"


@dataclass
class _PersonMood:
    last: str | None = None
    pending: str | None = None
    pending_hits: int = 0
    asked_pair: tuple[str, str] | None = None
    last_ask_at: float = 0.0


@dataclass
class _DeviceMoods:
    people: dict[str, _PersonMood] = field(default_factory=dict)


_lock = threading.Lock()
_devices: dict[str, _DeviceMoods] = {}


def _device_key(device_id: str | None) -> str:
    return str(device_id or "").strip()


def _person_key(name: str) -> str:
    return str(name or "").strip().lower()


def reset_session_emotions(device_id: str | None = None) -> None:
    key = _device_key(device_id)
    with _lock:
        _devices.pop(key, None)


def observe_viewer_emotion(
    device_id: str | None,
    name: str | None,
    emotion: str | None,
) -> str | None:
    """Record this person's emotion. Return a one-time question after a stable change."""
    person_key = _person_key(name)
    mood_key = normalize_emotion(emotion)
    if not person_key or mood_key is None:
        return None

    now = time.time()
    device_key = _device_key(device_id)
    with _lock:
        bucket = _devices.setdefault(device_key, _DeviceMoods())
        person = bucket.people.setdefault(person_key, _PersonMood())
        if person.last is None:
            person.last = mood_key
            person.pending = None
            person.pending_hits = 0
            return None
        if mood_key == person.last:
            person.pending = None
            person.pending_hits = 0
            return None
        if mood_key == person.pending:
            person.pending_hits += 1
        else:
            person.pending = mood_key
            person.pending_hits = 1
        if person.pending_hits < _CHANGE_HITS:
            return None
        previous = person.last
        person.last = mood_key
        person.pending = None
        person.pending_hits = 0
        question = question_for_emotion_change(previous, mood_key)
        if not question:
            return None
        pair = (previous or "", mood_key)
        if person.asked_pair == pair:
            return None
        if person.last_ask_at and (now - person.last_ask_at) < _ASK_COOLDOWN_S:
            return None
        person.asked_pair = pair
        person.last_ask_at = now
        return question

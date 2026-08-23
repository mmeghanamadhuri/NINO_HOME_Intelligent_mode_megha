"""Load external CSV voice question banks for soak testing."""

from __future__ import annotations

import csv
import os
import random
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

_DEFAULT_CSV = (
    Path(__file__).resolve().parent.parent / "data" / "voice_assistant_test_questions.csv"
)
_FALLBACK_CSV = Path.home() / "Downloads" / "voice_assistant_test_questions.csv"

_STOP_WORDS = frozenset(
    {
        "a",
        "an",
        "the",
        "is",
        "are",
        "was",
        "were",
        "what",
        "who",
        "how",
        "when",
        "where",
        "why",
        "which",
        "do",
        "does",
        "did",
        "can",
        "could",
        "would",
        "should",
        "you",
        "your",
        "me",
        "my",
        "i",
        "it",
        "this",
        "that",
        "there",
        "tell",
        "say",
        "give",
        "please",
        "about",
        "many",
        "much",
        "have",
        "has",
        "had",
        "be",
        "been",
        "being",
        "on",
        "in",
        "at",
        "to",
        "for",
        "of",
        "and",
        "or",
        "if",
        "then",
        "with",
        "from",
        "as",
        "by",
        "one",
        "two",
        "three",
        "word",
        "reply",
        "answer",
    }
)

_CATEGORY_HINTS: dict[str, tuple[str, ...]] = {
    "time and date": (
        "time",
        "day",
        "today",
        "hour",
        "minute",
        "monday",
        "tuesday",
        "wednesday",
        "thursday",
        "friday",
        "saturday",
        "sunday",
        "am",
        "pm",
        "date",
        "year",
        "month",
    ),
    "alarm": ("alarm", "remind", "timer", "set", "ok", "minute", "hour", "cancel"),
    "timer": ("alarm", "remind", "timer", "set", "ok", "minute", "hour", "cancel"),
    "memory": ("remember", "recall", "earlier", "before", "said", "asked", "yes", "no"),
    "emotional": ("here", "help", "listen", "sorry", "understand", "feel", "talk", "yes"),
    "joke": ("joke", "funny", "why", "laugh", "haha", "ha"),
    "science": ("because", "is", "are", "energy", "water", "earth", "plant", "light"),
    "robot": ("camera", "voice", "speaker", "help", "alarm", "memory", "see", "hear"),
    "honest": ("cannot", "can't", "don't", "do not", "sorry", "unable", "not able"),
    "small talk": ("good", "well", "fine", "hello", "hi", "help", "great", "thanks"),
}


@dataclass(frozen=True)
class CsvVoiceQuestion:
    question_id: str
    category: str
    question: str
    good_answer_hint: str
    expected: tuple[str, ...]


def soak_voice_csv_path() -> Path | None:
    raw = os.environ.get("SOAK_VOICE_QUESTIONS_CSV", "").strip()
    if raw:
        path = Path(raw).expanduser()
        return path if path.is_file() else None
    if _DEFAULT_CSV.is_file():
        return _DEFAULT_CSV
    if _FALLBACK_CSV.is_file():
        return _FALLBACK_CSV
    return None


def csv_questions_enabled() -> bool:
    raw = os.environ.get("SOAK_VOICE_QUESTIONS_CSV", "auto").strip().lower()
    if raw in {"0", "false", "no", "off", "disable", "disabled"}:
        return False
    return soak_voice_csv_path() is not None


def _words_from_text(text: str, *, limit: int = 4) -> tuple[str, ...]:
    tokens: list[str] = []
    for word in re.findall(r"[a-z0-9']+", text.lower()):
        if len(word) < 3 or word in _STOP_WORDS:
            continue
        if word not in tokens:
            tokens.append(word)
        if len(tokens) >= limit:
            break
    return tuple(tokens)


def _math_keywords(question: str) -> tuple[str, ...]:
    q = question.lower()
    nums = [int(n) for n in re.findall(r"\b(\d+)\b", q)]
    keywords: list[str] = []

    if "percent" in q or "%" in q:
        if len(nums) >= 2:
            keywords.append(str(round(nums[0] * nums[1] / 100)))
    elif "factorial" in q and nums:
        n = nums[0]
        value = 1
        for i in range(2, n + 1):
            value *= i
        keywords.append(str(value))
    elif "squared" in q and nums:
        keywords.append(str(nums[0] ** 2))
    elif "square root" in q and nums:
        import math

        root = int(math.isqrt(nums[0]))
        if root * root == nums[0]:
            keywords.append(str(root))
    elif "power" in q and len(nums) >= 2:
        keywords.append(str(nums[0] ** nums[1]))
    elif "half of" in q and nums:
        keywords.append(str(nums[0] // 2))
    elif "third of" in q and nums:
        keywords.append(str(nums[0] // 3))
    elif "divided by" in q and len(nums) >= 2 and nums[1]:
        if "decimal" in q:
            keywords.append(str(round(nums[0] / nums[1], 1)))
        else:
            keywords.append(str(nums[0] // nums[1]))
    elif "times" in q or "multiply" in q:
        if len(nums) >= 2:
            keywords.append(str(nums[0] * nums[1]))
    elif "plus" in q or "sum of" in q or "add" in q:
        if len(nums) >= 2:
            keywords.append(str(sum(nums[:2])))
    elif "minus" in q or "subtract" in q:
        if len(nums) >= 2:
            keywords.append(str(nums[0] - nums[1]))
    elif "round" in q and nums:
        keywords.append(str(round(float(nums[0]))))
    elif "prime" in q:
        keywords.extend(("yes", "no", "prime"))
    elif len(nums) == 1 and "minutes" in q and "hour" in q:
        keywords.append(str(nums[0] * 60))

    for n in nums[:3]:
        keywords.append(str(n))
    return tuple(dict.fromkeys(keywords))


def infer_expected_keywords(
    question: str,
    category: str,
    good_answer_hint: str = "",
) -> tuple[str, ...]:
    cat = str(category or "").lower()
    q = str(question or "").lower()

    if "math" in cat or "calculation" in cat:
        math_keys = _math_keywords(question)
        if math_keys:
            return math_keys

    for key, hints in _CATEGORY_HINTS.items():
        if key in cat or key in q:
            return hints

    if "prime number" in q:
        return ("yes", "no", "prime")
    if "capital of" in q:
        return _words_from_text(q.split("capital of", 1)[-1], limit=2)
    if "joke" in q:
        return _CATEGORY_HINTS["joke"]
    if "alarm" in q or "remind" in q or "timer" in q:
        return _CATEGORY_HINTS["alarm"]
    if "time" in q or "day is it" in q or "date" in q:
        return _CATEGORY_HINTS["time and date"]
    if q.startswith(("can you", "do you", "are you", "will you")):
        return ("yes", "no", "can", "cannot", "can't", "help", "sorry")

    hint_words = _words_from_text(good_answer_hint, limit=3)
    if hint_words:
        return hint_words
    return ()


@lru_cache(maxsize=4)
def load_csv_voice_questions(path: str) -> tuple[CsvVoiceQuestion, ...]:
    rows: list[CsvVoiceQuestion] = []
    with open(path, newline="", encoding="utf-8") as handle:
        for raw in csv.DictReader(handle):
            question = str(raw.get("question") or "").strip()
            if not question:
                continue
            category = str(raw.get("category") or "").strip()
            hint = str(raw.get("what_a_good_answer_looks_like") or "").strip()
            rows.append(
                CsvVoiceQuestion(
                    question_id=str(raw.get("id") or len(rows) + 1),
                    category=category,
                    question=question,
                    good_answer_hint=hint,
                    expected=infer_expected_keywords(question, category, hint),
                )
            )
    return tuple(rows)


def pick_csv_voice_questions(
    *,
    cycle_number: int,
    count: int,
    exclude: set[str] | None = None,
) -> list[tuple[str, tuple[str, ...]]]:
    path = soak_voice_csv_path()
    if path is None or count <= 0:
        return []

    bank = load_csv_voice_questions(str(path))
    if not bank:
        return []

    exclude = exclude or set()
    available = [row for row in bank if row.question not in exclude]
    if not available:
        return []

    rng = random.Random()
    rng.seed(f"soak-csv-{cycle_number}-{path.stat().st_mtime}")

    # Rotate categories each cycle so soak hits factual, math, alarms, memory, etc.
    by_category: dict[str, list[CsvVoiceQuestion]] = {}
    for row in available:
        by_category.setdefault(row.category, []).append(row)

    categories = sorted(by_category)
    if not categories:
        return []

    start = cycle_number % len(categories)
    ordered_categories = categories[start:] + categories[:start]

    picked: list[tuple[str, tuple[str, ...]]] = []
    seen: set[str] = set(exclude)

    # One question per category first — maximizes diversity for agent learning.
    for category in ordered_categories:
        if len(picked) >= count:
            break
        pool = [row for row in by_category[category] if row.question not in seen]
        if not pool:
            continue
        row = rng.choice(pool)
        picked.append((row.question, row.expected))
        seen.add(row.question)

    if len(picked) < count:
        remaining = [row for row in available if row.question not in seen]
        extra = rng.sample(remaining, k=min(count - len(picked), len(remaining)))
        picked.extend((row.question, row.expected) for row in extra)

    return picked[:count]

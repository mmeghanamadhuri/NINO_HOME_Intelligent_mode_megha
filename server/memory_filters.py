"""Gates and validators for conversation logging and long-term memory extraction."""

from __future__ import annotations

import hashlib
import re

# ---------------------------------------------------------------------------
# Ephemeral / non-storable queries (jokes, trivia, commands)
# ---------------------------------------------------------------------------

# Optional trailing punctuation from Whisper / casual speech.
_TRAILING_PUNCT = r"[.!?…]*"

_EPHEMERAL_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\b(?:tell|give|say)(?:\s+me)?\s+(?:a |an )?joke\b",
        r"\btell me (?:a |an )?joke\b",
        r"\bsend me (?:a |an )?joke\b",
        rf"\b(?:a |an )?joke\??{_TRAILING_PUNCT}\s*$",
        rf"\bjoke\??{_TRAILING_PUNCT}\s*$",
        r"\bmake me laugh\b",
        r"\bsomething funny\b",
        r"\bfunny (?:story|joke)\b",
        r"\bcheer me up\b",
        r"\bit(?:'s| is) (?:a |only a )?joke\b",
        r"\bjust (?:a |kidding|joking)\b",
        r"\bi(?:'m| am) (?:just )?joking\b",
    )
)

_TRIVIA_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\bhow does (?:a |the )?(?:cpu|gpu|cnn|ann|ram|rom)\b",
        r"\bhow (?:do|does) (?:a |the )?(?:cpu|gpu)\b",
        r"\bwhat is (?:a |the )?(?:cpu|gpu|cnn|ann|ai|ml)\b",
        r"\bdifference between\b",
        r"\bexplain\b.{0,32}\b(?:cpu|gpu|neural|machine learning|artificial intelligence)\b",
        r"\btell me how\b.{0,24}\b(?:cpu|gpu|works)\b",
        r"\b(?:please\s+)?(?:define|explain|describe)\b.{0,40}\b(?:micro\s?country|microprocessor|micro\s?controller|gpu|cpu)\b",
        r"\b(?:detail\s+)?explain\b.{0,24}\b(?:gpu|cpu|micro)\b",
        r"\bthis is a microprocessor\b",
        r"\bhow to (?:play|use|make)\b",
        r"\bhow does (?:a |the )?(?:mic(?:rophone)?|speaker|led|lcd|display)\b",
        r"\bhow (?:do|does)\b.{0,24}\b(?:mic(?:rophone)?|speaker|led|lcd|display)\b",
        r"\bhow (?:do|does)\b.{0,16}\b(?:work|works)\b",
        r"\btell me how\b",
        r"\bdifference between\b.{0,40}\b(?:led|lcd|display)\b",
        # General knowledge / acronym questions (Whisper often drops "what is the").
        r"\b(?:full\s+form|meaning|definition|acronym|abbreviation|stands?\s+for|short\s+for|expand)\b",
        r"^(?:full\s+form|meaning|definition|acronym|abbreviation)\s+of\b",
        r"\bwhat(?:'s| is)\s+(?:the\s+)?(?:full\s+form|meaning|acronym|abbreviation)\b",
        r"\bspell(?:ing)?\s+(?:of\s+)?\w+",
    )
)

_QUESTION_LEAD = re.compile(
    r"^(?:what|how|why|when|where|who|which|do|does|did|can|could|would|should|is|are|am|was|were|"
    r"please|tell me|define|explain|describe)\b",
    re.IGNORECASE,
)

_MEMORY_RECALL_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\bwhat do i prefer\b",
        r"\bwhat(?:'s| is) my (?:birthday|birthdate|favorite|favourite|hobby|job)\b",
        r"\bwhat(?:'s| is) my (?:favourite|favorite)\s+\w+",
        r"\bwhat(?:'s| is) my (?:favourite|favorite)\s+food\b",
        r"\b(?:this|it|that)(?:'s| is) my (?:favourite|favorite)\s+\w+",
        r"\bwhat do i (?:like|love|enjoy)\b",
        r"\bwhat (?:don'?t|do not) i (?:like|want|enjoy)\b",
        r"\bwhat do i (?:dislike|hate)\b",
        r"\btell me (?:about )?my (?:favourite|favorite)\b",
        r"\bdo you know what my (?:favourite|favorite)\b",
        r"\bdo i prefer\b",
        r"\bwhich do i (?:like|prefer)\b",
        r"\bwhat (?:are|is) my (?:hobbies|preferences|likes|dislikes)\b",
        r"\bwhat do i prefer(?:\s+to)?\s+(?:drink|eat|have)\b",
        r"\b(?:please|what).{0,16}my (?:favourite|favorite)\s+[\w\s]+\b",
        r"\bmy (?:favourite|favorite)\s+(?:soft\s+)?drinks?\b",
        r"\bwhen(?:'s| is) my (?:birthday|birthdate)\b",
        r"\b(?:miss|mes|miz|what'?s)\s+my\s+birthday\b",
        r"\bdo you know my (?:birthday|birthdate)\b",
    )
)

# STT often hears "what is" as "this is" / "it is" — never treat as a preference update.
_RECALL_PRONOUNS = frozenset({"this", "it", "that", "what", "which", "there"})

RECALL_ALL_PREFERENCES = "__all_preferences__"
RECALL_DISLIKES = "__dislikes__"

_GREETING_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"^happy (?:birthday|celebrate)\b",
        r"^it'?s my birthday[.!?…]*\s*$",
        r"^good (?:morning|afternoon|evening|night)\b",
    )
)

_TTS_ECHO_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"^the birth\s*date is\b",
        r"^your birth\s*date is\b",
        r"^your birthday is\b",
        r"^born on\b",
        r"^the birthday is\b",
        r"^you (?:were|was) born\b",
        r"^got it[!.]",
    )
)

_EXPLICIT_FACT_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\bi (?:really |actually )?(?:like|love|prefer|hate|enjoy)\b",
        r"\bi (?:work|study|live) (?:as|at|in)\b",
        r"\bi was born\b",
        r"\bi am (?:a |an )?\w+",
        r"\bi'm (?:a |an )?\w+",
        r"\bmy (?:name|birthday|birthdate|job|favorite|favourite|hobby|hobbies|allergy|allergies|food)\b",
        r"\bmy (?:favorite|favourite)\s+food\b",
        r"\b(?:favorite|favourite)\s+\w+\s+is\b",
        r"\b(?:not|instead of)\b.{0,24}\b(?:favorite|favourite|prefer)\b",
        r"\bmy name is\b",
        r"\bmet my (?:friend|brother|sister|colleague)\b",
        r"\bi learned to\b",
    )
)

_MEMORY_STOPWORDS = frozenset(
    {
        "that",
        "this",
        "with",
        "from",
        "have",
        "your",
        "their",
        "more",
        "than",
        "very",
        "like",
        "love",
        "prefer",
        "user",
        "they",
        "them",
    }
)

_ALARM_FOLLOWUP_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\b(?:you|why).{0,40}\b(?:medicine|medication|meds|pill|alarm|reminder)\b",
        r"\b(?:medicine|medication|meds|pill)\b.{0,40}\b(?:night|morning|today|tonight)\b",
        r"\b(?:alarm|reminder)(?!\s+(?:at|for)\s).{0,40}\b(?:night|morning|today|tonight)\b",
        r"\bwhy (?:are you |did you )(?:tell|remind|set)\b",
        r"\bwhat alarm\b",
        r"\bwhich alarm\b",
        r"\bmy (?:medicine|medication|meds) (?:alarm|reminder)\b",
        r"\btelling me to take\b",
    )
)

_MEMORY_KEY_ALIASES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bbirth\s*date\b|\bborn on\b|\bbirthday\b", re.I), "birthdate"),
    (re.compile(r"\bfavorite food\b|\bfavourite food\b|\bfavorite meal\b", re.I), "favorite_food"),
    (re.compile(r"\bfavorite beverage\b|\bfavorite drink\b|\bfavourite drink\b|\bprefer.*\b(?:tea|coffee|beer)\b|\bdrinks?\b", re.I), "favorite_drink"),
    (re.compile(r"\bfavorite sport\b|\bfavourite sport\b", re.I), "favorite_sport"),
    (re.compile(r"\bfavorite game\b|\bplays?\s+chess\b", re.I), "favorite_game"),
    (re.compile(r"\bhobb(?:y|ies)\b", re.I), "hobbies"),
    (re.compile(r"\bjob\b|\bwork(?:s|ing)? as\b|\bengineer\b|\bposition\b", re.I), "job_title"),
    (re.compile(r"\ballerg", re.I), "allergies"),
    (re.compile(r"\blocation\b|\blives in\b|\bfrom\b", re.I), "location"),
    (re.compile(r"\beducation\b|\bdegree\b|\bb-?tech\b", re.I), "education"),
    (re.compile(r"\bproject\b|\bworking on\b", re.I), "current_projects"),
)

_JUNK_MEMORY_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"^i$",
        r"^you$",
        r"^don't believe\b",
        r"\bjust a joke\b",
        r"\bisn't actually born\b",
        r"\bnot born on june\b",
        r"^currently working on projects\b",
        r"^current project\b",
        r"\bintelligent assistant\b",
        r"\bnothing in front of the camera\b",
        r"\bno one is currently\b",
        r"\[insert\b",
        r"\binsert actual date\b",
        r"\bare my hobbies\b",
        r"\bpreferred beverage\b",
        r"\breading and playing\b",
    )
)

_PERSONAL_FACT_SIGNALS: tuple[re.Pattern[str], ...] = _EXPLICIT_FACT_PATTERNS

_SKIP_LOG_REPLY_PATHS = frozenset(
    {
        "alarm",
        "volume",
        "servo_360",
        "recap",
        "recap_blocked_no_face",
        "identity_llm",
        "memory_recall",
        "memory_llm_recall",
        "memory_llm_store",
        "stt_rejected",
    }
)

_MEMORY_TOPIC_KEYWORDS: dict[str, tuple[str, ...]] = {
    "medicine": ("medicine", "medication", "meds", "pill", "alarm", "reminder", "take"),
    "birthday": ("birthday", "born", "birthdate", "birth date"),
    "beverage": ("tea", "coffee", "beer", "drink", "beverage"),
    "food": ("food", "idli", "rice", "meal", "eat", "favourite", "favorite"),
    "work": ("job", "work", "engineer", "project", "microcontroller"),
}

_RECALL_KEY_HINTS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bfood\b", re.I), "favorite_food"),
    (re.compile(r"\b(?:soft\s+)?drinks?\b|\b(?:tea|coffee|beverage)\b", re.I), "favorite_drink"),
    (re.compile(r"\bsports?\b", re.I), "favorite_sport"),
    (re.compile(r"\bbirthday|birthdate|born\b", re.I), "birthdate"),
    (re.compile(r"\bhobb", re.I), "hobbies"),
    (re.compile(r"\bjob\b|\bwork\b", re.I), "job_title"),
    (re.compile(r"\bgame\b", re.I), "favorite_game"),
)

_FAVORITE_TOPIC_EXTRACT = re.compile(
    r"\bwhat(?:'s| is) my (?:favourite|favorite)\s+(.+?)\s*\??$",
    re.IGNORECASE,
)
_PREFER_TOPIC_EXTRACT = re.compile(
    r"\bwhat do i prefer(?:\s+to)?\s+(.+?)\s*\??$",
    re.IGNORECASE,
)

_TOPIC_CANONICAL_KEYS: dict[str, str] = {
    "food": "favorite_food",
    "drink": "favorite_drink",
    "drinks": "favorite_drink",
    "beverage": "favorite_drink",
    "beverages": "favorite_drink",
    "soft_drink": "favorite_drink",
    "soft_drinks": "favorite_drink",
    "sport": "favorite_sport",
    "sports": "favorite_sport",
    "game": "favorite_game",
    "hobby": "hobbies",
    "hobbies": "hobbies",
}

_MEMORY_KEY_LOOKUP_ALIASES: dict[str, tuple[str, ...]] = {
    "favorite_beverage": ("favorite_drink",),
    "favorite_drink": ("favorite_beverage",),
}

_PREFERENCE_UPDATE = re.compile(
    r"\b(?:please\s+)?(?:my\s+)?(?:favourite|favorite)\s+([\w\s]+?)\s+is\s+(.+?)\s*$",
    re.IGNORECASE,
)

_PREFERENCE_UPDATE_REVERSED = re.compile(
    r"^(.+?)\s+is\s+my\s+(?:favourite|favorite)\s+([\w\s]+?)\s*\.?$",
    re.IGNORECASE,
)

_PREFERENCE_LIKE = re.compile(
    r"\bi\s+(?:really\s+)?(?:like|love|prefer)\s+(.+?)\s*\.?$",
    re.IGNORECASE,
)

_PREFERENCE_DISLIKE = re.compile(
    r"\bi\s+(?:really\s+)?(?:don'?t\s+like|dislike|hate)\s+(.+?)\s*\.?$",
    re.IGNORECASE,
)

_BIRTHDATE_UPDATE_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\bbirth\s*date\s+is\s+(?:on\s+)?(.+?)\s*\.?$",
        r"\bbirthday\s+is\s+(?:on\s+)?(.+?)\s*\.?$",
        r"\bmy\s+birthday\s+is\s+(?:on\s+)?(.+?)\s*\.?$",
        r"\bmy\s+birth\s*date\s+is\s+(?:on\s+)?(.+?)\s*\.?$",
        r"\bi\s+was\s+born\s+(?:on\s+)?(.+?)\s*\.?$",
        r"\bborn\s+on\s+(.+?)\s*\.?$",
    )
)


def is_ephemeral_query(user_text: str) -> bool:
    text = user_text.strip()
    if not text:
        return False
    return any(p.search(text) for p in _EPHEMERAL_PATTERNS)


def is_trivia_query(user_text: str) -> bool:
    text = user_text.strip()
    if not text:
        return False
    return any(p.search(text) for p in _TRIVIA_PATTERNS)


def is_question_query(user_text: str) -> bool:
    text = user_text.strip()
    if not text:
        return False
    if text.endswith("?"):
        return True
    if _QUESTION_LEAD.search(text):
        return True
    return False


def is_memory_recall_question(user_text: str) -> bool:
    text = user_text.strip()
    if not text:
        return False
    return any(p.search(text) for p in _MEMORY_RECALL_PATTERNS)


def is_preference_update_statement(user_text: str) -> bool:
    text = user_text.strip()
    if not text:
        return False
    if is_memory_recall_question(text):
        return False
    if is_likely_tts_echo(text):
        return False
    return (
        parse_preference_update(text) is not None
        or parse_like_dislike_update(text) is not None
        or parse_birthdate_update(text) is not None
    )


def parse_birthdate_update(user_text: str) -> str | None:
    """Return normalized birthdate text from 'my birthday is June 25th'."""
    text = user_text.strip()
    if is_memory_recall_question(text):
        return None
    if is_likely_tts_echo(text):
        return None
    for pattern in _BIRTHDATE_UPDATE_PATTERNS:
        match = pattern.search(text)
        if match:
            value = match.group(1).strip().rstrip(".!?…")
            if value and value.lower() not in _RECALL_PRONOUNS:
                return value
    return None


def is_unintelligible_stt(user_text: str) -> bool:
    """Garbled or empty STT — do not send to the LLM."""
    text = user_text.strip()
    if not text:
        return True
    if re.fullmatch(r"[\s.*…\-!]+", text):
        return True
    letters = sum(1 for c in text if c.isalpha())
    if len(text) >= 4 and letters < 3:
        return True
    return False


def _clean_recall_topic(raw: str) -> str:
    topic = raw.strip().rstrip(".!?…")
    topic = topic.split("?")[0].strip()
    topic = re.split(r"\s+or\s+", topic, maxsplit=1, flags=re.IGNORECASE)[0].strip()
    return topic


def infer_recall_memory_key(user_text: str) -> str | None:
    """Map a recall question to a memories.memory_key slot."""
    if not is_memory_recall_question(user_text):
        return None
    text = user_text.strip()

    for pattern in (_FAVORITE_TOPIC_EXTRACT, _PREFER_TOPIC_EXTRACT):
        match = pattern.search(text)
        if match:
            topic = _clean_recall_topic(match.group(1))
            if topic and topic.lower() not in _RECALL_PRONOUNS:
                return canonical_preference_key(topic)

    for pattern, key in _RECALL_KEY_HINTS:
        if pattern.search(text):
            return key

    if re.search(r"\b(?:don'?t|do not|dislike|hate)\b", text, re.IGNORECASE):
        return RECALL_DISLIKES
    if re.search(r"\bwhat do i (?:like|love|enjoy)\b", text, re.IGNORECASE):
        return RECALL_ALL_PREFERENCES
    if re.search(r"\bwhat do i prefer\b", text, re.IGNORECASE):
        return RECALL_ALL_PREFERENCES
    return None


def canonical_preference_key(topic: str) -> str:
    """Normalize a spoken topic ('soft drink', 'sport') to a memories.memory_key."""
    slug = normalize_memory_key(topic)
    if slug in _TOPIC_CANONICAL_KEYS:
        return _TOPIC_CANONICAL_KEYS[slug]
    if slug.startswith("favorite_"):
        if slug == "favorite_beverage":
            return "favorite_drink"
        return slug
    return normalize_memory_key(f"favorite_{slug}")


def memory_key_lookup_candidates(primary_key: str) -> tuple[str, ...]:
    """Keys to try when reading preferences (handles drink vs beverage naming)."""
    seen: set[str] = set()
    ordered: list[str] = []
    for key in (primary_key, *_MEMORY_KEY_LOOKUP_ALIASES.get(primary_key, ())):
        if key and key not in seen:
            seen.add(key)
            ordered.append(key)
    return tuple(ordered)


def parse_preference_update(user_text: str) -> tuple[str, str] | None:
    """Return (topic, value) from 'my favorite food is Idli' or 'Idli is my favorite food'."""
    text = user_text.strip()
    match = _PREFERENCE_UPDATE.search(text)
    if match:
        topic = match.group(1).strip().lower()
        value = match.group(2).strip().rstrip(".!?…")
        if topic and value:
            return topic, value
    match = _PREFERENCE_UPDATE_REVERSED.match(text)
    if match:
        value = match.group(1).strip().rstrip(".!?…")
        topic = match.group(2).strip().lower()
        if value.lower() in _RECALL_PRONOUNS:
            return None
        if topic and value:
            return topic, value
    return None


def parse_like_dislike_update(user_text: str) -> tuple[str, str] | None:
    """Return ('like'|'dislike', subject) from 'I hate mushrooms'."""
    text = user_text.strip()
    if parse_preference_update(text):
        return None
    match = _PREFERENCE_DISLIKE.search(text)
    if match:
        subject = match.group(1).strip().rstrip(".!?…")
        if subject:
            return "dislike", subject
    match = _PREFERENCE_LIKE.search(text)
    if match:
        subject = match.group(1).strip().rstrip(".!?…")
        if subject and subject.lower() not in _RECALL_PRONOUNS:
            return "like", subject
    return None


def preference_update_memory_key(topic: str) -> str:
    return canonical_preference_key(topic)


def format_preference_update_ack(user_text: str, *, person_name: str = "") -> str:
    birthdate = parse_birthdate_update(user_text)
    if birthdate:
        prefix = f"OK {person_name}, " if person_name else "OK, "
        return f"{prefix}got it — your birthday is {birthdate}."
    parsed = parse_preference_update(user_text)
    if parsed:
        topic, value = parsed
        prefix = f"OK {person_name}, " if person_name else "OK, "
        return f"{prefix}got it — your favorite {topic} is {value}."
    like_dislike = parse_like_dislike_update(user_text)
    if like_dislike:
        kind, subject = like_dislike
        prefix = f"OK {person_name}, " if person_name else "OK, "
        if kind == "dislike":
            return f"{prefix}got it — you don't like {subject}."
        return f"{prefix}got it — you like {subject}."
    return "Got it, I'll remember that."


def format_memories_list_for_recall(memories: list[str], *, dislikes: bool = False) -> str:
    """Format stored facts for display (used by tests and legacy helpers)."""
    if not memories:
        return ""
    answers = [format_memory_for_recall(m) for m in memories if m.strip()]
    if not answers:
        return ""
    if len(answers) == 1:
        return answers[0]
    if dislikes:
        return "You told me you don't like: " + "; ".join(answers)
    return "Here's what I remember: " + "; ".join(answers)


def format_memory_for_recall(memory_text: str) -> str:
    """Turn a stored fact into a direct second-person spoken answer."""
    text = memory_text.strip()
    match = re.match(
        r"^(.+?)\s+is\s+my\s+(?:favorite|favourite)\s+([\w\s]+?)\.?$",
        text,
        re.IGNORECASE,
    )
    if match:
        return f"Your favorite {match.group(2)} is {match.group(1)}."
    match = re.match(
        r"^(.+?)\s+is\s+\w+'s\s+(?:favorite|favourite)\s+(\w+)\.?$",
        text,
        re.IGNORECASE,
    )
    if match:
        return f"Your favorite {match.group(2)} is {match.group(1)}."
    match = re.match(
        r"^(?:favorite|favourite)\s+(\w+)\s+is\s+(.+?)\.?$",
        text,
        re.IGNORECASE,
    )
    if match:
        return f"Your favorite {match.group(1)} is {match.group(2)}."
    match = re.match(
        r"^my birthday is (?:on )?(.+?)\.?$",
        text,
        re.IGNORECASE,
    )
    if match:
        return f"Your birthday is {match.group(1)}."
    match = re.match(
        r"^birthday is (?:on )?(.+?)\.?$",
        text,
        re.IGNORECASE,
    )
    if match:
        return f"Your birthday is {match.group(1)}."
    match = re.match(
        r"^birth\s*date is (?:on )?(.+?)\.?$",
        text,
        re.IGNORECASE,
    )
    if match:
        return f"Your birthday is {match.group(1)}."
    return text if text.lower().startswith("your ") else f"You told me {text}"


def is_greeting_smalltalk(user_text: str) -> bool:
    text = user_text.strip()
    if not text:
        return False
    return any(p.search(text) for p in _GREETING_PATTERNS)


def is_likely_tts_echo(user_text: str) -> bool:
    """Whisper sometimes transcribes NiNO's own TTS instead of the user."""
    text = user_text.strip()
    if not text:
        return False
    if any(p.search(text) for p in _TTS_ECHO_PATTERNS):
        if not re.search(r"\bi (?:was )?born\b", text, re.IGNORECASE):
            return True
    return False


def user_explicitly_states_personal_fact(user_text: str) -> bool:
    text = user_text.strip()
    if not text or is_question_query(text) or is_likely_tts_echo(text):
        return False
    return any(p.search(text) for p in _EXPLICIT_FACT_PATTERNS)


def is_alarm_followup_question(user_text: str) -> bool:
    text = user_text.strip()
    if not text:
        return False
    return any(p.search(text) for p in _ALARM_FOLLOWUP_PATTERNS)


def is_alarm_or_reminder_command(user_text: str) -> bool:
    """Alarm/reminder voice commands are not durable chat — never log to conversations."""
    text = user_text.strip()
    if not text:
        return False
    from alarm_medical import looks_like_medicine_reminder_set
    from alarm_voice import (
        is_cancel_all_alarm_command,
        is_delete_one_alarm_command,
        is_list_alarm_command,
        is_reminder_command,
        is_set_alarm_command,
    )

    if looks_like_medicine_reminder_set(text):
        return True
    return any(
        fn(text)
        for fn in (
            is_set_alarm_command,
            is_reminder_command,
            is_list_alarm_command,
            is_cancel_all_alarm_command,
            is_delete_one_alarm_command,
        )
    )


def user_shares_personal_fact(user_text: str) -> bool:
    return user_explicitly_states_personal_fact(user_text)


def conversation_log_skip_reason(user_text: str, *, reply_path: str = "llm") -> str | None:
    """Deny-list: store all real chat unless it is noise (jokes, recap, alarms, echo, etc.)."""
    from memory_service import is_stt_fragment

    if reply_path in _SKIP_LOG_REPLY_PATHS:
        return f"skipped_{reply_path}"
    if reply_path not in {"llm", "recap_answer"}:
        return f"skipped_{reply_path}"
    if is_stt_fragment(user_text):
        return "skipped_fragment"
    from llm_service import is_conversation_recap_question

    if is_conversation_recap_question(user_text):
        return "skipped_recap"
    if is_ephemeral_query(user_text):
        return "skipped_ephemeral"
    if is_alarm_followup_question(user_text):
        return "skipped_alarm_followup"
    if is_alarm_or_reminder_command(user_text):
        return "skipped_alarm_command"
    if is_memory_recall_question(user_text):
        return "skipped_recall"
    if is_likely_tts_echo(user_text):
        return "skipped_tts_echo"
    return None


def memory_extract_skip_reason(user_text: str, *, reply_path: str = "llm") -> str | None:
    """Long-term facts: only when the user explicitly states something personal."""
    if reply_path != "llm":
        return f"skipped_{reply_path}"
    from memory_service import is_stt_fragment

    if is_stt_fragment(user_text):
        return "skipped_fragment"
    from llm_service import is_conversation_recap_question

    if is_conversation_recap_question(user_text):
        return "skipped_recap"
    if is_ephemeral_query(user_text):
        return "skipped_ephemeral"
    if is_question_query(user_text):
        return "skipped_question"
    if is_memory_recall_question(user_text):
        return "skipped_recall"
    if is_likely_tts_echo(user_text):
        return "skipped_tts_echo"
    if is_alarm_followup_question(user_text):
        return "skipped_alarm_followup"
    if is_alarm_or_reminder_command(user_text):
        return "skipped_alarm_command"
    if not user_explicitly_states_personal_fact(user_text):
        return "skipped_not_personal_fact"
    return None


def memory_grounded_in_user_text(memory_text: str, user_text: str) -> bool:
    """Reject LLM-hallucinated facts that never appeared in the user's speech."""
    mem = memory_text.strip().lower()
    user = user_text.strip().lower()
    if not mem or not user:
        return False
    if "[" in mem or "insert" in mem:
        return False
    tokens = [
        word
        for word in re.findall(r"[a-z]{4,}", mem)
        if word not in _MEMORY_STOPWORDS
    ]
    if not tokens:
        return False
    hits = sum(1 for word in tokens if word in user)
    required = 2 if len(tokens) >= 2 else 1
    return hits >= required


def should_extract_memories(user_text: str, *, reply_path: str = "llm") -> bool:
    """Run Ollama memory extraction only for durable personal chat (async backup)."""
    return memory_extract_skip_reason(user_text, reply_path=reply_path) is None


def normalize_memory_key(raw: str) -> str:
    key = re.sub(r"[^a-z0-9_]+", "_", raw.strip().lower()).strip("_")
    return key[:64]


def infer_memory_key(memory_text: str, explicit_key: str = "") -> str:
    if explicit_key:
        normalized = normalize_memory_key(explicit_key)
        if normalized and normalized not in {"other", "none", "unknown"}:
            return normalized
    text = memory_text.strip()
    for pattern, key in _MEMORY_KEY_ALIASES:
        if pattern.search(text):
            return key
    digest = hashlib.sha1(text.lower().encode()).hexdigest()[:10]
    return f"fact_{digest}"


def is_junk_memory_text(memory_text: str) -> bool:
    text = memory_text.strip()
    if len(text) < 8:
        return True
    if len(text.split()) < 2:
        return True
    return any(p.search(text) for p in _JUNK_MEMORY_PATTERNS)


def enrich_llm_memory_text(memory_text: str, memory_key: str, user_text: str) -> str:
    """Expand terse LLM extractions (e.g. 'Biryani') into storable facts."""
    mem = memory_text.strip()
    user = user_text.strip().rstrip(".!?…")
    if len(mem) >= 12 and len(mem.split()) >= 3:
        return mem
    if mem.lower() in user.lower() and len(user) >= 12:
        durable_signals = (
            "favorite",
            "favourite",
            "birthday",
            "born",
            "prefer",
            "hate",
            "dislike",
            "allerg",
            "instead",
        )
        if any(signal in user.lower() for signal in durable_signals):
            return user
    if memory_key.startswith("favorite_"):
        pref_signals = (
            "favorite",
            "favourite",
            "prefer",
            "like",
            "love",
            "hate",
            "dislike",
            "instead",
        )
        topic = memory_key.replace("favorite_", "", 1).replace("_", " ")
        if any(signal in user.lower() for signal in pref_signals) or topic in user.lower():
            return f"Favorite {topic} is {mem}"
        # LLM mis-keyed a non-preference utterance (e.g. "full form of Wi-Fi" → favorite_food).
        return mem
    if memory_key == "birthdate":
        return f"My birthday is on {mem}"
    label = memory_key.replace("_", " ")
    return f"{label}: {mem}"


def is_valid_llm_memory_item(
    memory_text: str,
    *,
    user_text: str,
    memory_key: str = "",
) -> bool:
    """Validation for facts the LLM already chose to store (less strict than async)."""
    text = memory_text.strip()
    if not text:
        return False
    if is_junk_memory_text(text):
        if memory_key and len(text) >= 3 and memory_grounded_in_user_text(text, user_text):
            return True
        return False
    return memory_grounded_in_user_text(text, user_text)


def is_valid_memory_text(
    memory_text: str,
    *,
    user_text: str,
    assistant_text: str,
) -> bool:
    text = memory_text.strip()
    if is_junk_memory_text(text):
        return False
    assistant = assistant_text.strip().lower()
    if assistant and text.lower() in assistant:
        return False
    # Reject facts that only parrot the assistant reply
    if assistant and len(text) > 20 and text.lower()[:40] in assistant:
        return False
    if not memory_grounded_in_user_text(text, user_text):
        return False
    return True


# ---------------------------------------------------------------------------
# Follow-up detection — decide whether recent conversation lines should be
# injected into the LLM prompt. Standalone questions must NOT inherit prior
# turns (that caused context bleed, e.g. "which planet has a ring?" answered
# from an earlier Pluto discussion).
# ---------------------------------------------------------------------------

_FOLLOWUP_CONNECTOR = re.compile(
    r"^\s*(?:and|so|then|also|besides|plus|but|what about|how about|"
    r"what else|anything else|and then|ok(?:ay)?\s+(?:and|so))\b",
    re.IGNORECASE,
)

_FOLLOWUP_CONTINUATION = re.compile(
    r"\b(?:tell me more|more about (?:it|that|this|them|those)|go on|continue|"
    r"say more|explain (?:that|it|this|more|again)|"
    r"(?:what|how) about (?:it|that|this|them|those)|"
    r"why is that|how come|what else|and you|elaborate)\b",
    re.IGNORECASE,
)

_ANAPHORA = re.compile(
    r"\b(?:it|its|it's|that|this|these|those|them|they|he|she|his|her|him|their)\b",
    re.IGNORECASE,
)


def query_needs_recent_context(user_text: str) -> bool:
    """True only when the query is a follow-up that references prior turns.

    Standalone questions ('which planet has a ring?', 'what's the weather?')
    return False so recent conversation lines are NOT fed to the LLM — this
    prevents unrelated new questions from inheriting stale context.
    """
    text = user_text.strip()
    if not text:
        return False
    if _FOLLOWUP_CONNECTOR.search(text):
        return True
    if _FOLLOWUP_CONTINUATION.search(text):
        return True
    # Short anaphoric follow-ups ('what is it made of?', 'how does that work?').
    word_count = len(re.findall(r"[A-Za-z0-9']+", text))
    if word_count <= 8 and _ANAPHORA.search(text):
        return True
    return False


def filter_recent_turns_for_prompt(
    recent: list[tuple[str, str]],
) -> list[tuple[str, str]]:
    """Drop deny-listed turns from recap context; keep everything else."""
    from memory_service import is_stt_fragment

    cleaned: list[tuple[str, str]] = []
    for user_text, assistant_text in recent:
        if conversation_log_skip_reason(user_text, reply_path="llm") is not None:
            continue
        if is_stt_fragment(user_text):
            continue
        cleaned.append((user_text.strip(), assistant_text.strip()))
    return cleaned


def _query_topics(user_text: str) -> set[str]:
    lower = user_text.lower()
    topics: set[str] = set()
    for topic, keywords in _MEMORY_TOPIC_KEYWORDS.items():
        if any(k in lower for k in keywords):
            topics.add(topic)
    return topics


def filter_memories_for_query(memories: list[str], user_text: str) -> list[str]:
    """Keep facts relevant to the current question; drop obvious mismatches."""
    if not memories or not user_text.strip():
        return memories

    topics = _query_topics(user_text)
    if not topics:
        return memories

    scored: list[tuple[int, str]] = []
    for mem in memories:
        lower = mem.lower()
        score = 0
        for topic in topics:
            if any(k in lower for k in _MEMORY_TOPIC_KEYWORDS[topic]):
                score += 3
        if "medicine" in topics or "medicine" in user_text.lower():
            if any(k in lower for k in _MEMORY_TOPIC_KEYWORDS["birthday"]):
                score -= 5
            if "don't believe" in lower or "joke" in lower:
                score -= 5
        if score < 0:
            continue
        scored.append((score, mem))

    if not scored:
        return []

    scored.sort(key=lambda item: (-item[0], item[1]))
    positive = [m for s, m in scored if s > 0]
    if positive:
        return positive[:10]
    return [m for _, m in scored[:5]]

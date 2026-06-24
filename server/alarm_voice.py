"""Voice command parsing for the alarm system."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from alarm_medical import is_medical_set_command
from alarm_service import Alarm, get_alarm_service
from alarm_time import system_now

logger = logging.getLogger(__name__)

_SET_ALARM_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\bset\s+(?:an?\s+)?alarm\s+(?:at|for)\s+(.+)",
        r"\bset\s+(?:an?\s+)?alarm\s+(.+)",
        r"\b(?:create|make)\s+(?:an?\s+)?alarm\s+(?:at|for)\s+(.+)",
        r"\balarm\s+(?:at|for)\s+(.+)",
    )
)

_DELETE_ONE_ALARM_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\b(?:cancel|delete|remove)\s+(?:my\s+)?(?:the\s+)?(?:alarm|reminder)\s+(?:at|for)\s+(.+)",
        r"\b(?:cancel|delete|remove)\s+(?:my\s+)?(.+?)\s+(?:alarm|reminder)\b",
        r"\b(?:cancel|delete|remove)\s+(?:the\s+)?(?:alarm|reminder)\s+(?:called|named)\s+(.+)",
    )
)

_CANCEL_ALL_ALARM_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\b(?:cancel|delete|remove|turn\s+off|stop)\s+(?:all\s+)?(?:my\s+)?alarms?\s*$",
        r"\b(?:cancel|delete|remove|turn\s+off|stop)\s+(?:all\s+)?(?:my\s+)?reminders?\s*$",
        r"\b(?:cancel|delete|remove)\s+(?:all\s+)?(?:my\s+)?(?:alarms?|reminders?)\b",
        r"\bclear\s+(?:all\s+)?(?:my\s+)?(?:alarms?|reminders?)\b",
    )
)

_LIST_ALARM_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\bwhat\s+(?:alarms?|reminders?)\s+(?:do\s+i\s+have|are\s+set)\b",
        r"\bwhat\s+(?:are\s+)?(?:my\s+)?(?:alarms?|reminders?)\b",
        r"\blist\s+(?:all\s+)?(?:my\s+)?(?:alarms?|reminders?)\b",
        r"\b(?:list|show|tell)(?:\s+(?:me|us))?\s+(?:my\s+)?(?:alarms?|reminders?)\b",
        r"\bshow\s+(?:me\s+)?(?:my\s+)?(?:alarms?|reminders?)\b",
        r"\bdo\s+i\s+have\s+(?:any\s+)?(?:alarms?|reminders?)\b",
        r"\b(?:any|some)\s+(?:alarms?|reminders?)\s+(?:set|scheduled|pending)\b",
        r"\bhow\s+many\s+(?:alarms?|reminders?)\s+(?:do\s+i\s+have|are\s+set)\b",
        r"\b(?:list|show).{0,24}\b(?:alarms?|reminders?)\b",
    )
)

_REMINDER_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\bremind(?:er)?\s+me\s+to\s+(.+)",
        r"\bremaind\s+me\s+to\s+(.+)",  # common speech/Whisper typo
        r"\bset\s+(?:a\s+)?reminder\s+to\s+(.+)",
        r"\b(?:create|make)\s+(?:a\s+)?reminder\s+to\s+(.+)",
    )
)

_AT_TIME_SUFFIX = re.compile(r"\s+at\s+(.+)$", re.IGNORECASE)

_TIME_TOKEN = re.compile(
    r"""
    (?P<hour>\d{1,2})
    (?:
        \s*[.:]\s*(?P<minute>\d{1,2})
        | \s+(?P<minute_spaced>\d{1,2})
        | \s*(?P<minute_compact>\d{2})\s*(?=[ap]\.?m\b)
    )?
    \s*
    (?P<ampm>a\.?m\.?|p\.?m\.?)?
    """,
    re.IGNORECASE | re.VERBOSE,
)

_COMPACT_BEFORE_AMPM = re.compile(
    r"\b(?P<digits>\d{3,4})\s*(?P<ampm>a\.?m\.?|p\.?m\.?)\b",
    re.IGNORECASE,
)


@dataclass
class AlarmVoiceResult:
    handled: bool
    reply: str = ""


@dataclass
class AlarmParseResult:
    fire_at: datetime | None = None
    error: str | None = None
    rolled_to_tomorrow: bool = False


def is_set_alarm_command(user_text: str) -> bool:
    text = user_text.strip()
    if not text:
        return False
    return any(p.search(text) for p in _SET_ALARM_PATTERNS)


def is_delete_one_alarm_command(user_text: str) -> bool:
    text = user_text.strip()
    if not text:
        return False
    return any(p.search(text) for p in _DELETE_ONE_ALARM_PATTERNS)


def is_cancel_all_alarm_command(user_text: str) -> bool:
    text = user_text.strip()
    if not text:
        return False
    if is_delete_one_alarm_command(text):
        return False
    return any(p.search(text) for p in _CANCEL_ALL_ALARM_PATTERNS)


def is_list_alarm_command(user_text: str) -> bool:
    text = user_text.strip()
    if not text:
        return False
    return any(p.search(text) for p in _LIST_ALARM_PATTERNS)


def _looks_like_list_after_nlp(user_text: str) -> bool:
    """Catch garbled list requests that failed strict regex and NLP set path."""
    text = user_text.strip().lower()
    if not text:
        return False
    has_list_word = bool(re.search(r"\b(list|show|tell|what|any|how many)\b", text))
    has_alarm_word = bool(re.search(r"\b(alarms?|reminders?)\b", text))
    has_set_word = bool(re.search(r"\b(set|remind|wake|forget)\b", text))
    return has_list_word and has_alarm_word and not has_set_word


def is_reminder_command(user_text: str) -> bool:
    text = user_text.strip()
    if not text:
        return False
    return any(p.search(text) for p in _REMINDER_PATTERNS)


def _resolve_person_name(
    *,
    person_name: str = "",
    camera_identity_name: str | None = None,
    camera_identity_state: str = "no_face",
) -> str:
    """Name for alarm speech — only from live face recognition, never session fallback."""
    if person_name:
        name = str(person_name).strip()
        if name and name.lower() not in {"unknown", "face"}:
            return name[:64]
    if camera_identity_state == "recognized" and camera_identity_name:
        name = str(camera_identity_name).strip()
        if name and name.lower() not in {"unknown", "face"}:
            return name[:64]
    return ""


def handle_alarm_voice(
    user_text: str,
    *,
    user_id: int | None = None,
    person_name: str = "",
    camera_identity_name: str | None = None,
    camera_identity_state: str = "no_face",
) -> AlarmVoiceResult:
    text = user_text.strip()
    if not text:
        return AlarmVoiceResult(handled=False)

    resolved_name = _resolve_person_name(
        person_name=person_name,
        camera_identity_name=camera_identity_name,
        camera_identity_state=camera_identity_state,
    )

    from alarm_ack import handle_alarm_ack_voice

    ack_result = handle_alarm_ack_voice(text)
    if ack_result.handled:
        return ack_result

    if is_delete_one_alarm_command(text):
        result = _handle_delete_one_alarm(text, user_id=user_id)
        if result.handled:
            return result
    if is_cancel_all_alarm_command(text):
        return _handle_cancel_alarms(user_id=user_id)
    if is_list_alarm_command(text):
        logger.info("Voice list alarms (regex) | heard: %s", text[:120])
        return _handle_list_alarms(user_id=user_id)

    regex_set_attempted = False

    if is_medical_set_command(text):
        regex_set_attempted = True
        result = _handle_set_medical_alarm(
            text, person_name=resolved_name, user_id=user_id
        )
        if result.handled:
            return result
    if is_reminder_command(text):
        regex_set_attempted = True
        result = _handle_set_reminder(
            text, person_name=resolved_name, user_id=user_id
        )
        if result.handled:
            return result
    elif is_set_alarm_command(text):
        regex_set_attempted = True
        result = _handle_set_alarm(text, person_name=resolved_name, user_id=user_id)
        if result.handled:
            return result

    from alarm_nlp import looks_alarm_related, nlp_fallback_enabled, try_nlp_alarm

    if nlp_fallback_enabled() and (regex_set_attempted or looks_alarm_related(text)):
        nlp_result = try_nlp_alarm(
            text, person_name=resolved_name, user_id=user_id
        )
        if nlp_result.handled:
            logger.info("Alarm handled via Ollama NLP fallback | heard: %s", text[:120])
            return nlp_result

    # List phrasing + "alarm" but regex missed — still list, don't send to general chat
    if is_list_alarm_command(text) or _looks_like_list_after_nlp(text):
        logger.info("Voice list alarms (loose match) | heard: %s", text[:120])
        return _handle_list_alarms(user_id=user_id)

    if regex_set_attempted:
        return AlarmVoiceResult(
            handled=True,
            reply="I could not set that reminder. Try remind me to take medicines at 6 AM.",
        )

    return AlarmVoiceResult(handled=False)


def _extract_time_phrase(user_text: str) -> str | None:
    text = user_text.strip()
    for pattern in _SET_ALARM_PATTERNS:
        match = pattern.search(text)
        if match:
            return match.group(1).strip()
    return None


def _extract_reminder_tail(user_text: str) -> str | None:
    text = user_text.strip()
    for pattern in _REMINDER_PATTERNS:
        match = pattern.search(text)
        if match:
            return match.group(1).strip()
    return None


def _split_label_and_time_phrase(tail: str) -> tuple[str, str] | None:
    """Split 'take medicines at 6 am today' → ('take medicines', '6 am today')."""
    match = _AT_TIME_SUFFIX.search(tail.strip())
    if not match:
        return None
    label = tail[: match.start()].strip().rstrip(",.")
    time_phrase = match.group(1).strip()
    if not label or not time_phrase:
        return None
    return label, time_phrase


def _extract_time_phrase_from_text(user_text: str) -> str:
    """Pull a time phrase from mixed alarm/reminder utterances."""
    phrase = _extract_time_phrase(user_text)
    if phrase:
        split = _split_label_and_time_phrase(phrase)
        if split:
            return split[1]
        if parse_alarm_datetime(phrase).fire_at is not None:
            return phrase

    tail = _extract_reminder_tail(user_text)
    if tail:
        split = _split_label_and_time_phrase(tail)
        if split:
            return split[1]

    for pattern in _SET_ALARM_PATTERNS:
        match = pattern.search(user_text)
        if match:
            tail = match.group(1).strip()
            split = _split_label_and_time_phrase(tail)
            if split:
                return split[1]
            at_match = _AT_TIME_SUFFIX.search(tail)
            if at_match:
                return at_match.group(1).strip()

    at_match = _AT_TIME_SUFFIX.search(user_text)
    if at_match:
        return at_match.group(1).strip()
    return ""


def _extract_medical_label(user_text: str) -> str:
    tail = _extract_reminder_tail(user_text)
    if tail:
        split = _split_label_and_time_phrase(tail)
        if split:
            return _clean_reminder_label(split[0])
    for pattern in _SET_ALARM_PATTERNS:
        match = pattern.search(user_text)
        if match:
            split = _split_label_and_time_phrase(match.group(1).strip())
            if split:
                return _clean_reminder_label(split[0])
    return "take your medication"


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


def _clean_reminder_label(label: str) -> str:
    cleaned = label.strip().rstrip(".")
    cleaned = re.sub(r"^to\s+", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s+", " ", cleaned)
    cleaned = normalize_label_for_user(cleaned)
    return cleaned[:120]


def format_time_phrase_parts(
    hour: int, minute: int, ampm: str = "", *, day: str = ""
) -> str:
    """Build a parser-friendly time phrase; fix Ollama 24h + AM/PM mixes like 20:36 PM."""
    ampm_clean = (ampm or "").strip().upper()
    if ampm_clean in {"EMPTY", "NONE", "NULL"}:
        ampm_clean = ""

    if ampm_clean in {"AM", "PM"}:
        if hour > 12:
            hour -= 12
        elif hour == 0:
            hour = 12
        core = f"{hour}:{minute:02d} {ampm_clean}"
    elif hour >= 13:
        core = f"{hour}:{minute:02d}"
    else:
        core = f"{hour}:{minute:02d}"

    if day in {"today", "tomorrow"}:
        return f"{core} {day}"
    return core


def _normalize_time_phrase(phrase: str) -> str:
    """Whisper-friendly fixes before regex parse (3.50 → 3:50, 350 AM → 3:50 AM)."""
    text = phrase.strip()

    def _fix_24h_ampm(match: re.Match[str]) -> str:
        hour = int(match.group("hour"))
        minute = int(match.group("minute"))
        ampm = match.group("ampm")
        if hour > 12:
            hour -= 12
        elif hour == 0:
            hour = 12
        return f"{hour}:{minute:02d} {ampm}"

    text = re.sub(
        r"\b(?P<hour>\d{1,2}):(?P<minute>\d{2})\s*(?P<ampm>a\.?m\.?|p\.?m\.?)\b",
        _fix_24h_ampm,
        text,
        flags=re.IGNORECASE,
    )

    def _compact_replace(match: re.Match[str]) -> str:
        digits = match.group("digits")
        ampm = match.group("ampm")
        if len(digits) == 3:
            hour, minute = int(digits[0]), int(digits[1:])
        else:
            hour, minute = int(digits[:2]), int(digits[2:])
        if hour > 23 or minute > 59:
            return match.group(0)
        return f"{hour}:{minute:02d} {ampm}"

    text = _COMPACT_BEFORE_AMPM.sub(_compact_replace, text)
    text = re.sub(
        r"\b(\d{1,2})[.](\d{2})\b",
        r"\1:\2",
        text,
    )
    return text


def parse_alarm_datetime(phrase: str) -> AlarmParseResult:
    """Parse a time phrase using the PC system clock as 'now'."""
    cleaned = _normalize_time_phrase(phrase.strip().rstrip("."))
    lower = cleaned.lower()

    day_offset = 0
    explicit_today = "today" in lower
    if "tomorrow" in lower:
        day_offset = 1
        cleaned = re.sub(r"\btomorrow\b", "", cleaned, flags=re.IGNORECASE).strip()
    cleaned = re.sub(r"\btoday\b", "", cleaned, flags=re.IGNORECASE).strip()

    match = _TIME_TOKEN.search(cleaned)
    if not match:
        return AlarmParseResult(error="I could not understand that time. Try something like 4:30 AM.")

    hour = int(match.group("hour"))
    minute_raw = match.group("minute") or match.group("minute_spaced") or match.group("minute_compact")
    minute = int(minute_raw) if minute_raw else 0
    ampm = (match.group("ampm") or "").lower().replace(".", "")

    if minute > 59 or hour > 23:
        return AlarmParseResult(error="That time does not look valid.")

    if ampm in {"am", "pm"}:
        if hour < 1 or hour > 12:
            return AlarmParseResult(error="Please use a 12-hour time like 4:30 AM.")
        if ampm == "pm" and hour != 12:
            hour += 12
        elif ampm == "am" and hour == 12:
            hour = 0
    elif hour > 12:
        pass  # 24-hour style, e.g. 16:30
    elif hour <= 12 and not ampm:
        if "pm" in lower or "p m" in lower:
            if hour != 12:
                hour += 12
        elif "am" in lower or "a m" in lower:
            if hour == 12:
                hour = 0

    now = system_now()
    fire_at = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    rolled_to_tomorrow = False

    if day_offset:
        fire_at += timedelta(days=day_offset)
    elif fire_at <= now:
        if explicit_today:
            fire_at += timedelta(days=1)
            rolled_to_tomorrow = True
        else:
            fire_at += timedelta(days=1)

    logger.info(
        "Alarm time parse | phrase=%r normalized=%r system_now=%s fire_at=%s rolled_to_tomorrow=%s",
        phrase,
        cleaned,
        now.isoformat(timespec="seconds"),
        fire_at.isoformat(timespec="seconds"),
        rolled_to_tomorrow,
    )

    return AlarmParseResult(fire_at=fire_at, rolled_to_tomorrow=rolled_to_tomorrow)


def _ok_prefix(person_name: str) -> str:
    name = (person_name or "").strip()
    return f"OK {name}, " if name else "OK, "


def _save_alarm(
    fire_at: datetime,
    parsed: AlarmParseResult,
    *,
    label: str = "",
    person_name: str = "",
    user_id: int | None = None,
    source_text: str = "",
    force_medical: bool = False,
) -> AlarmVoiceResult:
    service = get_alarm_service()
    label = _clean_reminder_label(label)
    alarm = service.add_alarm(
        fire_at,
        label=label,
        person_name=person_name,
        user_id=user_id,
        source_text=source_text or label,
        force_medical=force_medical,
    )
    when = _spoken_alarm_time(alarm)
    ok = _ok_prefix(person_name)
    medical = alarm.is_medical()
    priority_note = " Priority medical reminder." if medical else ""

    if label:
        if parsed.rolled_to_tomorrow:
            return AlarmVoiceResult(
                handled=True,
                reply=(
                    f"That time already passed today. "
                    f"{ok}I will remind you to {label} at {when} tomorrow.{priority_note}"
                ),
            )
        day_note = _day_note_for(fire_at)
        return AlarmVoiceResult(
            handled=True,
            reply=f"{ok}I will remind you to {label} at {when}{day_note}.{priority_note}",
        )

    if parsed.rolled_to_tomorrow:
        return AlarmVoiceResult(
            handled=True,
            reply=f"That time already passed today. {ok}alarm set for {when} tomorrow.",
        )

    return AlarmVoiceResult(
        handled=True,
        reply=f"{ok}alarm set for {when}{_day_note_for(fire_at)}.",
    )


def _day_note_for(fire_at: datetime) -> str:
    now = system_now()
    if fire_at.date() == now.date():
        return " today"
    if fire_at.date() == (now + timedelta(days=1)).date():
        return " tomorrow"
    return ""


def _handle_set_reminder(
    user_text: str, *, person_name: str = "", user_id: int | None = None
) -> AlarmVoiceResult:
    tail = _extract_reminder_tail(user_text)
    if not tail:
        return AlarmVoiceResult(handled=False)

    split = _split_label_and_time_phrase(tail)
    if not split:
        return AlarmVoiceResult(handled=False)

    raw_label, time_phrase = split
    label = _clean_reminder_label(raw_label)
    parsed = parse_alarm_datetime(time_phrase)
    if parsed.error or parsed.fire_at is None:
        return AlarmVoiceResult(handled=False)

    logger.info(
        "Voice reminder (regex) | person=%r label=%r time_phrase=%r",
        person_name or "(none)",
        label,
        time_phrase,
    )
    return _save_alarm(
        parsed.fire_at,
        parsed,
        label=label,
        person_name=person_name,
        user_id=user_id,
        source_text=user_text,
    )


def _handle_set_medical_alarm(
    user_text: str, *, person_name: str = "", user_id: int | None = None
) -> AlarmVoiceResult:
    from alarm_medical import _MEDICAL_SET_PATTERNS

    phrase = ""
    for pattern in _MEDICAL_SET_PATTERNS:
        match = pattern.search(user_text)
        if not match:
            continue
        if match.lastindex and match.lastindex >= 1:
            phrase = (match.group(1) or "").strip()
        break

    if not phrase:
        phrase = _extract_time_phrase_from_text(user_text)
    if not phrase:
        return AlarmVoiceResult(handled=False)

    parsed = parse_alarm_datetime(phrase)
    if parsed.error or parsed.fire_at is None:
        return AlarmVoiceResult(handled=False)

    label = _extract_medical_label(user_text)
    logger.info(
        "Voice medical alarm | person=%r label=%r phrase=%r",
        person_name or "(none)",
        label,
        phrase,
    )
    return _save_alarm(
        parsed.fire_at,
        parsed,
        label=label,
        person_name=person_name,
        user_id=user_id,
        source_text=user_text,
        force_medical=True,
    )


def _handle_set_alarm(
    user_text: str, *, person_name: str = "", user_id: int | None = None
) -> AlarmVoiceResult:
    phrase = _extract_time_phrase(user_text)
    if not phrase:
        return AlarmVoiceResult(handled=False)

    parsed = parse_alarm_datetime(phrase)
    if parsed.error or parsed.fire_at is None:
        return AlarmVoiceResult(handled=False)

    logger.info("Voice alarm (regex) | person=%r time_phrase=%r", person_name or "(none)", phrase)
    return _save_alarm(
        parsed.fire_at, parsed, person_name=person_name, user_id=user_id, source_text=user_text
    )


def _extract_delete_target(user_text: str) -> str | None:
    text = user_text.strip()
    for pattern in _DELETE_ONE_ALARM_PATTERNS:
        match = pattern.search(text)
        if match:
            target = (match.group(1) or "").strip()
            if target:
                return target
    return None


def _handle_delete_one_alarm(
    user_text: str, *, user_id: int | None = None
) -> AlarmVoiceResult:
    target = _extract_delete_target(user_text)
    if not target:
        return AlarmVoiceResult(handled=False)

    parsed = parse_alarm_datetime(target)
    fire_at = parsed.fire_at if parsed.error is None else None
    label_hint = target if fire_at is None else ""

    removed = get_alarm_service().remove_pending_matching(
        fire_at=fire_at,
        label_hint=label_hint,
        user_id=user_id,
    )
    if removed is None:
        if fire_at is not None:
            return AlarmVoiceResult(
                handled=True,
                reply="I could not find a pending alarm at that time.",
            )
        return AlarmVoiceResult(
            handled=True,
            reply="I could not find a pending alarm matching that.",
        )

    when = _spoken_alarm_time(removed)
    label = (removed.label or "").strip()
    if label:
        label = normalize_label_for_user(label)
        return AlarmVoiceResult(
            handled=True,
            reply=f"OK, I deleted your {label} reminder for {when}.",
        )
    return AlarmVoiceResult(handled=True, reply=f"OK, I deleted your alarm for {when}.")


def _handle_cancel_alarms(*, user_id: int | None = None) -> AlarmVoiceResult:
    service = get_alarm_service()
    count = service.cancel_all(user_id=user_id)
    if count == 0:
        return AlarmVoiceResult(handled=True, reply="You have no alarms set.")
    if count == 1:
        return AlarmVoiceResult(handled=True, reply="OK, I deleted your alarm.")
    return AlarmVoiceResult(handled=True, reply=f"OK, I deleted {count} alarms.")


def _handle_list_alarms(*, user_id: int | None = None) -> AlarmVoiceResult:
    service = get_alarm_service()
    pending = sorted(
        service.list_pending(user_id=user_id), key=lambda a: (a.priority, a.fire_at)
    )
    awaiting = service.list_awaiting_ack(user_id=user_id)
    logger.info(
        "Voice list alarms | pending=%d awaiting_ack=%d",
        len(pending),
        len(awaiting),
    )
    if awaiting and not pending:
        return AlarmVoiceResult(
            handled=True,
            reply=(
                f"You have {len(awaiting)} medication reminder(s) waiting for confirmation. "
                "Please say yes when you have taken it, or no if not."
            ),
        )
    if not pending and not awaiting:
        return AlarmVoiceResult(handled=True, reply="You have no alarms or reminders set.")

    if len(pending) == 1:
        kind = "reminder" if pending[0].label else "alarm"
        return AlarmVoiceResult(
            handled=True,
            reply=f"You have one {kind}: {_describe_alarm(pending[0])}.",
        )

    descriptions = [_describe_alarm(a) for a in pending[:4]]
    body = ". ".join(descriptions)
    if len(pending) > 4:
        return AlarmVoiceResult(
            handled=True,
            reply=f"You have {len(pending)} alarms. {body}. And {len(pending) - 4} more.",
        )
    return AlarmVoiceResult(
        handled=True,
        reply=f"You have {len(pending)} alarms. {body}.",
    )


def _describe_alarm(alarm: Alarm) -> str:
    when = _spoken_alarm_time(alarm)
    kind = "priority medication reminder" if alarm.is_medical() else (
        "reminder" if alarm.label else "alarm"
    )
    now = system_now()
    fire = alarm.fire_datetime()
    day_word = ""
    if fire.date() == (now + timedelta(days=1)).date():
        day_word = "tomorrow "
    name = (alarm.person_name or "").strip()
    name_bit = f"{name}: " if name else ""
    label = (alarm.label or "").strip()
    if label:
        label = normalize_label_for_user(label)
        return f"{name_bit}{kind} {day_word}at {when}, {label}"
    return f"{name_bit}{kind} {day_word}at {when}".strip()


def _spoken_alarm_time(alarm: Alarm) -> str:
    return alarm.spoken_time()

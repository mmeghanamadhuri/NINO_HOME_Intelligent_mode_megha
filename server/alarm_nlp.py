"""Ollama JSON fallback when regex alarm parsing fails or phrasing is non-standard."""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

from alarm_time import system_clock_info
from alarm_voice import (
    AlarmVoiceResult,
    _handle_cancel_alarms,
    _handle_list_alarms,
    _save_alarm,
    format_time_phrase_parts,
    parse_alarm_datetime,
)
from llm_service import DEFAULT_MODEL, DEFAULT_OLLAMA_URL, ollama_generate

logger = logging.getLogger(__name__)

_ALARM_HINT_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\bremind(?:er)?\b",
        r"\bremaind\b",
        r"\balarm\b",
        r"\breminders?\b",
        r"\bwake\s+me\b",
        r"\bdon'?t\s+forget\b",
        r"\bdo\s+not\s+forget\b",
        r"\bforget\s+to\b",
        r"\bnotify\s+me\b",
        r"\balert\s+me\b",
        r"\bin\s+\d+\s+minutes?\b",
    )
)

_JSON_OBJECT = re.compile(r"\{[^{}]*\}", re.DOTALL)


def nlp_fallback_enabled() -> bool:
    return os.environ.get("ALARM_NLP_FALLBACK", "1").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


def looks_alarm_related(user_text: str) -> bool:
    text = user_text.strip()
    if not text:
        return False
    return any(p.search(text) for p in _ALARM_HINT_PATTERNS)


def try_nlp_alarm(user_text: str, *, person_name: str = "") -> AlarmVoiceResult:
    """Call Ollama for structured intent; validate with parse_alarm_datetime before save."""
    if not nlp_fallback_enabled():
        return AlarmVoiceResult(handled=False)

    from alarm_voice import is_list_alarm_command

    if is_list_alarm_command(user_text):
        return _handle_list_alarms()

    try:
        payload = _extract_via_ollama(user_text)
    except Exception as exc:
        logger.warning("Alarm NLP fallback failed: %s", exc)
        return AlarmVoiceResult(handled=False)

    if not payload:
        return AlarmVoiceResult(handled=False)

    intent = str(payload.get("intent", "none")).strip().lower()
    logger.info("Alarm NLP fallback | intent=%s payload=%s", intent, payload)

    if intent in {"none", "unknown", ""}:
        return AlarmVoiceResult(handled=False)

    if intent == "cancel":
        return _handle_cancel_alarms()

    if intent == "list":
        return _handle_list_alarms()

    if intent in {"set_alarm", "set_reminder", "reminder", "alarm"}:
        return _apply_set_intent(payload, user_text, person_name=person_name)

    return AlarmVoiceResult(handled=False)


def _extract_via_ollama(user_text: str) -> dict[str, Any] | None:
    clock = system_clock_info()
    prompt = (
        "You extract alarm and reminder commands from voice transcripts.\n"
        f"Current PC local time: {clock['now']} ({clock['timezone_name']}).\n"
        f"User said: {user_text.strip()}\n\n"
        "Reply with ONLY one JSON object, no markdown, no explanation:\n"
        '{"intent":"none|set_alarm|set_reminder|cancel|list",'
        '"label":"short task or empty",'
        '"time":"H:MM or HH:MM or empty",'
        '"ampm":"AM|PM|empty",'
        '"day":"today|tomorrow|empty"}\n\n'
        "Rules:\n"
        "- set_reminder: user wants a labeled reminder (label = verb phrase, e.g. take medicines, go to school)\n"
        "- set_alarm: only a clock time, no task\n"
        "- set_reminder with medicine/medication/pills in label = priority medical (requires yes/no ack)\n"
        "- cancel / list: user wants to clear or hear pending alarms\n"
        "- time: digits only like 6:00 or 8:30; use ampm when user said AM/PM/morning/evening\n"
        "- morning -> AM, evening/night -> PM unless clearly otherwise\n"
        "- day: today, tomorrow, or empty\n"
        "- intent none if not about alarms or reminders\n"
    )

    model = os.environ.get("OLLAMA_MODEL", DEFAULT_MODEL).strip()
    api_url = os.environ.get("OLLAMA_URL", DEFAULT_OLLAMA_URL).strip()
    raw = ollama_generate(prompt, model=model, api_url=api_url, num_predict=120, timeout_s=45)
    return _parse_json_object(raw)


def _parse_json_object(raw: str) -> dict[str, Any] | None:
    text = raw.strip()
    if not text:
        return None
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        pass
    match = _JSON_OBJECT.search(text)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        return None


def _apply_set_intent(
    payload: dict[str, Any], user_text: str, *, person_name: str = ""
) -> AlarmVoiceResult:
    label = str(payload.get("label", "") or "").strip()
    time_phrase = _payload_to_time_phrase(payload)
    if not time_phrase:
        return AlarmVoiceResult(
            handled=True,
            reply="I could not understand that time. Try remind me to take medicines at 6 AM.",
        )

    parsed = parse_alarm_datetime(time_phrase)
    if parsed.error or parsed.fire_at is None:
        logger.warning(
            "Alarm NLP rejected | user_text=%r time_phrase=%r error=%s",
            user_text[:120],
            time_phrase,
            parsed.error,
        )
        return AlarmVoiceResult(
            handled=True,
            reply=parsed.error or "I could not set that reminder.",
        )

    logger.info(
        "Alarm NLP accepted | person=%r label=%r time_phrase=%r fire_at=%s",
        person_name or "(none)",
        label,
        time_phrase,
        parsed.fire_at.isoformat(timespec="seconds"),
    )
    return _save_alarm(
        parsed.fire_at,
        parsed,
        label=label,
        person_name=person_name,
        source_text=user_text,
    )


def _payload_to_time_phrase(payload: dict[str, Any]) -> str:
    time_raw = str(payload.get("time", "") or "").strip().lower()
    if not time_raw:
        return ""

    ampm = str(payload.get("ampm", "") or "").strip().upper()
    if ampm in {"EMPTY", "NONE", "NULL"}:
        ampm = ""
    day = str(payload.get("day", "") or "").strip().lower()
    if day not in {"today", "tomorrow"}:
        day = ""

    time_raw = re.sub(r"(\d)[.](\d)", r"\1:\2", time_raw)

    if ":" in time_raw:
        hour_s, minute_s = time_raw.split(":", 1)
        minute_s = re.sub(r"\D.*$", "", minute_s)
        if not minute_s:
            return ""
        hour = int(hour_s)
        minute = int(minute_s)
    else:
        digits = re.sub(r"\D", "", time_raw)
        if len(digits) >= 3:
            hour = int(digits[:-2]) if len(digits) > 3 else int(digits[0])
            minute = int(digits[-2:])
        elif digits:
            hour = int(digits)
            minute = 0
        else:
            return ""

    return format_time_phrase_parts(hour, minute, ampm, day=day)

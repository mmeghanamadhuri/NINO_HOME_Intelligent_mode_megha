"""Voice handling for medical alarm acknowledgment and follow-up."""

from __future__ import annotations

import logging
import re

from alarm_medical import (
    ACK_AWAITING,
    ACK_RESCHEDULE_PROMPT,
    is_negative_ack,
    is_positive_ack,
    wants_cancel,
    wants_reschedule,
)
from alarm_service import get_alarm_service
from alarm_medical import medical_label_object
from alarm_voice import AlarmVoiceResult, parse_alarm_datetime

logger = logging.getLogger(__name__)

_TIME_IN_PHRASE = re.compile(
    r"\b(?:at|for)\s+(.+)$",
    re.IGNORECASE,
)


def handle_alarm_ack_voice(user_text: str) -> AlarmVoiceResult:
    service = get_alarm_service()
    text = user_text.strip()
    if not text:
        return AlarmVoiceResult(handled=False)

    prompt_alarm = service.get_reschedule_prompt_alarm()
    if prompt_alarm is not None:
        return _handle_reschedule_follow_up(text, prompt_alarm.id)

    awaiting = service.list_awaiting_ack()
    if not awaiting:
        return AlarmVoiceResult(handled=False)

    target = service.get_active_ack_alarm() or awaiting[-1]

    if is_positive_ack(text):
        if service.confirm_ack(target.id):
            obj = medical_label_object(target.label)
            return AlarmVoiceResult(
                handled=True,
                reply=f"Thank you. I've noted that you have taken your {obj}.",
            )
        return AlarmVoiceResult(handled=False)

    if is_negative_ack(text):
        if service.decline_ack(target.id):
            obj = medical_label_object(target.label)
            return AlarmVoiceResult(
                handled=True,
                reply=(
                    f"I understand you have not taken your {obj} yet. "
                    "Would you like to reschedule this reminder, or cancel it?"
                ),
            )
        return AlarmVoiceResult(handled=False)

    return AlarmVoiceResult(handled=False)


def _handle_reschedule_follow_up(user_text: str, alarm_id: str) -> AlarmVoiceResult:
    service = get_alarm_service()
    alarm = service.get_alarm(alarm_id)
    if alarm is None:
        service.clear_reschedule_prompt()
        return AlarmVoiceResult(handled=False)

    if wants_cancel(user_text) and not wants_reschedule(user_text):
        service.cancel_alarm(alarm_id)
        service.clear_reschedule_prompt()
        return AlarmVoiceResult(handled=True, reply="OK, I cancelled that medication reminder.")

    time_phrase = _extract_reschedule_time(user_text)
    if time_phrase:
        parsed = parse_alarm_datetime(time_phrase)
        if parsed.error or parsed.fire_at is None:
            return AlarmVoiceResult(
                handled=True,
                reply=parsed.error or "I could not understand that time. Try reschedule for 6 PM.",
            )
        updated = service.reschedule_alarm(alarm_id, parsed.fire_at)
        if updated:
            when = updated.spoken_time()
            obj = medical_label_object(updated.label)
            return AlarmVoiceResult(
                handled=True,
                reply=f"OK, I rescheduled your {obj} reminder for {when}.",
            )

    if wants_reschedule(user_text):
        return AlarmVoiceResult(
            handled=True,
            reply="What time should I reschedule your medication reminder for?",
        )

    return AlarmVoiceResult(
        handled=True,
        reply="Say reschedule for a new time, for example reschedule for 6 PM, or say cancel.",
    )


def _extract_reschedule_time(user_text: str) -> str | None:
    text = user_text.strip()
    match = _TIME_IN_PHRASE.search(text)
    if match:
        return (match.group(1) or "").strip()
    if wants_reschedule(text):
        stripped = _RESCHEDULE_PREFIX.sub("", text).strip()
        return stripped or None
    return text if re.search(r"\d|am|pm|morning|evening|noon", text, re.I) else None


_RESCHEDULE_PREFIX = re.compile(
    r"^(?:please\s+)?(?:reschedule|re-schedule)(?:\s+it)?(?:\s+for)?\s*",
    re.IGNORECASE,
)

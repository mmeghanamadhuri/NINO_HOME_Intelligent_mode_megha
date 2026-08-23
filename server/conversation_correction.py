"""Conversation correction — rework replies without overwriting history."""

from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

_CORRECTION_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\bthat(?:'?s| is)\s+(?:wrong|incorrect|not\s+(?:right|correct|true))\b",
        r"\b(?:you(?:'?re| are)|that(?:'?s| is))\s+wrong\b",
        r"\bnot\s+(?:what|how)\s+i\s+(?:said|meant|asked)\b",
        r"\b(?:you\s+)?misunderstood\b",
        r"\bfix(?:\s+that|\s+it|\s+your)\s+(?:reply|answer|response)\b",
        r"\btry\s+again\b",
        r"\bthat(?:'?s| is)\s+not\s+(?:what|how)\b",
        r"\bcorrect\s+(?:that|your(?:self)?)\b",
    )
)

_REWORK_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\b(?:what\s+i\s+(?:meant|said)\s+(?:was|is))\b",
        r"\b(?:actually,?\s+i\s+(?:meant|wanted|asked))\b",
        r"\b(?:no,?\s+i\s+(?:meant|said|asked))\b",
    )
)


def is_correction_request(user_text: str) -> bool:
    text = (user_text or "").strip()
    if not text or len(text) > 200:
        return False
    return any(p.search(text) for p in _CORRECTION_PATTERNS)


def extract_rework_hint(user_text: str) -> str:
    """Optional clarification the user gave with the correction."""
    text = (user_text or "").strip()
    for pat in _REWORK_PATTERNS:
        m = pat.search(text)
        if m:
            return text[m.start() :].strip()
    return ""


def rework_voice_reply(
    *,
    user_text: str,
    viewer_name: str | None,
    device_id: str,
    last_turn: dict[str, Any] | None,
) -> tuple[str, str]:
    """Generate a corrected assistant reply; returns (reply, reply_path)."""
    from llm_service import answer_voice_query, DEFAULT_MODEL, resolve_ollama_api_url

    prev_user = str((last_turn or {}).get("user_text") or "").strip()
    prev_assistant = str((last_turn or {}).get("assistant_text") or "").strip()
    hint = extract_rework_hint(user_text)
    name = (viewer_name or "the user").strip()

    prompt = (
        f"The user ({name}) said your previous answer was wrong or incorrect.\n"
        f"Their correction: {user_text.strip()}\n"
    )
    if hint:
        prompt += f"Clarification: {hint}\n"
    if prev_user:
        prompt += f"Original question: {prev_user}\n"
    if prev_assistant:
        prompt += f"Your previous (rejected) answer: {prev_assistant}\n"
    prompt += (
        "Acknowledge the mistake briefly, then give a corrected helpful answer. "
        "Do not repeat the wrong answer. Keep it conversational and under 3 sentences."
    )

    try:
        reply = answer_voice_query(
            prompt,
            model=DEFAULT_MODEL,
            api_url=resolve_ollama_api_url(),
            temperature=0.55,
        )
        cleaned = (reply or "").strip()
        if cleaned:
            return cleaned, "conversation_correction"
    except Exception:
        logger.exception("Conversation correction LLM failed device=%s", device_id)

    if prev_user:
        return (
            f"Sorry about that. Let me try again — you asked: {prev_user[:120]}",
            "conversation_correction_fallback",
        )
    return (
        "Sorry, I got that wrong. What would you like me to correct?",
        "conversation_correction_fallback",
    )

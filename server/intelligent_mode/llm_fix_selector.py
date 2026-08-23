"""LLM-assisted recovery action selection from a whitelisted action set."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any

from intelligent_mode.experience_playbook import error_pattern
from intelligent_mode.fix_history import fix_stats_for_prompt, similar_incidents_summary
from intelligent_mode.incidents import Incident
from intelligent_mode.recovery import RECOVERY_FIX_ACTIONS, recovery_chain_for

logger = logging.getLogger(__name__)

_CONFIDENCE_RANK = {"low": 0, "medium": 1, "high": 2}


@dataclass(frozen=True)
class FixSelection:
    action: str | None
    confidence: str
    reasoning: str
    source: str = "llm"

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "confidence": self.confidence,
            "reasoning": self.reasoning,
            "source": self.source,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> FixSelection:
        return cls(
            action=str(raw.get("action") or "").strip() or None,
            confidence=str(raw.get("confidence") or "low").strip().lower(),
            reasoning=str(raw.get("reasoning") or "").strip(),
            source=str(raw.get("source") or "llm"),
        )


def confidence_meets(actual: str, minimum: str) -> bool:
    actual_rank = _CONFIDENCE_RANK.get(str(actual or "").lower(), 0)
    minimum_rank = _CONFIDENCE_RANK.get(str(minimum or "medium").lower(), 1)
    return actual_rank >= minimum_rank


def _allowed_actions_for_incident(incident: Incident, tried: set[str]) -> list[str]:
    chain = recovery_chain_for(incident.subsystem)
    candidates = [action for action in chain if action not in tried]
    if not candidates:
        candidates = [
            action
            for action in sorted(RECOVERY_FIX_ACTIONS)
            if action not in tried and action != "none"
        ]
    return [action for action in candidates if action in RECOVERY_FIX_ACTIONS]


def _parse_llm_json(text: str) -> dict[str, Any] | None:
    text = text.strip()
    if not text:
        return None
    try:
        payload = json.loads(text)
        return payload if isinstance(payload, dict) else None
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{[^{}]*\}", text, flags=re.DOTALL)
    if not match:
        return None
    try:
        payload = json.loads(match.group(0))
        return payload if isinstance(payload, dict) else None
    except json.JSONDecodeError:
        return None


def select_fix_action(
    incident: Incident,
    snapshot: dict[str, Any],
    *,
    tried_actions: set[str],
    min_confidence: str = "medium",
    allowed_actions: list[str] | None = None,
    verified_only: bool = False,
) -> FixSelection | None:
    """Ask the LLM to pick the next whitelisted recovery action."""
    allowed = allowed_actions if allowed_actions is not None else _allowed_actions_for_incident(
        incident, tried_actions
    )
    if not allowed:
        return None

    try:
        from llm_service import ollama_generate, ollama_is_reachable

        if not ollama_is_reachable():
            return None
    except Exception:
        return None

    stats = fix_stats_for_prompt(
        incident.subsystem,
        verified_only=verified_only,
        pattern=error_pattern(str(incident.error or ""), str(incident.subsystem or "")),
    )
    similar = similar_incidents_summary(incident)
    evidence: list[str] = []
    debug = incident.debug_report if isinstance(incident.debug_report, dict) else {}
    if debug.get("root_cause"):
        evidence.append(f"root_cause={debug.get('root_cause')}")
    if debug.get("category"):
        evidence.append(f"category={debug.get('category')}")
    for fix in (incident.fixes or [])[-3:]:
        evidence.append(
            f"prior_fix {fix.action}: {'ok' if fix.success else 'fail'} — {str(fix.detail or '')[:80]}"
        )

    llm = snapshot.get("llm") or {}
    stt = snapshot.get("stt") or {}
    memory = snapshot.get("memory") or {}

    prompt = (
        "You are NiNO Intelligent Mode. Choose ONE next recovery action from ALLOWED_ACTIONS only.\n"
        "Respond with JSON only, no markdown:\n"
        '{"action":"<one of ALLOWED_ACTIONS or null>","confidence":"high|medium|low",'
        '"reasoning":"one short sentence"}\n\n'
        "Rules:\n"
        "- action MUST be exactly one value from ALLOWED_ACTIONS, or null if unsure\n"
        "- use historical success rates when available\n"
        "- if evidence suggests a code bug or idle camera (503 with no session), prefer null\n"
        "- confidence low if ambiguous\n\n"
        f"ALLOWED_ACTIONS: {json.dumps(allowed)}\n"
        f"Subsystem: {incident.subsystem}\n"
        f"Device: {incident.display_name} ({incident.device_id})\n"
        f"Error: {incident.error}\n"
        f"Status: {incident.status}\n"
        f"Evidence: {'; '.join(evidence[:8])}\n"
        f"Historical fix stats: {json.dumps(stats[:6])}\n"
        f"Similar incidents: {json.dumps(similar[:3])}\n"
        f"Snapshot: ollama_reachable={llm.get('reachable') if isinstance(llm, dict) else None}, "
        f"stt_loaded={stt.get('loaded') if isinstance(stt, dict) else None}, "
        f"memory_ready={memory.get('ready') if isinstance(memory, dict) else None}\n"
    )

    try:
        from llm_service import ollama_generate

        raw = ollama_generate(prompt, num_predict=180, temperature=0.1, timeout_s=35).strip()
    except Exception as exc:
        logger.debug("LLM fix selection skipped: %s", exc)
        return None

    payload = _parse_llm_json(raw)
    if not payload:
        return None

    action = str(payload.get("action") or "").strip() or None
    confidence = str(payload.get("confidence") or "low").strip().lower()
    reasoning = str(payload.get("reasoning") or "").strip()

    if action == "null":
        action = None
    if action and action not in allowed:
        logger.info("LLM fix selection rejected unknown action %s", action)
        return FixSelection(action=None, confidence="low", reasoning=f"Rejected non-whitelisted action: {action}")

    if action and not confidence_meets(confidence, min_confidence):
        return FixSelection(
            action=None,
            confidence=confidence,
            reasoning=reasoning or f"Confidence {confidence} below minimum {min_confidence}",
        )

    return FixSelection(action=action, confidence=confidence, reasoning=reasoning)

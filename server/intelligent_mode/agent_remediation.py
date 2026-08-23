"""Incidents Intelligent Mode fixes on its own — no developer escalation."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from intelligent_mode.incidents import Incident
from intelligent_mode.soak_test import parse_soak_unexpected_reply, soak_reply_would_pass
from intelligent_mode.voice_incident_filters import (
    is_ollama_cpu_port_error,
    is_soak_live_session_skip,
)


@dataclass(frozen=True)
class AgentRemediation:
    """How Intelligent Mode should handle an incident without developer involvement."""

    pattern_id: str
    summary: str
    fixable_by_agent: bool = True
    auto_resolve_reason: str = ""
    recovery_actions: tuple[str, ...] = ()
    skip_code_bug_email: bool = True


def _wav_too_large_error(error: str) -> bool:
    lowered = error.lower()
    return "wav too large" in lowered or (
        "play_wav failed" in lowered and "too large" in lowered
    )


def _stt_empty_error(error: str) -> bool:
    lowered = error.lower()
    return any(
        token in lowered
        for token in (
            "no speech",
            "stt empty",
            "stt_empty",
            "stt path=stt_empty",
            "stt path=stt_silent",
            "stt path=stt_rejected",
            "wake_reject",
        )
    )


def _unexpected_soak_reply(error: str) -> tuple[str, str] | None:
    parsed = parse_soak_unexpected_reply(error)
    if parsed is None:
        return None
    path, reply = parsed
    if soak_reply_would_pass(path=path, reply=reply):
        return path, reply
    # LLM/alarm paths: substantive reply is acceptable even when keywords differ.
    route = path.strip().lower()
    text = reply.strip()
    if route in {"llm", "alarm", "joke", "greeting", "smalltalk"} and len(text) >= 8:
        if not any(p in text.lower() for p in ("could not reach", "language model", "try again")):
            return path, reply
    return None


def classify_agent_remediation(incident: Incident) -> AgentRemediation | None:
    """Return remediation plan when the agent should handle this incident."""
    error = str(incident.error or "")
    subsystem = str(incident.subsystem or "").lower()

    if subsystem == "voice" and is_soak_live_session_skip(error):
        return AgentRemediation(
            pattern_id="soak_live_session_skip",
            summary="Soak voice test skipped because a live voice session was active — not a failure.",
            auto_resolve_reason=(
                "Auto-resolved: soak test deferred while the bot had an active voice session."
            ),
            recovery_actions=(),
        )

    if is_ollama_cpu_port_error(error):
        return AgentRemediation(
            pattern_id="ollama_cpu_optional",
            summary=(
                "CPU Ollama on port 11434 is unreachable; GPU Ollama is the configured primary."
            ),
            auto_resolve_reason=(
                "Auto-resolved: GPU Ollama is healthy; CPU port 11434 is an optional fallback only."
            ),
            recovery_actions=(),
        )

    if _wav_too_large_error(error):
        return AgentRemediation(
            pattern_id="wav_auto_split",
            summary="Long TTS audio is auto-split before ESP playback.",
            auto_resolve_reason=(
                "Auto-resolved: Intelligent Mode splits long WAV replies for the ESP speaker."
            ),
            recovery_actions=("voice_pipeline_recovery",),
        )

    soak_ok = _unexpected_soak_reply(error)
    if soak_ok is not None:
        path, reply = soak_ok
        return AgentRemediation(
            pattern_id="soak_valid_reply",
            summary="Soak test flagged a valid voice reply — no developer fix needed.",
            auto_resolve_reason=(
                "Auto-resolved: voice routing succeeded and the reply was acceptable "
                f"(path={path}, reply={reply[:80]})."
            ),
            recovery_actions=(),
        )

    if subsystem == "voice" and _stt_empty_error(error):
        return AgentRemediation(
            pattern_id="voice_stt_recovery",
            summary="STT returned empty — Intelligent Mode resets voice pipeline and reloads models.",
            recovery_actions=(
                "voice_pipeline_recovery",
                "whisper_reload_cuda",
                "whisper_reload_cpu",
                "voice_state_reset",
            ),
        )

    if subsystem == "voice" and "unexpected reply" in error.lower():
        return AgentRemediation(
            pattern_id="soak_reply_recovery",
            summary="Soak reply wording differed — Intelligent Mode retries voice pipeline recovery.",
            recovery_actions=("voice_pipeline_recovery", "voice_state_reset"),
        )

    return None


def is_agent_remediatable_incident(incident: Incident) -> bool:
    return classify_agent_remediation(incident) is not None


def auto_resolve_reason(incident: Incident) -> str | None:
    plan = classify_agent_remediation(incident)
    if plan is None:
        return None
    reason = str(plan.auto_resolve_reason or "").strip()
    return reason or None


def preferred_recovery_actions(incident: Incident) -> tuple[str, ...]:
    plan = classify_agent_remediation(incident)
    if plan is None:
        return ()
    actions = plan.recovery_actions
    try:
        from intelligent_mode.config import load_config
        from intelligent_mode.experience_playbook import order_actions_by_experience
        from intelligent_mode.recovery import tried_actions

        cfg = load_config()
        if cfg.experience_playbook_enabled or cfg.fix_history_enabled:
            return order_actions_by_experience(
                actions,
                incident,
                exclude=tried_actions(incident),
                min_samples=cfg.min_fix_history_samples,
                use_playbook=cfg.experience_playbook_enabled,
                use_fix_history=cfg.fix_history_enabled,
                verified_only=cfg.learn_from_verification,
            )
    except Exception:
        pass
    return actions


def merge_remediation_into_debug(
    debug: dict[str, Any],
    incident: Incident,
) -> dict[str, Any]:
    plan = classify_agent_remediation(incident)
    if plan is None:
        return debug
    out = dict(debug)
    out.pop("code_bug", None)
    out["category"] = "operational"
    out["fixable_by_agent"] = True
    out["agent_remediation"] = {
        "pattern_id": plan.pattern_id,
        "summary": plan.summary,
        "recovery_actions": list(plan.recovery_actions),
    }
    if plan.summary and not out.get("root_cause"):
        out["root_cause"] = plan.summary
    steps = list(out.get("suggested_actions") or [])
    if plan.recovery_actions:
        steps.insert(
            0,
            f"Intelligent Mode will try: {', '.join(plan.recovery_actions[:3])}",
        )
    out["suggested_actions"] = steps[:8]
    return out

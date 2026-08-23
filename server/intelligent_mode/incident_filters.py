"""Central job rules: when to open incidents and when to email humans."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from intelligent_mode.incidents import Incident
from intelligent_mode.voice_incident_filters import (
    is_soak_live_session_skip,
    should_suppress_voice_incident,
)

if TYPE_CHECKING:
    from intelligent_mode.detectors import DetectionCandidate

# Agent remediation patterns that are handled silently (log + ops UI only).
_SILENT_EMAIL_PATTERN_IDS = frozenset(
    {
        "soak_live_session_skip",
        "ollama_cpu_optional",
        "soak_valid_reply",
    }
)

# Benign patterns that should never be opened as incidents (log at debug only).
_NO_OPEN_PATTERN_IDS = _SILENT_EMAIL_PATTERN_IDS


def is_test_skip_message(message: str) -> bool:
    """True when a smoke/e2e/soak result was intentionally skipped (not a failure)."""
    lowered = str(message or "").lower().strip()
    if not lowered:
        return False
    if lowered.startswith("skipped"):
        return True
    if " skipped" in lowered or "— skipped" in lowered or "- skipped" in lowered:
        return True
    if ":skipped" in lowered or lowered.endswith(":skipped"):
        return True
    if "unreachable —" in lowered and "skipped" in lowered:
        return True
    return False


def should_suppress_incident(
    error: str,
    subsystem: str,
    snapshot: dict | None = None,
    *,
    device_id: str | None = None,
) -> bool:
    """Do not open an incident — benign test skip or known false positive."""
    raw = str(error or "").strip()
    if not raw:
        return False
    if should_suppress_voice_incident(raw, subsystem, snapshot, device_id=device_id):
        return True
    if is_test_skip_message(raw):
        return True
    if is_soak_live_session_skip(raw):
        return True
    lowered = raw.lower()
    if lowered.startswith("[smoke:") and is_test_skip_message(raw.split("]", 1)[-1]):
        return True
    if lowered.startswith("[soak:") and is_test_skip_message(raw.split("]", 1)[-1]):
        return True
    if lowered.startswith("[e2e:") and is_test_skip_message(raw.split("]", 1)[-1]):
        return True
    return False


def silent_email_pattern_id(incident: Incident) -> str | None:
    """Return agent pattern id if this incident should not trigger resolve email."""
    try:
        from intelligent_mode.agent_remediation import classify_agent_remediation

        plan = classify_agent_remediation(incident)
        if plan is not None and plan.pattern_id in _SILENT_EMAIL_PATTERN_IDS:
            return plan.pattern_id
    except Exception:
        pass
    return None


def should_email_incident(incident: Incident) -> bool:
    """False for benign auto-resolved patterns (ops dashboard still shows them)."""
    if silent_email_pattern_id(incident) is not None:
        return False
    try:
        from intelligent_mode.agent_remediation import classify_agent_remediation
        from intelligent_mode.voice_incident_filters import is_stt_empty_error

        plan = classify_agent_remediation(incident)
        if (
            plan is not None
            and plan.pattern_id == "voice_stt_recovery"
            and str(incident.status or "").lower() == "resolved"
            and not any(
                fix.success and fix.action and fix.action != "none"
                for fix in incident.fixes
            )
        ):
            return False
        if (
            str(incident.status or "").lower() == "resolved"
            and is_stt_empty_error(incident.error)
            and not any(
                fix.success and fix.action and fix.action != "none"
                for fix in incident.fixes
            )
        ):
            return False
    except Exception:
        pass
    return True


def is_benign_resolved_incident(incident: Incident) -> bool:
    """True for resolved noise incidents safe to prune from the active store."""
    if str(incident.status or "").lower() != "resolved":
        return False
    if should_suppress_incident(
        incident.error,
        incident.subsystem,
        incident.after_snapshot or incident.before_snapshot or None,
    ):
        return True
    if silent_email_pattern_id(incident) is not None:
        return True
    return False


def candidate_signature(candidate: DetectionCandidate) -> str:
    return Incident.make_signature(
        candidate.device_id,
        candidate.subsystem,
        candidate.error,
    )


def dedupe_detection_candidates(
    candidates: list[DetectionCandidate],
    *,
    open_signatures: set[str] | None = None,
) -> list[DetectionCandidate]:
    """Drop duplicate candidates and redundant smoke/e2e LLM duplicates in one tick."""
    open_signatures = open_signatures or set()
    seen: set[str] = set()
    has_live_llm = False
    out: list[DetectionCandidate] = []

    for candidate in candidates:
        sig = candidate_signature(candidate)
        if sig in seen or sig in open_signatures:
            continue
        err_lower = str(candidate.error or "").lower()
        is_test_derived = err_lower.startswith("[smoke:") or err_lower.startswith(
            "[e2e:"
        )
        if str(candidate.subsystem or "").lower() == "llm":
            if has_live_llm and is_test_derived:
                continue
            if not is_test_derived:
                has_live_llm = True
        seen.add(sig)
        out.append(candidate)
    return out


def should_open_incident(
    candidate: DetectionCandidate,
    snapshot: dict[str, Any] | None,
    *,
    open_signatures: set[str] | None = None,
) -> bool:
    """False when detection should not create or update an incident record."""
    if should_suppress_incident(
        candidate.error, candidate.subsystem, snapshot, device_id=candidate.device_id
    ):
        return False
    sig = candidate_signature(candidate)
    if open_signatures and sig in open_signatures:
        return False
    try:
        from intelligent_mode.agent_remediation import classify_agent_remediation

        preview = Incident(
            device_id=candidate.device_id,
            display_name=candidate.display_name,
            subsystem=candidate.subsystem,
            severity=candidate.severity,
            tier=candidate.tier,
            error=candidate.error,
            signature=sig,
        )
        plan = classify_agent_remediation(preview)
        if (
            plan is not None
            and not plan.recovery_actions
            and plan.pattern_id in _NO_OPEN_PATTERN_IDS
        ):
            return False
    except Exception:
        pass
    return True

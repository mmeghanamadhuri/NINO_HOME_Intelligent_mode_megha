"""UI-facing incident classification for the ops dashboard."""

from __future__ import annotations

from typing import Any

from intelligent_mode.code_bug_analyzer import is_code_bug_incident
from intelligent_mode.incidents import Incident
from intelligent_mode.soak_test import (
    parse_soak_unexpected_reply,
    soak_reply_would_pass,
    stale_soak_incident_resolution,
)


def _incident_from_dict(raw: dict[str, Any]) -> Incident:
    return Incident.from_dict(raw)


def _agent_remediation_meta(raw: dict[str, Any]) -> dict[str, Any] | None:
    try:
        from intelligent_mode.agent_remediation import classify_agent_remediation

        plan = classify_agent_remediation(_incident_from_dict(raw))
        if plan is None:
            return None
        return {
            "pattern_id": plan.pattern_id,
            "summary": plan.summary,
            "recovery_actions": list(plan.recovery_actions),
            "auto_resolve_reason": plan.auto_resolve_reason,
        }
    except Exception:
        return None


def classify_incident_for_ui(raw: dict[str, Any]) -> dict[str, Any]:
    """Return UI metadata: issue_kind, plain_english, fixable flags, developer visibility."""
    error = str(raw.get("error") or "")
    error_lower = error.lower()
    status = str(raw.get("status") or "").lower()
    debug = raw.get("debug_report") if isinstance(raw.get("debug_report"), dict) else {}
    code_bug = debug.get("code_bug") if isinstance(debug.get("code_bug"), dict) else {}
    agent_plan = _agent_remediation_meta(raw)

    parsed = parse_soak_unexpected_reply(error)
    stale_reason = stale_soak_incident_resolution(error)

    meta: dict[str, Any] = {
        "issue_kind": "operational",
        "queue": "agent_working",
        "plain_english": "",
        "fixable_by_intelligent_mode": bool(debug.get("fixable_by_agent", True)),
        "show_developer_issue": False,
        "auto_resolves_on_tick": False,
        "handled_by_agent": False,
    }

    if agent_plan:
        meta["agent_remediation"] = agent_plan
        meta["handled_by_agent"] = True
        meta["fixable_by_intelligent_mode"] = True
        meta["show_developer_issue"] = False
        meta["plain_english"] = str(agent_plan.get("summary") or "")
        pattern = str(agent_plan.get("pattern_id") or "")
        parsed = parse_soak_unexpected_reply(error)
        if parsed is not None:
            meta["soak_reply_path"], meta["soak_reply_text"] = parsed
        if pattern in {"soak_valid_reply", "wav_auto_split"}:
            meta["issue_kind"] = "agent_auto_fixed"
            meta["queue"] = "agent_resolved" if status == "resolved" else "agent_working"
            meta["auto_resolves_on_tick"] = not bool(agent_plan.get("recovery_actions"))
            if pattern == "soak_valid_reply":
                meta["plain_english"] = (
                    "The bot gave a valid voice reply. The soak test expected different "
                    "keywords — Intelligent Mode handles this automatically."
                )
            elif pattern == "wav_auto_split":
                meta["plain_english"] = (
                    "The spoken reply was long. Intelligent Mode splits audio into smaller "
                    "clips before sending to the robot speaker — no developer action needed."
                )
            return meta
        if pattern == "voice_stt_recovery":
            meta["issue_kind"] = "agent_handling"
            meta["queue"] = "agent_working"
            meta["plain_english"] = (
                "Speech-to-text returned empty. Intelligent Mode is resetting the voice "
                "pipeline and reloading models."
            )
            return meta
        if pattern == "soak_reply_recovery":
            meta["issue_kind"] = "agent_handling"
            meta["queue"] = "agent_working"
            meta["plain_english"] = (
                "Soak test wording differed from expectations. Intelligent Mode is "
                "retrying the voice pipeline."
            )
            return meta

    if parsed is not None:
        path, reply = parsed
        meta["soak_reply_path"] = path
        meta["soak_reply_text"] = reply
        if soak_reply_would_pass(path=path, reply=reply) or stale_reason:
            meta.update(
                {
                    "issue_kind": "agent_auto_fixed",
                    "queue": "agent_resolved" if status == "resolved" else "agent_working",
                    "plain_english": (
                        "The bot gave a valid voice reply. The soak test expected different "
                        "keywords, so this is a test false alarm — not a broken microphone or STT bug."
                    ),
                    "fixable_by_intelligent_mode": True,
                    "show_developer_issue": False,
                    "handled_by_agent": True,
                    "auto_resolves_on_tick": True,
                }
            )
            return meta
        meta["plain_english"] = (
            f"Soak test rejected the bot's {path} reply. Intelligent Mode will retry or "
            "review whether the test expectations need updating."
        )
        meta["queue"] = "agent_working"

    if stale_reason:
        meta.update(
            {
                "issue_kind": "agent_auto_fixed",
                "queue": "agent_resolved" if status == "resolved" else "agent_working",
                "plain_english": stale_reason,
                "fixable_by_intelligent_mode": True,
                "handled_by_agent": True,
                "auto_resolves_on_tick": True,
            }
        )
        return meta

    try:
        is_code = is_code_bug_incident(_incident_from_dict(raw))
    except Exception:
        is_code = bool(code_bug.get("is_code_bug"))

    if is_code or code_bug.get("is_code_bug"):
        meta.update(
            {
                "issue_kind": "developer_required",
                "queue": "developer",
                "plain_english": str(
                    code_bug.get("bug_summary")
                    or debug.get("root_cause")
                    or "A software change is needed — restarts alone will not fix this."
                ),
                "fixable_by_intelligent_mode": False,
                "show_developer_issue": True,
                "handled_by_agent": False,
            }
        )
        return meta

    category = str(debug.get("category") or "").lower()
    if category == "operational":
        meta["plain_english"] = str(
            debug.get("root_cause") or "Transient ops issue — Intelligent Mode can retry safe fixes."
        )
        meta["fixable_by_intelligent_mode"] = bool(debug.get("fixable_by_agent", True))
        meta["handled_by_agent"] = bool(debug.get("fixable_by_agent", True))
        meta["queue"] = "agent_working" if status in {"open", "fixing"} else meta["queue"]
    elif category in {"logic_bug", "regression"}:
        meta.update(
            {
                "issue_kind": "developer_required",
                "queue": "developer" if not debug.get("fixable_by_agent") else "agent_working",
                "plain_english": str(
                    debug.get("root_cause") or "Possible logic bug — review logs and recent changes."
                ),
                "show_developer_issue": not bool(debug.get("fixable_by_agent", False)),
                "fixable_by_intelligent_mode": bool(debug.get("fixable_by_agent", False)),
            }
        )
    else:
        meta["plain_english"] = str(debug.get("root_cause") or "")

    if status == "resolved" and meta.get("fixable_by_intelligent_mode"):
        meta["queue"] = "agent_resolved"
        meta["handled_by_agent"] = True
    elif status == "escalated" and meta.get("show_developer_issue"):
        meta["queue"] = "developer"
    elif status in {"open", "fixing"}:
        meta["queue"] = "agent_working"

    return meta


def enrich_incident_for_ui(raw: dict[str, Any]) -> dict[str, Any]:
    out = dict(raw)
    out["ui"] = classify_incident_for_ui(raw)
    return out

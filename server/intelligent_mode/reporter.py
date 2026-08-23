"""Human-readable incident reports for email and ops alerts."""

from __future__ import annotations

import html
import logging
import re
from typing import Any

from intelligent_mode.incidents import FixAttempt, Incident

logger = logging.getLogger(__name__)

_SUBSYSTEM_LABELS = {
    "camera": "Camera",
    "bot": "Robot connectivity",
    "llm": "AI language model",
    "memory": "Conversation memory",
    "stt": "Speech-to-text",
    "tts": "Text-to-speech",
    "voice": "Voice assistant",
    "discovery": "Network discovery",
    "server": "Server",
}

_STATUS_LABELS = {
    "open": "Needs attention",
    "fixing": "Intelligent Mode fix in progress",
    "resolved": "Resolved",
    "escalated": "Needs manual help",
    "code_bug": "Code fix required",
    "agent_fixed": "Fixed automatically by Intelligent Mode",
    "agent_fixing": "Intelligent Mode is fixing this",
}

_ACTION_LABELS = {
    "camera_restart": "Restart camera stream",
    "lan_discovery": "Re-scan the local network",
    "ollama_restart_warm": "Restart AI language model",
    "ollama_cpu_fallback": "Switch AI model to CPU fallback",
    "memory_reconnect": "Reconnect conversation memory",
    "postgres_start": "Start PostgreSQL memory database",
    "whisper_preload": "Reload speech-to-text model",
    "whisper_reload_cuda": "Reload speech-to-text on GPU",
    "whisper_reload_cpu": "Reload speech-to-text on CPU",
    "voice_state_reset": "Reset voice session",
    "voice_pipeline_recovery": "Reset full voice pipeline (AI + speech + TTS)",
    "piper_model_download": "Download speech voice model",
    "piper_reload_cpu": "Reload text-to-speech on CPU",
    "none": "No Intelligent Mode action",
}

_AGENT_PATTERN_EMAIL: dict[str, dict[str, str]] = {
    "wav_auto_split": {
        "title": "Long voice reply — handled automatically",
        "what_happened": (
            "NiNO tried to speak a long answer, but the audio file was bigger than the "
            "robot speaker accepts (~380 KB)."
        ),
        "what_agent_does": (
            "Intelligent Mode now splits long audio into smaller clips before sending them "
            "to the robot speaker, so the full answer still plays."
        ),
        "your_action": "No action needed. This is handled by Intelligent Mode automatically.",
    },
    "voice_stt_recovery": {
        "title": "Speech not recognized — Intelligent Mode is recovering",
        "what_happened": (
            "The robot received audio, but speech-to-text returned empty text "
            "(no words were recognized). This is flagged only after repeated "
            "failures in a short window — a single silent clip is normal."
        ),
        "what_agent_does": (
            "Intelligent Mode resets the voice pipeline, reloads the speech-to-text model "
            "(GPU then CPU if needed), and clears any stuck voice session state. "
            "The incident stays open until recovery runs and STT succeeds again."
        ),
        "your_action": (
            "If this repeats during real conversations, check the robot mic, speak clearly "
            "after wake, and reduce background noise. Soak-test silence is usually harmless."
        ),
    },
    "soak_valid_reply": {
        "title": "Soak test false alarm — reply was actually fine",
        "what_happened": (
            "An automated soak test flagged a voice reply as unexpected, but the bot "
            "answered correctly — only the exact wording differed from the test keywords."
        ),
        "what_agent_does": (
            "Intelligent Mode reviewed the reply and confirmed it was acceptable. "
            "No developer or manual fix is required."
        ),
        "your_action": "No action needed.",
    },
    "soak_reply_recovery": {
        "title": "Voice reply wording differed — Intelligent Mode is retrying",
        "what_happened": (
            "An automated soak test expected specific keywords in the voice reply, "
            "but the bot gave a different (possibly still valid) answer."
        ),
        "what_agent_does": (
            "Intelligent Mode resets the voice pipeline and re-checks the voice stack."
        ),
        "your_action": "No action needed unless this alert repeats every cycle.",
    },
}

_SEVERITY_COLORS = {
    "critical": ("#a00020", "#fdecee"),
    "warning": ("#b36b00", "#fff6e8"),
    "resolved": ("#1b7f3a", "#e8f7ec"),
    "escalated": ("#005ea8", "#e8f2fb"),
}


def _friendly_subsystem(name: str) -> str:
    key = str(name or "").strip().lower()
    return _SUBSYSTEM_LABELS.get(key, key.replace("_", " ").title() or "Unknown")


def _get_agent_plan(incident: Incident):
    try:
        from intelligent_mode.agent_remediation import classify_agent_remediation

        return classify_agent_remediation(incident)
    except Exception:
        return None


def _had_recovery_fix(incident: Incident) -> bool:
    return any(
        fix.success and fix.action and fix.action != "none" for fix in incident.fixes
    )


def _is_agent_handled(incident: Incident) -> bool:
    plan = _get_agent_plan(incident)
    if plan is not None:
        return True
    debug = incident.debug_report if isinstance(incident.debug_report, dict) else {}
    remediation = debug.get("agent_remediation")
    return isinstance(remediation, dict) and bool(remediation.get("pattern_id"))


def _agent_pattern_id(incident: Incident) -> str:
    plan = _get_agent_plan(incident)
    if plan is not None:
        return plan.pattern_id
    debug = incident.debug_report if isinstance(incident.debug_report, dict) else {}
    remediation = debug.get("agent_remediation") if isinstance(debug.get("agent_remediation"), dict) else {}
    return str(remediation.get("pattern_id") or "")


def _friendly_status(status: str, *, incident: Incident | None = None) -> str:
    key = str(status or "").strip().lower()
    if incident is not None and _is_agent_handled(incident):
        if key == "resolved":
            return _STATUS_LABELS["agent_fixed"]
        if key in {"open", "fixing"}:
            return _STATUS_LABELS["agent_fixing"]
    if incident is not None:
        try:
            from intelligent_mode.code_bug_analyzer import is_code_bug_incident

            if is_code_bug_incident(incident) and key in {"escalated", "open", "fixing"}:
                return _STATUS_LABELS["code_bug"]
        except Exception:
            pass
    return _STATUS_LABELS.get(key, key.replace("_", " ").title() or "Unknown")


def _friendly_action(action: str) -> str:
    key = str(action or "").strip().lower()
    return _ACTION_LABELS.get(key, key.replace("_", " ").title() or "Intelligent Mode fix")


def _short_device_id(device_id: str) -> str:
    raw = str(device_id or "").strip()
    if not raw:
        return "unknown"
    compact = raw.replace(":", "").replace("-", "").lower()
    if len(compact) > 12:
        return compact[:12]
    return compact


def _bot_label(incident: Incident) -> str:
    display = str(incident.display_name or "").strip()
    device_id = str(incident.device_id or "").strip()
    if device_id == "server":
        return "NiNO Server"
    mac_like = re.fullmatch(r"([0-9a-f]{2}:){5}[0-9a-f]{2}", display, re.I)
    id_like = display.lower().replace(":", "") == device_id.lower().replace(":", "")
    if display and not mac_like and not id_like:
        return display
    short = _short_device_id(device_id)
    return f"Robot {short}"


def _humanize_error(incident: Incident) -> str:
    error = str(incident.error or "").strip()
    lower = error.lower()
    subsystem = str(incident.subsystem or "").lower()
    debug = incident.debug_report if isinstance(incident.debug_report, dict) else {}

    pattern_id = _agent_pattern_id(incident)
    agent_copy = _AGENT_PATTERN_EMAIL.get(pattern_id)
    if agent_copy:
        return str(agent_copy.get("what_happened") or error)

    if "wav too large" in lower or ("play_wav failed" in lower and "too large" in lower):
        return (
            "NiNO tried to speak a long answer, but the audio was larger than the robot "
            "speaker accepts. Intelligent Mode splits long audio automatically."
        )

    if any(
        token in lower
        for token in ("stt empty", "stt_empty", "no speech", "stt path=stt_empty", "wake_reject")
    ):
        return (
            "The robot received audio but speech-to-text did not recognize any words. "
            "Intelligent Mode is resetting the voice pipeline."
        )

    if "unexpected reply" in lower:
        return (
            "An automated test expected specific words in the voice reply, but the bot "
            "answered differently. Intelligent Mode checks whether the reply was still acceptable."
        )

    if subsystem == "camera" and "503" in lower:
        root = str(debug.get("root_cause") or "").lower()
        if "idle" in root or "session-gated" in root or "no voice session" in root:
            return (
                "The camera is off because the robot is idle. "
                "This is expected until someone starts a voice conversation."
            )
        return (
            "The camera could not provide a snapshot. "
            "It may still be starting up or the USB camera may be busy."
        )

    if subsystem == "bot" and "503" in lower:
        return (
            "The robot responded but the camera snapshot was unavailable. "
            "This is normal when the robot is not in an active voice session."
        )

    if "connection refused" in lower:
        return "A required service is not running or cannot be reached on the network."
    if "unreachable" in lower:
        return "The robot or service could not be reached over the network."
    if "disconnected" in lower or "stale" in lower:
        return "The video stream stopped or is not updating."
    if "timeout" in lower:
        return "The request timed out before a response was received."
    if "not loaded" in lower:
        return "A required model or service has not finished loading yet."
    if error.startswith("[smoke:"):
        return "An Intelligent Mode health check failed: " + re.sub(r"^\[smoke:[^\]]+\]\s*", "", error)

    if error.lower().startswith("http"):
        return f"Network request failed ({error})."
    return error or "An unexpected problem was detected."


def _headline(incident: Incident) -> str:
    subsystem = _friendly_subsystem(incident.subsystem)
    bot = _bot_label(incident)
    status = str(incident.status or "").lower()
    if _is_agent_handled(incident):
        if status == "resolved":
            pattern = _agent_pattern_id(incident)
            if pattern == "voice_stt_recovery" and not _had_recovery_fix(incident):
                return f"{subsystem} — speech not recognized (recovered) on {bot}"
            if _had_recovery_fix(incident):
                return f"{subsystem} auto-fixed on {bot}"
            return f"{subsystem} handled automatically on {bot}"
        if status in {"open", "fixing"}:
            return f"{subsystem} — Intelligent Mode is fixing this on {bot}"
    try:
        from intelligent_mode.code_bug_analyzer import is_code_bug_incident

        if is_code_bug_incident(incident):
            return f"{subsystem} code bug on {bot} — developer fix required"
    except Exception:
        pass
    if status == "resolved":
        vr = incident.verification_report if isinstance(incident.verification_report, dict) else {}
        if vr.get("passed"):
            return f"{subsystem} verified fixed on {bot}"
        if vr.get("passed") is False:
            return f"{subsystem} fix not verified on {bot}"
        return f"{subsystem} restored on {bot}"
    if status == "escalated":
        return f"{subsystem} issue on {bot} needs manual help"
    return f"{subsystem} issue on {bot}"


def _format_fix_line(fix: FixAttempt | None) -> str:
    if fix is None:
        return "Intelligent Mode has not attempted a fix yet."
    action = _friendly_action(fix.action)
    result = "succeeded" if fix.success else "did not succeed"
    detail = str(fix.detail or "").strip()
    detail = re.sub(r"\[[0-9a-f:]+\]\s*", "", detail, flags=re.I)
    detail = re.sub(r"^restarted\s+https?://\S+;\s*", "", detail, flags=re.I)
    if detail:
        detail = detail[0].upper() + detail[1:]
        return f"{action} — {result}. {detail}"
    return f"{action} — {result}."


def _format_all_fixes(incident: Incident) -> list[str]:
    fixes = incident.fixes or []
    if not fixes:
        return ["Intelligent Mode has not attempted a fix yet."]
    lines: list[str] = []
    for index, fix in enumerate(fixes, start=1):
        line = _format_fix_line(fix)
        if len(fixes) > 1:
            lines.append(f"{index}. {line}")
        else:
            lines.append(line)
    return lines


def _plain_english_intro(incident: Incident) -> list[str]:
    """Top-of-email summary anyone can read first."""
    status = str(incident.status or "").lower()
    pattern_id = _agent_pattern_id(incident)
    agent_copy = _AGENT_PATTERN_EMAIL.get(pattern_id)

    lines = [
        "IN PLAIN ENGLISH (read this first)",
        "-" * 40,
        (
            "NiNO Intelligent Mode is an automated testing and recovery agent. It runs "
            "health checks and soak tests, detects problems, tries safe fixes, and emails you."
        ),
        "",
    ]

    if agent_copy:
        lines.extend(
            [
                f"Issue type: {agent_copy['title']}",
                "",
                f"What happened: {agent_copy['what_happened']}",
                "",
                f"What Intelligent Mode did: {agent_copy['what_agent_does']}",
                "",
                f"What you need to do: {agent_copy['your_action']}",
            ]
        )
        return lines

    if status == "resolved":
        lines.append("What happened: A problem was detected during automated testing or monitoring.")
        lines.append("")
        lines.append("What Intelligent Mode did: It applied a fix and confirmed the system recovered.")
        lines.append("")
        lines.append("What you need to do: No action needed.")
        return lines

    try:
        from intelligent_mode.code_bug_analyzer import is_code_bug_incident

        if is_code_bug_incident(incident):
            lines.extend(
                [
                    "Issue type: Software bug — needs a developer",
                    "",
                    f"What happened: {_humanize_error(incident)}",
                    "",
                    "What Intelligent Mode did: It detected the issue but cannot change application code.",
                    "",
                    "What you need to do: A developer must review the suggested fix in this email.",
                ]
            )
            return lines
    except Exception:
        pass

    lines.extend(
        [
            f"What happened: {_humanize_error(incident)}",
            "",
            "What Intelligent Mode did: "
            + (
                "It is attempting automated recovery now."
                if status in {"open", "fixing"}
                else "It could not fully recover automatically."
            ),
            "",
            "What you need to do: See WHAT TO DO NEXT below.",
        ]
    )
    return lines


def _agent_remediation_section(incident: Incident) -> list[str]:
    pattern_id = _agent_pattern_id(incident)
    if not pattern_id:
        return []
    agent_copy = _AGENT_PATTERN_EMAIL.get(pattern_id)
    plan = _get_agent_plan(incident)
    if not agent_copy and plan is None:
        return []

    bot = _bot_label(incident)
    lines = [
        "",
        "HANDLED BY INTELLIGENT MODE (no developer needed)",
        "-" * 40,
        f"Robot / system: {bot}",
        f"Pattern: {pattern_id.replace('_', ' ')}",
    ]
    if agent_copy:
        lines.extend(
            [
                "",
                agent_copy["title"],
                "",
                f"What happened: {agent_copy['what_happened']}",
                f"Agent action: {agent_copy['what_agent_does']}",
                f"Your action: {agent_copy['your_action']}",
            ]
        )
    if plan is not None and plan.recovery_actions:
        actions = ", ".join(_friendly_action(a) for a in plan.recovery_actions)
        lines.extend(["", f"Recovery steps used/planned: {actions}"])
    return lines


def _agent_remediation_html(incident: Incident) -> str:
    pattern_id = _agent_pattern_id(incident)
    agent_copy = _AGENT_PATTERN_EMAIL.get(pattern_id)
    if not agent_copy:
        return ""

    def _block(title: str, body: str) -> str:
        return f"""
              <div style="font-size:13px;font-weight:700;text-transform:uppercase;letter-spacing:0.06em;color:#1b7f3a;margin:18px 0 8px 0;">{html.escape(title)}</div>
              <p style="margin:0 0 12px 0;line-height:1.6;white-space:pre-wrap;">{html.escape(body)}</p>"""

    banner = """
              <div style="padding:12px 14px;background:#e8f7ec;border:1px solid #b8e0c4;border-radius:8px;margin:0 0 16px 0;">
                <div style="font-size:12px;color:#1b7f3a;text-transform:uppercase;letter-spacing:0.06em;font-weight:700;">Handled by Intelligent Mode</div>
                <div style="font-size:15px;font-weight:600;margin-top:6px;color:#145a32;">No developer action required</div>
              </div>"""

    parts = [
        banner,
        _block("Issue type", agent_copy["title"]),
        _block("What happened", agent_copy["what_happened"]),
        _block("What Intelligent Mode did", agent_copy["what_agent_does"]),
        _block("What you need to do", agent_copy["your_action"]),
    ]
    plan = _get_agent_plan(incident)
    if plan is not None and plan.recovery_actions:
        actions = ", ".join(_friendly_action(a) for a in plan.recovery_actions)
        parts.append(_block("Recovery steps", actions))
    return "".join(parts)


def _next_steps(incident: Incident) -> list[str]:
    debug = incident.debug_report if isinstance(incident.debug_report, dict) else {}
    if _is_agent_handled(incident):
        pattern_id = _agent_pattern_id(incident)
        copy = _AGENT_PATTERN_EMAIL.get(pattern_id, {})
        action = str(copy.get("your_action") or "").strip()
        if action:
            return [action]
        return ["No action needed. Intelligent Mode is handling this automatically."]

    steps = [str(item).strip() for item in (debug.get("suggested_actions") or []) if str(item).strip()]
    if steps:
        return steps[:5]

    status = str(incident.status or "").lower()
    if status == "resolved":
        return ["No action needed. The system reported this issue as resolved."]
    if status == "escalated":
        return [
            "Review the NiNO Ops Dashboard for live status.",
            "Check robot power, Wi‑Fi, and USB camera connections.",
            "Inspect server logs if the issue persists.",
        ]

    subsystem = str(incident.subsystem or "").lower()
    if subsystem == "camera":
        return [
            "Open the Ops Dashboard and confirm whether the robot is idle or in a voice session.",
            "If someone is talking to the robot, wait a few seconds and try again.",
            "If the problem continues, check the robot's USB camera connection.",
        ]
    if subsystem == "llm":
        return [
            "Confirm the AI language model service (Ollama) is running.",
            "Restart the NiNO server if needed.",
        ]
    return [
        "Open the NiNO Ops Dashboard for the latest status.",
        "If this alert repeats, check network connectivity and power to the robot.",
    ]


def _analysis_summary(incident: Incident) -> str:
    debug = incident.debug_report if isinstance(incident.debug_report, dict) else {}
    llm_analysis = str(debug.get("llm_analysis") or "").strip()
    root = str(debug.get("root_cause") or "").strip()
    if root and llm_analysis:
        return f"{root}\n\nAgent note: {llm_analysis}"
    if llm_analysis:
        return llm_analysis
    if root:
        return root
    return _humanize_error(incident)


def _fix_selection_summary(incident: Incident) -> str:
    debug = incident.debug_report if isinstance(incident.debug_report, dict) else {}
    selection = debug.get("fix_selection") if isinstance(debug.get("fix_selection"), dict) else {}
    if not selection:
        return ""
    action = str(selection.get("action") or "").strip()
    confidence = str(selection.get("confidence") or "").strip()
    reasoning = str(selection.get("reasoning") or "").strip()
    if not action and not reasoning:
        return ""
    parts = []
    if action:
        parts.append(f"Selected action: {_friendly_action(action)}")
    if confidence:
        parts.append(f"Confidence: {confidence}")
    if reasoning:
        parts.append(reasoning)
    return " · ".join(parts)


def _technical_details(incident: Incident) -> list[tuple[str, str]]:
    rows = [
        ("Incident ID", incident.incident_id),
        ("Device ID", incident.device_id),
        ("Severity", str(incident.severity or "unknown").title()),
        ("Raw error", str(incident.error or "—")),
    ]
    debug = incident.debug_report if isinstance(incident.debug_report, dict) else {}
    if debug.get("category"):
        rows.append(("Category", str(debug.get("category"))))
    if debug.get("confidence"):
        rows.append(("Confidence", str(debug.get("confidence"))))
    if "fixable_by_agent" in debug:
        rows.append(("Fixable by Intelligent Mode", "Yes" if debug.get("fixable_by_agent") else "No"))
    return rows


def email_subject(incident: Incident) -> str:
    status = str(incident.status or "open").lower()
    bot = _bot_label(incident)
    subsystem = _friendly_subsystem(incident.subsystem)
    debug = incident.debug_report if isinstance(incident.debug_report, dict) else {}
    code_bug = debug.get("code_bug") if isinstance(debug.get("code_bug"), dict) else {}
    if _is_agent_handled(incident):
        prefix = "Auto-fixed" if status == "resolved" else "Auto-fix in progress"
    elif code_bug.get("is_code_bug"):
        prefix = "Code bug"
    elif status == "resolved":
        prefix = "Resolved"
    elif status == "escalated":
        prefix = "Help needed"
    elif incident.severity == "critical":
        prefix = "Action needed"
    else:
        prefix = "Alert"

    subject = f"[NiNO] {prefix} — {subsystem} — {bot}"
    if len(subject) > 120:
        subject = subject[:117] + "..."
    return subject


def _plain_english_code_bug(code: dict, incident: Incident) -> str:
    bug_id = str(code.get("id") or "").strip()
    if bug_id == "wav_too_large_esp":
        return (
            "NiNO tried to speak a long answer, but the audio file was bigger than the robot "
            "speaker can accept (~380 KB). Intelligent Mode now splits long audio automatically — "
            "you should not see this as a developer task anymore."
        )
    if _is_agent_handled(incident):
        pattern_id = _agent_pattern_id(incident)
        agent_copy = _AGENT_PATTERN_EMAIL.get(pattern_id)
        if agent_copy:
            return (
                f"{agent_copy['what_happened']} {agent_copy['what_agent_does']} "
                f"{agent_copy['your_action']}"
            )
    summary = str(code.get("bug_summary") or incident.error or "").strip()
    if summary:
        return (
            f"This is a software bug, not a loose cable or Wi‑Fi glitch. {summary} "
            "Intelligent Mode cannot fix it with restarts — a code change on the server or robot is needed."
        )
    return (
        "This is a software bug that Intelligent Mode cannot fix by restarting services. "
        "A developer needs to change the code."
    )


def _code_bug_bot_lines(incident: Incident) -> tuple[str, str, str]:
    """Return (display label, device_id, section prefix for headers)."""
    bot = _bot_label(incident)
    device_id = str(incident.device_id or "unknown").strip() or "unknown"
    if device_id == "server":
        prefix = "NiNO Server (PC)"
    else:
        prefix = f"{bot} (ID: {device_id})"
    return bot, device_id, prefix


def _code_bug_section(incident: Incident) -> list[str]:
    debug = incident.debug_report if isinstance(incident.debug_report, dict) else {}
    code = debug.get("code_bug") if isinstance(debug.get("code_bug"), dict) else {}
    if not code.get("is_code_bug"):
        return []

    bot, device_id, bot_prefix = _code_bug_bot_lines(incident)

    lines = [
        "",
        "CODE BUG ANALYSIS",
        "-" * 40,
        f"Bot: {bot}",
        f"Device ID: {device_id}",
        "",
        "WHAT THIS MEANS (plain English)",
        _plain_english_code_bug(code, incident),
        "",
        f"THE PROBLEM — {bot_prefix}",
        str(code.get("bug_summary") or debug.get("root_cause") or incident.error or "Unknown"),
        "",
        f"LIKELY CAUSE — {bot_prefix}",
        str(code.get("likely_cause") or debug.get("root_cause") or "See server logs."),
        "",
        f"SUGGESTED FIX (code change) — {bot_prefix}",
        str(code.get("suggested_fix") or "Review affected files and server logs."),
    ]
    files = code.get("affected_files") or []
    if files:
        lines.extend(
            [
                "",
                f"AFFECTED FILES — {bot_prefix}",
                ", ".join(str(f) for f in files),
            ]
        )

    if code.get("firmware_update_recommended"):
        lines.extend(["", f"FIRMWARE UPDATE — {bot_prefix}"])
        fw = str(code.get("firmware_filename") or "").strip()
        if device_id != "server":
            lines.append(f"Target bot for OTA: {bot} ({device_id})")
        if fw:
            lines.append("1. Build firmware: idf.py build (in ESP-P4-UK-Demo)")
            lines.append(f"2. Copy build/*.bin to server/firmware/{fw}")
            if device_id != "server":
                lines.append(
                    f"3. OTA deploy: POST /api/ota/deploy/{device_id} "
                    f"or Ops Dashboard → OTA → {bot}"
                )
            else:
                lines.append("3. OTA deploy via Ops Dashboard when a target bot is known")
            lines.append(f"   Recommended file: {fw}")
        else:
            lines.append(
                f"Build firmware, upload .bin to server/firmware/, then OTA to {bot_prefix}."
            )
        ota_detail = str(code.get("ota_detail") or "").strip()
        if ota_detail:
            lines.append(f"OTA status ({bot}): {ota_detail}")

    return lines


def _code_bug_html(incident: Incident) -> str:
    debug = incident.debug_report if isinstance(incident.debug_report, dict) else {}
    code = debug.get("code_bug") if isinstance(debug.get("code_bug"), dict) else {}
    if not code.get("is_code_bug"):
        return ""

    bot, device_id, bot_prefix = _code_bug_bot_lines(incident)

    def _block(title: str, body: str) -> str:
        return f"""
              <div style="font-size:13px;font-weight:700;text-transform:uppercase;letter-spacing:0.06em;color:#667085;margin:18px 0 8px 0;">{html.escape(title)}</div>
              <p style="margin:0 0 12px 0;line-height:1.6;white-space:pre-wrap;">{html.escape(body)}</p>"""

    bot_banner = f"""
              <div style="padding:12px 14px;background:#fff6e8;border:1px solid #f0d9a8;border-radius:8px;margin:0 0 16px 0;">
                <div style="font-size:12px;color:#667085;text-transform:uppercase;letter-spacing:0.06em;">Affected bot</div>
                <div style="font-size:16px;font-weight:600;margin-top:4px;">{html.escape(bot)}</div>
                <div style="font-size:13px;color:#444;margin-top:6px;">Device ID: {html.escape(device_id)}</div>
              </div>"""

    parts = [
        bot_banner,
        _block("What this means (plain English)", _plain_english_code_bug(code, incident)),
        _block(f"The problem — {bot_prefix}", str(code.get("bug_summary") or "")),
    ]
    if code.get("likely_cause"):
        parts.append(_block(f"Likely cause — {bot_prefix}", str(code.get("likely_cause"))))
    if code.get("suggested_fix"):
        parts.append(
            _block(f"Suggested fix (code change) — {bot_prefix}", str(code.get("suggested_fix")))
        )
    files = code.get("affected_files") or []
    if files:
        parts.append(_block(f"Affected files — {bot_prefix}", ", ".join(str(f) for f in files)))
    if code.get("firmware_update_recommended"):
        fw = str(code.get("firmware_filename") or "latest .bin in server/firmware/")
        target = (
            f"Target bot: {bot} ({device_id})\n"
            if device_id != "server"
            else "Target: assign bot in Ops Dashboard before OTA\n"
        )
        fw_text = (
            f"{target}"
            f"Rebuild ESP-IDF firmware, copy the .bin to server/firmware/, then OTA.\n"
            f"Recommended: {fw}\n"
            f"{code.get('ota_detail') or ''}"
        ).strip()
        parts.append(_block(f"Firmware update — {bot_prefix}", fw_text))
    return "".join(parts)


def _build_plain_report(incident: Incident) -> str:
    bot = _bot_label(incident)
    status = _friendly_status(incident.status, incident=incident)
    fix_lines = _format_all_fixes(incident)
    steps = _next_steps(incident)

    lines = [
        "NiNO Intelligent Mode — Testing & Recovery Agent",
        "=" * 40,
        "",
        f"Summary: {_headline(incident)}",
        f"Status:   {status}",
        "",
    ]
    lines.extend(_plain_english_intro(incident))
    lines.extend(
        [
            "",
            "WHAT HAPPENED (detail)",
            "-" * 40,
            _humanize_error(incident),
            "",
            "WHERE",
            "-" * 40,
            f"Robot / system: {bot}",
            f"Affected area:  {_friendly_subsystem(incident.subsystem)}",
            "",
            "WHAT INTELLIGENT MODE TRIED",
            "-" * 40,
        ]
    )
    lines.extend(fix_lines)
    lines.extend(
        [
            "",
            "WHAT TO DO NEXT",
            "-" * 40,
        ]
    )
    lines.extend(f"{index}. {step}" for index, step in enumerate(steps, start=1))

    analysis = _analysis_summary(incident)
    if analysis and analysis != _humanize_error(incident):
        lines.extend(["", "ANALYSIS", "-" * 40, analysis])

    fix_selection = _fix_selection_summary(incident)
    if fix_selection:
        lines.extend(["", "RECOVERY DECISION", "-" * 40, fix_selection])

    lines.extend(_agent_remediation_section(incident))
    lines.extend(_code_bug_section(incident))

    lines.extend(["", "TECHNICAL DETAILS", "-" * 40])
    for label, value in _technical_details(incident):
        lines.append(f"{label}: {value}")

    lines.extend(
        [
            "",
            "—",
            "NiNO Intelligent Mode · automated testing, detect, fix, verify, and report",
            "View live status: Ops Dashboard on your NiNO server (/ops)",
        ]
    )
    return "\n".join(lines) + "\n"


def _badge_colors(incident: Incident) -> tuple[str, str]:
    status = str(incident.status or "").lower()
    if status == "resolved":
        return _SEVERITY_COLORS["resolved"]
    if status == "escalated":
        return _SEVERITY_COLORS["escalated"]
    if incident.severity == "critical":
        return _SEVERITY_COLORS["critical"]
    return _SEVERITY_COLORS["warning"]


def build_html_report(incident: Incident) -> str:
    bot = html.escape(_bot_label(incident))
    headline = html.escape(_headline(incident))
    status = html.escape(_friendly_status(incident.status, incident=incident))
    analysis_raw = _analysis_summary(incident)
    summary_raw = _humanize_error(incident)
    analysis = html.escape(analysis_raw)
    summary = html.escape(summary_raw)
    badge_fg, badge_bg = _badge_colors(incident)
    steps = _next_steps(incident)
    steps_html = "".join(
        f'<li style="margin:0 0 8px 0;">{html.escape(step)}</li>' for step in steps
    )
    technical_rows = "".join(
        f"""
        <tr>
          <td style="padding:6px 0;color:#666;width:140px;vertical-align:top;">{html.escape(label)}</td>
          <td style="padding:6px 0;color:#222;word-break:break-word;">{html.escape(value)}</td>
        </tr>"""
        for label, value in _technical_details(incident)
    )

    show_analysis = bool(analysis_raw and analysis_raw != summary_raw)
    subsystem = html.escape(_friendly_subsystem(incident.subsystem))
    fix_lines = _format_all_fixes(incident)
    fix_html = "".join(
        f'<li style="margin:0 0 8px 0;">{html.escape(line)}</li>' for line in fix_lines
    )
    intro_lines = _plain_english_intro(incident)
    intro_html = "".join(
        f'<p style="margin:0 0 10px 0;line-height:1.6;color:#333;">{html.escape(line)}</p>'
        for line in intro_lines
        if line and not line.startswith("-")
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{html.escape(email_subject(incident))}</title>
</head>
<body style="margin:0;padding:0;background:#f4f6f8;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;color:#222;">
  <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:#f4f6f8;padding:24px 12px;">
    <tr>
      <td align="center">
        <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="max-width:640px;background:#ffffff;border:1px solid #dfe3e8;border-radius:12px;overflow:hidden;">
          <tr>
            <td style="background:#152238;color:#ffffff;padding:24px 28px;">
              <div style="font-size:13px;letter-spacing:0.08em;text-transform:uppercase;opacity:0.82;">NiNO Intelligent Mode · Testing & Recovery Agent</div>
              <div style="font-size:24px;font-weight:700;line-height:1.3;margin-top:8px;">{headline}</div>
            </td>
          </tr>
          <tr>
            <td style="padding:24px 28px 8px 28px;">
              <span style="display:inline-block;padding:6px 12px;border-radius:999px;background:{badge_bg};color:{badge_fg};font-size:12px;font-weight:700;letter-spacing:0.04em;text-transform:uppercase;">{status}</span>
            </td>
          </tr>
          <tr>
            <td style="padding:8px 28px 24px 28px;">
              <div style="padding:14px 16px;background:#f0f7ff;border:1px solid #c8ddf5;border-radius:10px;margin:0 0 18px 0;">
                <div style="font-size:12px;color:#005ea8;text-transform:uppercase;letter-spacing:0.06em;font-weight:700;margin-bottom:8px;">In plain English</div>
                {intro_html}
              </div>
              <p style="margin:0 0 18px 0;font-size:16px;line-height:1.6;">{summary}</p>
              <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="margin:0 0 20px 0;">
                <tr>
                  <td style="padding:14px 16px;background:#f8fafc;border:1px solid #e5e9ef;border-radius:10px;">
                    <div style="font-size:12px;color:#667085;text-transform:uppercase;letter-spacing:0.06em;">Robot / system</div>
                    <div style="font-size:16px;font-weight:600;margin-top:4px;">{bot}</div>
                    <div style="font-size:12px;color:#667085;text-transform:uppercase;letter-spacing:0.06em;margin-top:14px;">Affected area</div>
                    <div style="font-size:15px;margin-top:4px;">{subsystem}</div>
                  </td>
                </tr>
              </table>

              <div style="font-size:13px;font-weight:700;text-transform:uppercase;letter-spacing:0.06em;color:#667085;margin-bottom:8px;">What Intelligent Mode tried</div>
              <ul style="margin:0 0 20px 20px;padding:0;line-height:1.6;">{fix_html}</ul>

              <div style="font-size:13px;font-weight:700;text-transform:uppercase;letter-spacing:0.06em;color:#667085;margin-bottom:8px;">What to do next</div>
              <ol style="margin:0 0 20px 20px;padding:0;line-height:1.6;">{steps_html}</ol>

              {"<div style='font-size:13px;font-weight:700;text-transform:uppercase;letter-spacing:0.06em;color:#667085;margin-bottom:8px;'>Analysis</div><p style='margin:0 0 20px 0;line-height:1.6;'>" + analysis + "</p>" if show_analysis else ""}

              {_agent_remediation_html(incident)}

              {_code_bug_html(incident)}

              <div style="font-size:13px;font-weight:700;text-transform:uppercase;letter-spacing:0.06em;color:#667085;margin-bottom:8px;">Technical details</div>
              <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="font-size:13px;line-height:1.5;">{technical_rows}</table>
            </td>
          </tr>
          <tr>
            <td style="padding:18px 28px;background:#f8fafc;border-top:1px solid #e5e9ef;color:#667085;font-size:12px;line-height:1.6;">
              NiNO Intelligent Mode · automated testing, detect, fix, verify, and report<br>
              View live status on your NiNO server Ops Dashboard (<code>/ops</code>)
            </td>
          </tr>
        </table>
      </td>
    </tr>
  </table>
</body>
</html>"""


def build_report(incident: Incident, *, use_llm: bool = True) -> str:
    """Plain-text report stored on the incident and used as email fallback."""
    _ = use_llm
    return _build_plain_report(incident)


def _digest_intro(incidents: list[Incident]) -> str:
    """Summary line for digest email — distinguish auto-resolved vs needs attention."""
    if not incidents:
        return "No incidents in this batch."
    open_count = sum(
        1 for inc in incidents if str(inc.status or "").lower() in {"open", "fixing", "escalated"}
    )
    resolved_count = sum(1 for inc in incidents if str(inc.status or "").lower() == "resolved")
    total = len(incidents)
    if open_count == 0 and resolved_count == total:
        return (
            f"{total} incident(s) were handled automatically by Intelligent Mode — "
            "no action needed."
        )
    if open_count > 0:
        return f"{open_count} incident(s) need your attention ({total} total in this batch)."
    return f"{total} incident(s) in this batch."


def build_digest_report(incidents: list[Incident], *, use_llm: bool = False) -> str:
    _ = use_llm
    if not incidents:
        return "NiNO Intelligent Mode Digest\n\nNo incidents in this batch.\n"

    lines = [
        "NiNO Intelligent Mode Digest",
        "=" * 40,
        "Intelligent Mode is NiNO's automated testing and recovery agent.",
        _digest_intro(incidents),
        "",
    ]
    for index, inc in enumerate(incidents, start=1):
        agent_note = ""
        if _is_agent_handled(inc):
            pattern_id = _agent_pattern_id(inc)
            copy = _AGENT_PATTERN_EMAIL.get(pattern_id, {})
            if copy.get("your_action"):
                agent_note = f"   Action: {copy['your_action']}"
        lines.extend(
            [
                f"{index}. {_headline(inc)}",
                f"   Status: {_friendly_status(inc.status, incident=inc)}",
                f"   Issue:  {_humanize_error(inc)}",
            ]
        )
        if agent_note:
            lines.append(agent_note)
        lines.extend([f"   ID:     {inc.incident_id}", ""])
    lines.extend(
        [
            "—",
            "Open the Ops Dashboard on your NiNO server for full details (/ops).",
        ]
    )
    return "\n".join(lines) + "\n"


def build_digest_html(incidents: list[Incident]) -> str:
    items = []
    for inc in incidents:
        badge_fg, badge_bg = _badge_colors(inc)
        items.append(
            f"""
            <tr>
              <td style="padding:16px 0;border-bottom:1px solid #e5e9ef;">
                <div style="font-size:15px;font-weight:600;">{html.escape(_headline(inc))}</div>
                <div style="margin-top:6px;">
                  <span style="display:inline-block;padding:4px 10px;border-radius:999px;background:{badge_bg};color:{badge_fg};font-size:11px;font-weight:700;text-transform:uppercase;">{html.escape(_friendly_status(inc.status))}</span>
                </div>
                <div style="margin-top:8px;color:#444;line-height:1.5;">{html.escape(_humanize_error(inc))}</div>
                <div style="margin-top:6px;color:#667085;font-size:12px;">Incident ID: {html.escape(inc.incident_id)}</div>
              </td>
            </tr>"""
        )
    body = "".join(items)
    return f"""<!DOCTYPE html>
<html lang="en"><body style="margin:0;padding:24px;background:#f4f6f8;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;">
<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="max-width:640px;margin:0 auto;background:#fff;border:1px solid #dfe3e8;border-radius:12px;">
<tr><td style="background:#152238;color:#fff;padding:20px 24px;font-size:22px;font-weight:700;">NiNO Intelligent Mode Digest</td></tr>
<tr><td style="padding:20px 24px;color:#444;">{html.escape(_digest_intro(incidents))}</td></tr>
<tr><td style="padding:0 24px 20px 24px;"><table role="presentation" width="100%" cellspacing="0" cellpadding="0">{body}</table></td></tr>
<tr><td style="padding:16px 24px;background:#f8fafc;border-top:1px solid #e5e9ef;color:#667085;font-size:12px;">View live status on your NiNO server Ops Dashboard (<code>/ops</code>)</td></tr>
</table></body></html>"""

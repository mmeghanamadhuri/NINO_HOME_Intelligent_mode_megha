"""Self-debugging — gather evidence, classify root cause, suggest next steps."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from intelligent_mode.incidents import Incident
from intelligent_mode.soak_test import parse_soak_unexpected_reply, soak_reply_would_pass

logger = logging.getLogger(__name__)

_LATENCY_PATH = Path(__file__).resolve().parent.parent / "data" / "latency_log.json"
_SMOKE_PATH = Path(__file__).resolve().parent.parent / "data" / "intelligent_smoke_tests.json"


@dataclass
class DebugReport:
    incident_id: str
    category: str  # operational | configuration | logic_bug | hardware | regression | unknown
    root_cause: str
    confidence: str  # high | medium | low
    fixable_by_agent: bool
    evidence: list[str] = field(default_factory=list)
    suggested_actions: list[str] = field(default_factory=list)
    related_errors: list[str] = field(default_factory=list)
    llm_analysis: str = ""
    analyzed_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _load_latency_records(*, limit: int = 200) -> list[dict[str, Any]]:
    if not _LATENCY_PATH.is_file():
        return []
    try:
        raw = json.loads(_LATENCY_PATH.read_text(encoding="utf-8"))
        if not isinstance(raw, list):
            return []
        rows = [row for row in raw if isinstance(row, dict)]
        return rows[-limit:]
    except (OSError, json.JSONDecodeError):
        return []


def _load_smoke_history(*, limit: int = 10) -> list[dict[str, Any]]:
    if not _SMOKE_PATH.is_file():
        return []
    try:
        raw = json.loads(_SMOKE_PATH.read_text(encoding="utf-8"))
        history = raw.get("history") if isinstance(raw, dict) else []
        if not isinstance(history, list):
            return []
        return list(history[-limit:])
    except (OSError, json.JSONDecodeError):
        return []


def _recent_latency_for_device(
    records: list[dict[str, Any]], device_id: str, *, limit: int = 8
) -> list[dict[str, Any]]:
    if device_id == "server":
        return [r for r in records if r.get("event") == "voice_query"][-limit:]
    return [
        r
        for r in records
        if str(r.get("device_id") or "") == device_id
        or (device_id != "server" and str(r.get("device_id") or "").startswith(device_id[:8]))
    ][-limit:]


def _voice_error_patterns(records: list[dict[str, Any]]) -> list[str]:
    patterns: list[str] = []
    for row in records:
        if row.get("event") != "voice_query":
            continue
        err = str(row.get("error") or "").strip()
        path = str(row.get("reply_path") or "").strip()
        if err:
            patterns.append(f"voice error: {err[:120]}")
        if path in {"stt_empty", "stt_silent", "stt_rejected", "llm_error", "llm_unreachable"}:
            patterns.append(f"reply_path={path}")
        reply = str(row.get("reply_text") or "").lower()
        if "could not reach" in reply or "language model" in reply:
            patterns.append("spoken LLM failure in reply")
    return patterns


def _smoke_regression(
    history: list[dict[str, Any]], test_id: str
) -> str | None:
    """Return message if a test recently flipped from pass to fail."""
    outcomes: list[bool | None] = []
    for run in history:
        for result in run.get("results") or []:
            if not isinstance(result, dict):
                continue
            if str(result.get("test_id") or "") != test_id:
                continue
            outcomes.append(bool(result.get("passed")))
    if len(outcomes) < 2:
        return None
    if outcomes[-2] is True and outcomes[-1] is False:
        return f"Regression: {test_id} passed then failed across recent smoke runs"
    return None


def _rule_analyze(incident: Incident, evidence: list[str]) -> DebugReport:
    error = str(incident.error or "").lower()
    raw_error = str(incident.error or "")
    subsystem = incident.subsystem
    fixes = incident.fixes or []
    last_fix = fixes[-1] if fixes else None
    fix_failed = bool(last_fix and not last_fix.success)
    repeated_fix = len(fixes) >= 2 and all(not f.success for f in fixes[-2:])

    category = "unknown"
    root_cause = "Unable to determine root cause from available signals."
    confidence = "low"
    fixable = incident.tier <= 1 and incident.status != "escalated"
    suggested: list[str] = []

    parsed_soak = parse_soak_unexpected_reply(raw_error)
    if parsed_soak is not None:
        path, reply = parsed_soak
        if soak_reply_would_pass(path=path, reply=reply):
            return DebugReport(
                incident_id=incident.incident_id,
                category="operational",
                root_cause="Soak test flagged a valid voice reply — no developer fix needed.",
                confidence="high",
                fixable_by_agent=True,
                evidence=evidence[:8],
                suggested_actions=[
                    "No action needed — voice routing and reply were acceptable.",
                ],
            )

    if subsystem == "llm":
        if "connection refused" in error or "unreachable" in error:
            category = "operational"
            root_cause = "Ollama LLM service is not running or not reachable on the configured port."
            confidence = "high"
            fixable = not repeated_fix
            suggested = [
                "Run: bash server/scripts/start_ollama_gpu.sh",
                "Verify OLLAMA_URL points to a live instance (GPU port 11435 on Linux).",
            ]
        elif "not loaded" in error:
            category = "operational"
            root_cause = "Ollama is reachable but the configured model is not loaded in memory."
            confidence = "high"
            suggested = ["Run: ollama pull qwen2.5:1.5b", "Check OLLAMA_MODEL env var."]

    elif subsystem == "memory":
        if "connection refused" in error or "not ready" in error:
            category = "operational"
            root_cause = "PostgreSQL memory database is down or DATABASE_URL is wrong."
            confidence = "high"
            suggested = [
                "Run: bash server/scripts/init_memory_db.sh",
                "Confirm PostgreSQL is running on the host in DATABASE_URL.",
            ]

    elif subsystem == "camera":
        if "503" in error and ("idle" in error or "session" in error):
            category = "operational"
            root_cause = (
                "Camera returned HTTP 503 while no voice session is active — "
                "expected idle behavior for session-gated UVC."
            )
            confidence = "high"
            fixable = False
            suggested = [
                "No action needed when the bot is idle.",
                "Start a voice session (voice connect + wake on) to verify live camera.",
            ]
        elif "503" in error:
            category = "operational"
            root_cause = "ESP camera endpoint returned 503 during an active session — UVC may still be starting."
            confidence = "medium"
            suggested = [
                "Wait for USB hub enumeration to finish, then retry.",
                "Check ESP serial logs for UVC/camera errors.",
            ]
        elif "disconnected" in error or "stale" in error or "session" in error:
            category = "operational"
            root_cause = "Camera MJPEG stream is disconnected or not producing frames."
            confidence = "medium"
            suggested = [
                "Verify ESP is online and /stream responds.",
                "Check USB camera wiring on J18 hub.",
            ]

    elif subsystem == "bot":
        if "503" in error and ("idle" in error or "session" in error):
            category = "operational"
            root_cause = "Bot snapshot returned 503 with no active voice session — camera is session-gated."
            confidence = "high"
            fixable = False
            suggested = ["No action needed while idle.", "Use voice wake to open a session and retest."]
        elif "503" in error or "unreachable" in error or "http" in error:
            category = "operational"
            root_cause = "ESP32 bot HTTP endpoint is unreachable — network, power, or firmware hang."
            confidence = "medium"
            suggested = [
                "Ping the bot IP and open /snapshot.jpg in a browser.",
                "Power-cycle the ESP if HTTP stays down.",
            ]
        elif "no base url" in error or "missing" in error:
            category = "configuration"
            root_cause = "Bot is known but has no configured base/camera/playback URLs."
            confidence = "high"
            fixable = True
            suggested = [
                "Run LAN discovery or set devices.json with play_wav_url and camera_url.",
            ]

    elif subsystem == "stt":
        category = "operational"
        root_cause = "Speech-to-text model is not loaded or cloud STT API key is missing."
        confidence = "medium"
        suggested = [
            "Set ELEVENLABS_API_KEY or wait for Whisper preload.",
            "Check STT_PROVIDER configuration.",
        ]

    elif subsystem == "tts":
        category = "operational"
        root_cause = "Text-to-speech failed — Piper model missing or playback route error."
        confidence = "medium"
        suggested = [
            "Verify TTS_PROVIDER and Piper model download.",
            "Check ESP /play_wav route from the server.",
        ]

    elif subsystem == "voice":
        if any("reply_path=llm" in e for e in evidence):
            category = "logic_bug"
            root_cause = "Voice pipeline reaches STT but LLM reply generation fails — likely logic or routing bug."
            confidence = "medium"
            fixable = False
            suggested = [
                "Inspect voice_service.py reply_path handling for this utterance.",
                "Check latency_log.json for the failing voice_query record.",
            ]
        elif any("reply_path=stt" in e for e in evidence):
            category = "configuration"
            root_cause = "Voice pipeline fails at STT — mic audio empty or STT misconfigured."
            confidence = "medium"
            suggested = [
                "Verify USB 4-mic is streaming (voice status on ESP serial).",
                "Check STT provider and API keys.",
            ]
        else:
            category = "operational"
            root_cause = "Voice pipeline state may be stuck — session or registration conflict."
            confidence = "low"
            suggested = ["Reset voice state; verify VOICE_WS_URL is configured."]

    elif subsystem == "discovery":
        category = "configuration"
        root_cause = "LAN device discovery failed or found no bots."
        confidence = "medium"
        suggested = [
            "Ensure ESP and PC are on the same subnet.",
            "Check mDNS/_nino._tcp and firewall rules.",
        ]

    # Escalated + repeated failed fixes → likely not agent-fixable
    if incident.status == "escalated" or repeated_fix:
        fixable = False
        if category == "operational":
            category = "hardware" if "503" in error or "unreachable" in error else category
        suggested.append("Intelligent Mode fix chain exhausted — human investigation required.")
        if not suggested or suggested == ["Intelligent Mode fix chain exhausted — human investigation required."]:
            suggested.insert(
                0,
                "Review incident evidence and server logs (grep 'NINO |').",
            )

    # Logic bug hint from latency even when subsystem isn't voice
    voice_errors = [e for e in evidence if e.startswith("voice error:") or "reply_path=" in e]
    if voice_errors and category == "unknown":
        category = "logic_bug"
        root_cause = "Recent voice queries show pipeline failures despite infrastructure checks."
        confidence = "medium"
        fixable = False
        suggested.append("Inspect latency_log.json and voice_service routing for the failing path.")

    smoke_hint = incident.before_snapshot.get("hint") if isinstance(incident.before_snapshot, dict) else {}
    smoke_test = smoke_hint.get("smoke_test") if isinstance(smoke_hint, dict) else None
    if isinstance(smoke_test, dict):
        test_id = str(smoke_test.get("test_id") or "")
        history = _load_smoke_history(limit=8)
        regression = _smoke_regression(history, test_id) if test_id else None
        if regression:
            category = "regression"
            root_cause = regression
            confidence = "high"
            fixable = False
            suggested.append(f"Compare recent changes affecting {test_id}.")

    return DebugReport(
        incident_id=incident.incident_id,
        category=category,
        root_cause=root_cause,
        confidence=confidence,
        fixable_by_agent=fixable,
        evidence=evidence,
        suggested_actions=suggested,
        related_errors=voice_errors,
    )


def _maybe_llm_analyze(incident: Incident, report: DebugReport) -> str:
    if incident.subsystem == "llm":
        return ""
    try:
        from llm_service import ollama_is_reachable, ollama_generate

        if not ollama_is_reachable():
            return ""
        prompt = (
            "You are a robot server debugger. Given an incident, write 2-3 sentences: "
            "most likely root cause, whether it is ops/config/code/hardware, and one next step. "
            "Plain English, no markdown.\n\n"
            f"Bot: {incident.display_name} ({incident.device_id})\n"
            f"Subsystem: {incident.subsystem}\n"
            f"Error: {incident.error}\n"
            f"Status: {incident.status}\n"
            f"Rule analysis: {report.root_cause}\n"
            f"Evidence: {'; '.join(report.evidence[:6])}\n"
        )
        text = ollama_generate(prompt, num_predict=120, temperature=0.1, timeout_s=30).strip()
        return text[:500]
    except Exception as exc:
        logger.debug("LLM debug analysis skipped: %s", exc)
        return ""


def analyze_incident(
    incident: Incident,
    *,
    snapshot: dict[str, Any] | None = None,
    use_llm: bool = True,
) -> DebugReport:
    """Build a structured debug report for an incident."""
    evidence: list[str] = []

    records = _load_latency_records(limit=300)
    device_records = _recent_latency_for_device(records, incident.device_id)
    voice_patterns = _voice_error_patterns(device_records)
    evidence.extend(voice_patterns[:5])

    for fix in incident.fixes[-3:]:
        evidence.append(
            f"fix {fix.action}: {'ok' if fix.success else 'fail'} — {fix.detail[:100]}"
        )

    hint = incident.before_snapshot.get("hint") if isinstance(incident.before_snapshot, dict) else {}
    if isinstance(hint, dict) and hint:
        smoke = hint.get("smoke_test")
        if isinstance(smoke, dict):
            evidence.append(
                f"smoke {smoke.get('test_id')}: {smoke.get('message', '')[:100]}"
            )

    if snapshot:
        if incident.subsystem == "llm":
            llm = snapshot.get("llm") or {}
            if isinstance(llm, dict):
                evidence.append(f"ollama reachable={llm.get('reachable')} loaded={llm.get('loaded')}")
        if incident.subsystem == "camera" and incident.device_id != "server":
            cams = snapshot.get("cameras") or {}
            cam = cams.get(incident.device_id) if isinstance(cams, dict) else {}
            if isinstance(cam, dict):
                evidence.append(
                    f"camera connected={cam.get('connected')} error={str(cam.get('last_error') or '')[:80]}"
                )

    report = _rule_analyze(incident, evidence)
    if use_llm:
        llm_text = _maybe_llm_analyze(incident, report)
        if llm_text:
            report.llm_analysis = llm_text
    return report


def build_debug_report(
    incident: Incident,
    *,
    snapshot: dict[str, Any] | None = None,
    use_llm: bool = True,
    analyze_code: bool = True,
) -> dict[str, Any]:
    report = analyze_incident(incident, snapshot=snapshot, use_llm=use_llm)
    payload = report.to_dict()
    if not analyze_code:
        return payload
    try:
        from intelligent_mode.agent_remediation import (
            classify_agent_remediation,
            merge_remediation_into_debug,
        )

        # Agent remediation overrides code-bug classification for known benign patterns.
        payload = merge_remediation_into_debug(payload, incident)
        plan = classify_agent_remediation(incident)
        if plan is not None and plan.pattern_id in {
            "soak_live_session_skip",
            "ollama_cpu_optional",
            "soak_valid_reply",
        }:
            return payload

        from intelligent_mode.code_bug_analyzer import analyze_code_bug, merge_into_debug_report

        code_analysis = analyze_code_bug(incident, use_llm=use_llm)
        payload = merge_into_debug_report(payload, code_analysis)
        payload = merge_remediation_into_debug(payload, incident)
    except Exception:
        logger.exception("Code bug analysis failed for incident %s", incident.incident_id)
    return payload

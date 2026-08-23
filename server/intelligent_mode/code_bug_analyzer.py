"""Detect likely code bugs, suggest fixes, and recommend firmware OTA."""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from intelligent_mode.incidents import Incident
from intelligent_mode.soak_test import parse_soak_unexpected_reply, soak_reply_would_pass

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_SERVER_ROOT = Path(__file__).resolve().parent.parent
_LATENCY_PATH = _SERVER_ROOT / "data" / "latency_log.json"


@dataclass
class CodeBugAnalysis:
    is_code_bug: bool = False
    bug_summary: str = ""
    likely_cause: str = ""
    suggested_fix: str = ""
    affected_files: list[str] = field(default_factory=list)
    firmware_update_recommended: bool = False
    firmware_filename: str = ""
    server_change_recommended: bool = False
    ota_deployed: bool = False
    ota_detail: str = ""
    confidence: str = "low"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> CodeBugAnalysis:
        fields = cls.__dataclass_fields__
        return cls(**{k: raw[k] for k in fields if k in raw})


def _load_recent_voice_rows(device_id: str, *, limit: int = 20) -> list[dict[str, Any]]:
    if not _LATENCY_PATH.is_file():
        return []
    try:
        raw = json.loads(_LATENCY_PATH.read_text(encoding="utf-8"))
        if not isinstance(raw, list):
            return []
        rows = [r for r in raw if isinstance(r, dict) and r.get("event") == "voice_query"]
        if device_id != "server":
            rows = [
                r
                for r in rows
                if str(r.get("device_id") or "") == device_id
                or str(r.get("device_id") or "").startswith(device_id[:8])
            ]
        return rows[-limit:]
    except (OSError, json.JSONDecodeError):
        return []


def _read_snippet(rel_path: str, *, around_line: int | None = None, radius: int = 8) -> str:
    path = _REPO_ROOT / rel_path
    if not path.is_file():
        path = _SERVER_ROOT / rel_path
    if not path.is_file():
        return ""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    if around_line is not None and 1 <= around_line <= len(lines):
        start = max(0, around_line - 1 - radius)
        end = min(len(lines), around_line + radius)
        chunk = lines[start:end]
        return "\n".join(f"{start + i + 1:4d}| {line}" for i, line in enumerate(chunk))
    return "\n".join(f"{i + 1:4d}| {line}" for i, line in enumerate(lines[:40]))


def _latest_firmware_bin() -> str:
    explicit = os.environ.get("INTELLIGENT_OTA_FIRMWARE", "").strip()
    if explicit:
        path = _SERVER_ROOT / "firmware" / Path(explicit).name
        if path.is_file():
            return path.name
    fw_dir = _SERVER_ROOT / "firmware"
    if not fw_dir.is_dir():
        return ""
    bins = sorted(fw_dir.glob("*.bin"), key=lambda p: p.stat().st_mtime, reverse=True)
    return bins[0].name if bins else ""


def _pattern_from_incident(incident: Incident, voice_rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    error = str(incident.error or "").lower()
    subsystem = str(incident.subsystem or "").lower()

    parsed = parse_soak_unexpected_reply(str(incident.error or ""))
    if parsed is not None:
        path, reply = parsed
        if soak_reply_would_pass(path=path, reply=reply):
            return None

    reply_paths = {str(r.get("reply_path") or "").lower() for r in voice_rows}
    errors = {str(r.get("error") or "").lower() for r in voice_rows}

    error_suggests_stt = (
        "no speech" in error
        or "stt empty" in error
        or "stt_empty" in error
        or "wake_reject" in error
        or "stt path=" in error
    )
    latency_suggests_stt = (
        "stt_empty" in reply_paths
        or "wake_reject" in reply_paths
        or any("no speech" in e for e in errors)
    )
    voice_stt_issue = error_suggests_stt or (
        latency_suggests_stt
        and "[soak:" not in error
        and "unexpected reply" not in error
    )

    if "wav too large" in error or (
        "play_wav failed" in error and "too large" in error
    ):
        return None

    if subsystem == "voice" and voice_stt_issue:
        return None

    if subsystem == "voice" and "llm" in reply_paths:
        return {
            "id": "voice_llm_routing",
            "bug_summary": "Voice reaches STT but LLM reply generation fails — likely a routing or prompt bug.",
            "likely_cause": "LLM timeout, bad OLLAMA_URL, or voice_service reply_path handling after STT.",
            "suggested_fix": (
                "1. Inspect server/voice_service.py LLM branch after transcribe_wav.\n"
                "2. Check server/llm_service.py ollama_generate timeouts and model name.\n"
                "3. Review latency_log.json for reply_path=llm_error rows."
            ),
            "affected_files": ["server/voice_service.py", "server/llm_service.py"],
            "firmware_update_recommended": False,
            "server_change_recommended": True,
            "confidence": "medium",
        }

    if subsystem == "camera" and ("503" in error or "disconnected" in error):
        return {
            "id": "camera_session",
            "bug_summary": "Camera stream fails during an active voice session.",
            "likely_cause": "UVC host timing, session gating in firmware, or camera restart race on the server.",
            "suggested_fix": (
                "1. Check main/ UVC camera code and session gating with voice_assist.\n"
                "2. Review server/camera.py restart logic.\n"
                "3. Rebuild and OTA firmware if the ESP camera endpoint fails while voice is active."
            ),
            "affected_files": ["main/", "server/camera.py", "server/intelligent_mode/session_camera.py"],
            "firmware_update_recommended": True,
            "server_change_recommended": True,
            "confidence": "medium",
        }

    debug = incident.debug_report if isinstance(incident.debug_report, dict) else {}
    if str(debug.get("category") or "") in {"logic_bug", "regression"}:
        return {
            "id": "generic_logic_bug",
            "bug_summary": str(debug.get("root_cause") or incident.error or "Logic bug detected"),
            "likely_cause": str(debug.get("root_cause") or "See incident evidence and server logs."),
            "suggested_fix": "\n".join(
                f"- {a}" for a in (debug.get("suggested_actions") or [])[:5]
            )
            or "Review NiNO logs and recent git changes.",
            "affected_files": [],
            "firmware_update_recommended": subsystem in {"camera", "voice", "bot"},
            "server_change_recommended": subsystem != "bot" or subsystem == "server",
            "confidence": str(debug.get("confidence") or "medium"),
        }

    return None


def _maybe_llm_code_fix(
    incident: Incident,
    pattern: dict[str, Any],
    snippets: dict[str, str],
) -> str:
    try:
        from llm_service import ollama_generate, ollama_is_reachable

        if not ollama_is_reachable():
            return ""
    except Exception:
        return ""

    snippet_text = ""
    for path, body in snippets.items():
        if body:
            snippet_text += f"\n--- {path} ---\n{body[:1200]}\n"

    prompt = (
        "You are a NiNO robot codebase debugger. Write a short developer note (max 8 lines):\n"
        "1) What code bug is likely\n"
        "2) Which file(s) to change\n"
        "3) Concrete fix steps (no markdown)\n"
        "4) Say 'Rebuild firmware and OTA' only if ESP/main/ code may need changing\n\n"
        f"Subsystem: {incident.subsystem}\n"
        f"Error: {incident.error}\n"
        f"Pattern: {pattern.get('bug_summary')}\n"
        f"Initial fix hint: {pattern.get('suggested_fix')}\n"
        f"{snippet_text}\n"
    )
    try:
        text = ollama_generate(prompt, num_predict=200, temperature=0.15, timeout_s=45).strip()
        return text[:800]
    except Exception as exc:
        logger.debug("LLM code fix analysis skipped: %s", exc)
        return ""


def analyze_code_bug(
    incident: Incident,
    *,
    use_llm: bool = True,
) -> CodeBugAnalysis:
    """Return structured code-bug analysis for an incident."""
    if _agent_handles_incident(incident):
        return CodeBugAnalysis(is_code_bug=False)
    voice_rows = _load_recent_voice_rows(incident.device_id)
    pattern = _pattern_from_incident(incident, voice_rows)
    if pattern is None:
        return CodeBugAnalysis(is_code_bug=False)

    snippets: dict[str, str] = {}
    for rel in pattern.get("affected_files") or []:
        if rel.endswith("/"):
            continue
        if rel == "server/voice_service.py":
            snippets[rel] = _read_snippet(rel, around_line=2533)
        else:
            snippets[rel] = _read_snippet(rel)

    suggested = str(pattern.get("suggested_fix") or "")
    if use_llm:
        llm_fix = _maybe_llm_code_fix(incident, pattern, snippets)
        if llm_fix:
            suggested = f"{suggested}\n\nLLM developer note:\n{llm_fix}"

    fw_name = _latest_firmware_bin() if pattern.get("firmware_update_recommended") else ""

    return CodeBugAnalysis(
        is_code_bug=True,
        bug_summary=str(pattern.get("bug_summary") or ""),
        likely_cause=str(pattern.get("likely_cause") or ""),
        suggested_fix=suggested.strip(),
        affected_files=[str(f) for f in (pattern.get("affected_files") or []) if str(f).strip()],
        firmware_update_recommended=bool(pattern.get("firmware_update_recommended")),
        firmware_filename=fw_name,
        server_change_recommended=bool(pattern.get("server_change_recommended")),
        confidence=str(pattern.get("confidence") or "medium"),
    )


def try_firmware_ota_for_incident(incident: Incident, analysis: CodeBugAnalysis) -> CodeBugAnalysis:
    """Queue or deploy OTA when auto-OTA is enabled and a firmware build exists."""
    if not analysis.firmware_update_recommended:
        return analysis
    if os.environ.get("INTELLIGENT_AUTO_OTA", "0").strip().lower() in {"0", "false", "no", "off"}:
        analysis.ota_detail = "Auto-OTA disabled (set INTELLIGENT_AUTO_OTA=1 to enable)."
        return analysis
    if not analysis.firmware_filename:
        analysis.ota_detail = "No firmware .bin in server/firmware/ — build and upload first."
        return analysis
    if incident.device_id in {"", "server"}:
        analysis.ota_detail = "No target bot for OTA (server-only incident)."
        return analysis

    try:
        from intelligent_mode.context import get_context
        from ota_service import request_firmware_deploy

        ctx = get_context()
        record = ctx.registry.get(incident.device_id)
        if record is None:
            analysis.ota_detail = f"Bot {incident.device_id} not in registry."
            return analysis
        base = record.effective_base_url() if hasattr(record, "effective_base_url") else ""
        if not base:
            analysis.ota_detail = "Bot has no base URL for OTA."
            return analysis

        result = request_firmware_deploy(
            device_id=incident.device_id,
            filename=analysis.firmware_filename,
            base_url=base,
            requested_by="intelligent-mode",
        )
        status = str(result.get("status") or "")
        analysis.ota_deployed = status in {"deployed", "approved", "pending"}
        analysis.ota_detail = (
            f"OTA {status}: {analysis.firmware_filename} → {incident.device_id} "
            f"({result.get('firmware_url') or result.get('detail') or ''})"
        )[:500]
    except Exception as exc:
        analysis.ota_detail = f"OTA failed: {exc}"
        logger.warning("Intelligent mode OTA failed for %s: %s", incident.device_id, exc)

    return analysis


def _agent_handles_incident(incident: Incident) -> bool:
    try:
        from intelligent_mode.agent_remediation import is_agent_remediatable_incident

        return is_agent_remediatable_incident(incident)
    except Exception:
        return False


def is_code_bug_incident(incident: Incident) -> bool:
    """True when the incident requires a developer/code change, not ops recovery."""
    if _agent_handles_incident(incident):
        return False
    error = str(incident.error or "")
    error_lower = error.lower()
    parsed = parse_soak_unexpected_reply(error)
    if parsed is not None:
        path, reply = parsed
        if soak_reply_would_pass(path=path, reply=reply):
            return False
    debug = incident.debug_report if isinstance(incident.debug_report, dict) else {}
    code_bug = debug.get("code_bug") if isinstance(debug.get("code_bug"), dict) else {}
    if code_bug.get("is_code_bug"):
        return True
    category = str(debug.get("category") or "").lower()
    if category in {"logic_bug", "regression"} and debug.get("fixable_by_agent") is False:
        if parsed is not None:
            return False
        return True
    return False


def merge_into_debug_report(debug: dict[str, Any], analysis: CodeBugAnalysis) -> dict[str, Any]:
    """Attach code-bug fields to an existing debug_report dict."""
    if not analysis.is_code_bug:
        out = dict(debug)
        out.pop("code_bug", None)
        if out.get("category") == "logic_bug" and out.get("fixable_by_agent") is False:
            out["category"] = "operational"
            out["fixable_by_agent"] = True
            out["root_cause"] = (
                out.get("root_cause")
                or "Soak test flagged a valid voice reply — no developer fix needed."
            )
        return out
    out = dict(debug)
    out["category"] = "logic_bug"
    out["fixable_by_agent"] = False
    out["code_bug"] = analysis.to_dict()
    if analysis.bug_summary and not out.get("root_cause"):
        out["root_cause"] = analysis.bug_summary
    steps = list(out.get("suggested_actions") or [])
    if analysis.suggested_fix:
        steps.insert(0, "Code fix: see email section SUGGESTED FIX")
    if analysis.firmware_update_recommended:
        steps.append(
            f"Rebuild firmware and OTA deploy"
            + (f" ({analysis.firmware_filename})" if analysis.firmware_filename else "")
        )
    out["suggested_actions"] = steps[:8]
    return out

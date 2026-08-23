#!/usr/bin/env python3
"""Build and email an intelligent-mode report immediately."""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

SERVER_DIR = Path(__file__).resolve().parent.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))


def _load_env() -> None:
    env_path = SERVER_DIR / ".env"
    if not env_path.is_file():
        return
    try:
        from dotenv import load_dotenv

        load_dotenv(env_path)
        return
    except ImportError:
        pass
    for line in env_path.read_text(encoding="utf-8").splitlines():
        raw = line.strip()
        if not raw or raw.startswith("#") or "=" not in raw:
            continue
        if raw.startswith("export "):
            raw = raw[7:].strip()
        key, _, value = raw.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _build_snapshot() -> dict:
    from device_registry import get_device_registry
    from device_discovery import discovery_status
    from camera import CameraPool
    from tts_service import TTSService
    from face_registration_service import get_face_registration_service
    from llm_service import ollama_runtime_status
    from memory_service import get_memory_service
    from voice_service import whisper_runtime_status

    registry = get_device_registry()
    cameras = CameraPool()
    cameras.configure_from_registry()
    tts = TTSService()
    face_reg = get_face_registration_service()
    active = registry.ui_device_id()
    return {
        "device_id": active,
        "devices": registry.status(),
        "discovery": discovery_status(),
        "camera": cameras.status(active),
        "cameras": cameras.status(),
        "tts": tts.status(),
        "stt": whisper_runtime_status(),
        "llm": ollama_runtime_status(
            model=os.environ.get("OLLAMA_MODEL"),
            api_url=os.environ.get("OLLAMA_URL"),
        ),
        "memory": get_memory_service().status(),
        "face_registration": face_reg.status() if face_reg else {},
    }


def _format_smoke_report(run_dict: dict) -> str:
    lines = [
        "NiNO Intelligent Mode — Test Report",
        f"Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
        "",
        f"Smoke tests: {run_dict.get('passed', 0)}/{run_dict.get('total', 0)} passed",
        f"Overall: {'HEALTHY' if run_dict.get('ok') else 'ISSUES DETECTED'}",
        "",
    ]
    for item in run_dict.get("results") or []:
        mark = "PASS" if item.get("passed") else "FAIL"
        lines.append(
            f"[{mark}] {item.get('test_id')} — {item.get('message')} "
            f"({item.get('duration_ms')} ms)"
        )
    lines.extend(["", "— NiNO Server intelligent mode"])
    return "\n".join(lines)


def main() -> int:
    _load_env()
    from intelligent_mode.config import load_config
    from intelligent_mode.emailer import email_configured, send_incident_email
    from intelligent_mode.incidents import Incident
    from intelligent_mode.reporter import build_report
    from intelligent_mode.smoke_tests import run_smoke_suite

    config = load_config()
    if not config.email_to:
        print("Set INTELLIGENT_EMAIL_TO in server/.env", file=sys.stderr)
        return 1
    if not email_configured(config):
        print(
            "Email not fully configured. Set INTELLIGENT_SMTP_HOST and "
            "INTELLIGENT_SMTP_PASSWORD (Gmail App Password for Google Workspace).",
            file=sys.stderr,
        )
        return 1

    print("Collecting server status...")
    snapshot = _build_snapshot()
    print("Running smoke tests...")
    smoke_run = run_smoke_suite(snapshot)
    smoke_dict = smoke_run.to_dict()

    report_body = _format_smoke_report(smoke_dict)
    try:
        llm_report = build_report(
            Incident(
                device_id="server",
                display_name="NiNO Server",
                subsystem="smoke_tests",
                severity="warning" if smoke_dict.get("failed") else "info",
                tier=1,
                error=f"{smoke_dict.get('failed', 0)} smoke test(s) failed",
                signature="manual:smoke_report",
                status="open" if smoke_dict.get("failed") else "resolved",
                report="",
            ),
            use_llm=config.llm_reports and bool((snapshot.get("llm") or {}).get("reachable")),
        )
        if llm_report.strip():
            report_body = llm_report.strip() + "\n\n---\n\n" + report_body
    except Exception as exc:
        print(f"LLM report skipped: {exc}")

    incident = Incident(
        device_id="server",
        display_name="NiNO Server",
        subsystem="smoke_tests",
        severity="critical" if smoke_dict.get("failed") else "info",
        tier=1,
        error=f"Manual report: {smoke_dict.get('failed', 0)} failed of {smoke_dict.get('total', 0)} tests",
        signature="manual:report",
        status="escalated" if smoke_dict.get("failed") else "resolved",
        report=report_body,
    )

    print(f"Sending report to {config.email_to}...")
    ok, detail = send_incident_email(incident, report_body, config=config)
    if ok:
        print(f"Report sent to {config.email_to}")
        return 0
    print(f"Send failed: {detail}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

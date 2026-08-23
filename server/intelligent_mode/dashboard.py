"""Aggregate bot health, incidents, and agent activity for the ops dashboard."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from intelligent_mode.incident_ui import enrich_incident_for_ui
from intelligent_mode.session_camera import classify_camera_state


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _health_rank(label: str) -> int:
    return {"healthy": 0, "degraded": 1, "critical": 2, "unknown": 3, "offline": 4}.get(
        label, 3
    )


def _worst_health(*labels: str) -> str:
    if not labels:
        return "unknown"
    known = [label for label in labels if label != "unknown"]
    if not known:
        return "unknown"
    return max(known, key=_health_rank)


def _camera_subsystem_health(
    *,
    state: str,
    passed: bool | None,
    incident_severity: str | None = None,
) -> str:
    if incident_severity == "critical":
        return "critical"
    if state == "fault":
        return "critical"
    if state == "in_session":
        return "degraded"
    if state == "idle" and passed is not False:
        return "healthy"
    if passed is False or incident_severity == "warning":
        return "degraded"
    if passed is True or state in {"live", "idle"}:
        return "healthy"
    return "unknown"


def _subsystem_health(
    *,
    passed: bool | None,
    incident_severity: str | None = None,
    live_ok: bool | None = None,
) -> str:
    if incident_severity == "critical":
        return "critical"
    if live_ok is False:
        return "critical"
    if passed is False or incident_severity == "warning":
        return "degraded"
    if passed is True and (live_ok is None or live_ok):
        return "healthy"
    return "unknown"


def _agent_status(incidents: list[dict[str, Any]]) -> str:
    statuses = {str(inc.get("status") or "") for inc in incidents}
    if "fixing" in statuses:
        return "fixing"
    if "escalated" in statuses:
        return "escalated"
    if statuses & {"open"}:
        return "needs_help"
    return "idle"


def _smoke_by_device(last_run: dict[str, Any] | None) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {"server": []}
    if not last_run:
        return out
    for row in last_run.get("results") or []:
        if not isinstance(row, dict):
            continue
        device_id = str(row.get("device_id") or "server")
        out.setdefault(device_id, []).append(row)
    return out


def _incidents_for_device(
    incidents: list[dict[str, Any]], device_id: str, *, active_only: bool = False
) -> list[dict[str, Any]]:
    active_statuses = {"open", "fixing", "escalated"}
    rows: list[dict[str, Any]] = []
    for inc in incidents:
        if str(inc.get("device_id") or "") != device_id:
            continue
        if active_only and str(inc.get("status") or "") not in active_statuses:
            continue
        rows.append(inc)
    return rows


def _agent_label(subsystem: str) -> str:
    labels = {
        "llm": "LLM Agent",
        "camera": "Camera Agent",
        "memory": "Memory Agent",
        "stt": "STT Agent",
        "tts": "TTS Agent",
        "voice": "Voice Agent",
        "discovery": "Discovery Agent",
        "bot": "Bot Agent",
    }
    return labels.get(subsystem, f"{subsystem.title()} Agent")


def _action_label(action: str | None) -> str:
    labels = {
        "camera_restart": "Restart camera stream",
        "lan_discovery": "Re-scan local network",
        "ollama_restart_warm": "Restart AI language model",
        "ollama_cpu_fallback": "Switch AI to CPU fallback",
        "memory_reconnect": "Reconnect conversation memory",
        "postgres_start": "Start PostgreSQL",
        "whisper_preload": "Reload speech-to-text",
        "whisper_reload_cuda": "Reload STT on GPU",
        "whisper_reload_cpu": "Reload STT on CPU",
        "voice_state_reset": "Reset voice session",
        "voice_pipeline_recovery": "Reset full voice pipeline",
        "piper_model_download": "Download TTS voice model",
        "piper_reload_cpu": "Reload TTS on CPU",
    }
    key = str(action or "").strip().lower()
    return labels.get(key, key.replace("_", " ").title() if key else "Waiting")


def _build_agent_activity(incidents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    activity: list[dict[str, Any]] = []
    for inc in incidents:
        status = str(inc.get("status") or "")
        if status not in {"open", "fixing", "escalated"}:
            continue
        subsystem = str(inc.get("subsystem") or "")
        fixes = inc.get("fixes") or []
        last_fix = fixes[-1] if fixes else None
        ui = inc.get("ui") if isinstance(inc.get("ui"), dict) else {}
        activity.append(
            {
                "incident_id": inc.get("incident_id"),
                "device_id": inc.get("device_id"),
                "display_name": inc.get("display_name"),
                "subsystem": subsystem,
                "agent": _agent_label(subsystem),
                "status": status,
                "severity": inc.get("severity"),
                "error": inc.get("error"),
                "plain_english": ui.get("plain_english") or "",
                "issue_kind": ui.get("issue_kind") or "operational",
                "queue": ui.get("queue") or "agent_working",
                "current_action": last_fix.get("action") if isinstance(last_fix, dict) else None,
                "current_action_label": _action_label(
                    last_fix.get("action") if isinstance(last_fix, dict) else None
                ),
                "last_fix_success": last_fix.get("success") if isinstance(last_fix, dict) else None,
                "last_fix_detail": last_fix.get("detail") if isinstance(last_fix, dict) else None,
                "fix_attempts": inc.get("fix_attempts", 0),
                "detected_at": inc.get("detected_at"),
                "handled_by_agent": bool(ui.get("handled_by_agent")),
            }
        )
    fixing_first = {"fixing": 0, "escalated": 1, "open": 2}
    activity.sort(key=lambda row: fixing_first.get(str(row.get("status")), 9))
    return activity


def _build_issue_queues(enriched_incidents: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    queues: dict[str, list[dict[str, Any]]] = {
        "agent_working": [],
        "developer": [],
        "agent_resolved": [],
    }
    for inc in enriched_incidents:
        ui = inc.get("ui") if isinstance(inc.get("ui"), dict) else {}
        queue = str(ui.get("queue") or "agent_working")
        if queue not in queues:
            queue = "agent_working"
        row = {
            "incident_id": inc.get("incident_id"),
            "device_id": inc.get("device_id"),
            "display_name": inc.get("display_name"),
            "subsystem": inc.get("subsystem"),
            "agent": _agent_label(str(inc.get("subsystem") or "")),
            "status": inc.get("status"),
            "severity": inc.get("severity"),
            "error": inc.get("error"),
            "plain_english": ui.get("plain_english") or "",
            "issue_kind": ui.get("issue_kind") or "operational",
            "handled_by_agent": bool(ui.get("handled_by_agent")),
            "fix_attempts": inc.get("fix_attempts", 0),
            "detected_at": inc.get("detected_at"),
            "resolved_at": inc.get("resolved_at"),
            "report": inc.get("report") or "",
            "fixes": inc.get("fixes") or [],
            "ui": ui,
        }
        fixes = inc.get("fixes") or []
        if fixes and isinstance(fixes[-1], dict):
            row["current_action"] = fixes[-1].get("action")
            row["current_action_label"] = _action_label(fixes[-1].get("action"))
            row["last_fix_success"] = fixes[-1].get("success")
        queues[queue].append(row)

    for key in queues:
        if key == "agent_resolved":
            queues[key].sort(
                key=lambda row: str(row.get("resolved_at") or row.get("detected_at") or ""),
                reverse=True,
            )
            queues[key] = queues[key][:12]
        else:
            fixing_first = {"fixing": 0, "escalated": 1, "open": 2}
            queues[key].sort(
                key=lambda row: fixing_first.get(str(row.get("status")), 9),
            )
    return queues


def build_dashboard(
    *,
    snapshot: dict[str, Any],
    intelligent_status: dict[str, Any] | None = None,
    incidents: list[dict[str, Any]] | None = None,
    last_smoke_run: dict[str, Any] | None = None,
    voice_active_fn: Any = None,
) -> dict[str, Any]:
    """Build a single payload for the ops dashboard UI."""
    intelligent_status = intelligent_status or {}
    incidents = incidents or []
    enriched_incidents = [enrich_incident_for_ui(inc) for inc in incidents]
    smoke_by_device = _smoke_by_device(last_smoke_run)

    active_incidents = [
        inc
        for inc in enriched_incidents
        if str(inc.get("status") or "") in {"open", "fixing", "escalated"}
    ]
    recent_resolved = [
        inc
        for inc in reversed(enriched_incidents)
        if str(inc.get("status") or "") == "resolved"
    ][:10]

    device_rows = (snapshot.get("devices") or {}).get("devices") or []
    if not isinstance(device_rows, list):
        device_rows = []

    cameras = snapshot.get("cameras") or {}
    if not isinstance(cameras, dict):
        cameras = {}

    llm = snapshot.get("llm") or {}
    memory = snapshot.get("memory") or {}
    stt = snapshot.get("stt") or {}
    tts = snapshot.get("tts") or {}

    server_incidents = _incidents_for_device(enriched_incidents, "server", active_only=True)
    server_smoke = smoke_by_device.get("server", [])
    server_subsystems = {
        "llm": _subsystem_health(
            passed=next((r["passed"] for r in server_smoke if r.get("test_id") == "server:ollama_reachable"), None),
            incident_severity=next(
                (inc["severity"] for inc in server_incidents if inc.get("subsystem") == "llm"),
                None,
            ),
            live_ok=bool(llm.get("reachable")) if isinstance(llm, dict) else None,
        ),
        "memory": _subsystem_health(
            passed=next((r["passed"] for r in server_smoke if r.get("test_id") == "server:memory_ready"), None),
            incident_severity=next(
                (inc["severity"] for inc in server_incidents if inc.get("subsystem") == "memory"),
                None,
            ),
            live_ok=bool(memory.get("ready")) if isinstance(memory, dict) and memory.get("database_url_set") else None,
        ),
        "stt": _subsystem_health(
            passed=next((r["passed"] for r in server_smoke if str(r.get("test_id", "")).startswith("server:whisper")), None),
            incident_severity=next(
                (inc["severity"] for inc in server_incidents if inc.get("subsystem") == "stt"),
                None,
            ),
            live_ok=bool(stt.get("loaded")) if isinstance(stt, dict) and str(stt.get("provider") or "") == "whisper" else None,
        ),
        "tts": _subsystem_health(
            passed=next((r["passed"] for r in server_smoke if r.get("test_id") == "server:tts_healthy"), None),
            incident_severity=next(
                (inc["severity"] for inc in server_incidents if inc.get("subsystem") == "tts"),
                None,
            ),
            live_ok=not bool(tts.get("last_error")) if isinstance(tts, dict) else None,
        ),
        "voice": _subsystem_health(
            passed=next((r["passed"] for r in server_smoke if r.get("test_id") == "server:voice_ws_url"), None),
            incident_severity=next(
                (inc["severity"] for inc in server_incidents if inc.get("subsystem") == "voice"),
                None,
            ),
        ),
    }
    server_health = _worst_health(*server_subsystems.values())
    if server_incidents:
        server_health = _worst_health(
            server_health,
            "critical" if any(i.get("severity") == "critical" for i in server_incidents) else "degraded",
        )

    voice_active_global = False
    if voice_active_fn is not None:
        try:
            voice_active_global = bool(voice_active_fn())
        except Exception:
            voice_active_global = False

    server_payload = {
        "device_id": "server",
        "display_name": "NiNO Server",
        "health": server_health,
        "agent_status": _agent_status(server_incidents),
        "voice_pipeline_active": voice_active_global,
        "subsystems": server_subsystems,
        "incidents": server_incidents,
        "smoke_tests": server_smoke,
        "llm": llm if isinstance(llm, dict) else {},
        "memory": memory if isinstance(memory, dict) else {},
    }

    bots: list[dict[str, Any]] = []
    healthy_count = 0
    needs_help_count = 0

    for row in device_rows:
        if not isinstance(row, dict):
            continue
        device_id = str(row.get("device_id") or "").strip()
        if not device_id:
            continue

        display_name = str(row.get("display_name") or device_id)
        bot_incidents = _incidents_for_device(enriched_incidents, device_id, active_only=True)
        bot_smoke = smoke_by_device.get(device_id, [])

        cam = cameras.get(device_id) if isinstance(cameras, dict) else {}
        if not isinstance(cam, dict):
            cam = {}

        camera_state = classify_camera_state(
            snapshot,
            device_id,
            cam,
            voice_active_fn=voice_active_fn,
        )
        camera_connected = bool(cam.get("connected"))
        runtime = (snapshot.get("bot_runtime") or {}).get(device_id) or {}
        if not isinstance(runtime, dict):
            runtime = {}
        camera_session_active = bool(runtime.get("session_active"))
        camera_streaming = bool(runtime.get("streaming"))

        camera_live_passed = next(
            (r.get("passed") for r in bot_smoke if r.get("test_id") == f"bot:{device_id}:camera_live"),
            None,
        )
        snapshot_passed = next(
            (r.get("passed") for r in bot_smoke if r.get("test_id") == f"bot:{device_id}:snapshot_http"),
            None,
        )

        subsystems = {
            "bot": _subsystem_health(
                passed=snapshot_passed,
                incident_severity=next(
                    (inc["severity"] for inc in bot_incidents if inc.get("subsystem") == "bot"),
                    None,
                ),
                live_ok=snapshot_passed,
            ),
            "camera": _camera_subsystem_health(
                state=camera_state,
                passed=camera_live_passed,
                incident_severity=next(
                    (inc["severity"] for inc in bot_incidents if inc.get("subsystem") == "camera"),
                    None,
                ),
            ),
            "discovery": _subsystem_health(
                passed=next(
                    (r.get("passed") for r in bot_smoke if r.get("test_id") == f"bot:{device_id}:urls"),
                    None,
                ),
                incident_severity=next(
                    (inc["severity"] for inc in bot_incidents if inc.get("subsystem") == "discovery"),
                    None,
                ),
            ),
            "tts": _subsystem_health(
                passed=next(
                    (r.get("passed") for r in bot_smoke if r.get("test_id") == f"bot:{device_id}:playback_route"),
                    None,
                ),
                incident_severity=next(
                    (inc["severity"] for inc in bot_incidents if inc.get("subsystem") == "tts"),
                    None,
                ),
            ),
        }

        bot_health = _worst_health(*subsystems.values())
        if bot_incidents:
            bot_health = _worst_health(
                bot_health,
                "critical" if any(i.get("severity") == "critical" for i in bot_incidents) else "degraded",
            )

        voice_active = False
        if voice_active_fn is not None:
            try:
                voice_active = bool(voice_active_fn(device_id))
            except Exception:
                voice_active = False

        agent_status = _agent_status(bot_incidents)
        if agent_status == "idle" and bot_health in {"degraded", "critical"}:
            agent_status = "needs_help"

        if bot_health == "healthy" and not bot_incidents:
            healthy_count += 1
        else:
            needs_help_count += 1

        bots.append(
            {
                "device_id": device_id,
                "display_name": display_name,
                "health": bot_health,
                "agent_status": agent_status,
                "voice_pipeline_active": voice_active,
                "base_url": str(row.get("base_url") or ""),
                "camera_url": str(row.get("camera_url") or ""),
                "play_wav_url": str(row.get("play_wav_url") or ""),
                "camera_connected": camera_connected,
                "camera_state": camera_state,
                "camera_session_active": camera_session_active,
                "camera_streaming": camera_streaming,
                "camera_last_error": str(cam.get("last_error") or ""),
                "camera_frame_age_seconds": cam.get("last_frame_age_seconds"),
                "wifi_ssid": str(row.get("wifi_ssid") or ""),
                "wifi_rssi": row.get("wifi_rssi"),
                "subsystems": subsystems,
                "incidents": bot_incidents,
                "smoke_tests": bot_smoke,
            }
        )

    fixing_count = sum(
        1
        for inc in active_incidents
        if str(inc.get("status") or "") == "fixing"
    )
    escalated_count = sum(
        1
        for inc in active_incidents
        if str(inc.get("status") or "") == "escalated"
    )
    false_positive_count = sum(
        1
        for inc in active_incidents
        if (inc.get("ui") or {}).get("issue_kind") in {"agent_auto_fixed", "soak_false_positive"}
    )
    code_bug_count = sum(
        1
        for inc in active_incidents
        if (inc.get("ui") or {}).get("queue") == "developer"
        or (inc.get("ui") or {}).get("show_developer_issue")
    )
    agent_handling_count = sum(
        1
        for inc in active_incidents
        if (inc.get("ui") or {}).get("queue") == "agent_working"
    )
    agent_resolved_recent = [
        inc
        for inc in enriched_incidents
        if str(inc.get("status") or "") == "resolved"
        and (inc.get("ui") or {}).get("handled_by_agent")
    ][:12]
    issue_queues = _build_issue_queues(enriched_incidents)
    agent_activity = _build_agent_activity(active_incidents)

    return {
        "ok": True,
        "generated_at": _utc_now(),
        "summary": {
            "total_bots": len(bots),
            "healthy_bots": healthy_count,
            "needs_help_bots": needs_help_count,
            "open_incidents": len(active_incidents),
            "fixing_incidents": fixing_count,
            "escalated_incidents": escalated_count,
            "false_positive_incidents": false_positive_count,
            "code_bug_incidents": code_bug_count,
            "agent_handling_incidents": agent_handling_count,
            "agent_resolved_recent": len(agent_resolved_recent),
            "developer_needed_incidents": len(issue_queues.get("developer") or []),
            "server_health": server_health,
            "intelligent_mode_enabled": bool(intelligent_status.get("enabled")),
            "intelligent_mode_running": bool(intelligent_status.get("running")),
        },
        "intelligent_mode": intelligent_status,
        "server": server_payload,
        "bots": bots,
        "incidents": {
            "active": active_incidents,
            "recent_resolved": recent_resolved,
        },
        "issue_queues": issue_queues,
        "last_smoke_run": last_smoke_run,
        "last_tick": intelligent_status.get("last_tick"),
        "last_tick_at": intelligent_status.get("last_tick_at"),
        "agent_activity": agent_activity,
        "soak_test": intelligent_status.get("soak_test") or {},
        "server_runtime": intelligent_status.get("server_runtime") or {},
    }

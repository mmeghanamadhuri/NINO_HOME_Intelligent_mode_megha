"""Rule-based anomaly detection from live server snapshots."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from intelligent_mode.incidents import Incident
from intelligent_mode.session_camera import (
    camera_expects_stream,
    camera_stream_fault,
    probe_bot_snapshot,
    snapshot_acceptable,
)

VoiceActiveFn = Callable[[str | None], bool] | None

_LATENCY_PATH = Path(__file__).resolve().parent.parent / "data" / "latency_log.json"


def _recent_voice_failures(
    *,
    limit: int = 12,
    snapshot: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    if not _LATENCY_PATH.is_file():
        return []
    try:
        from intelligent_mode.voice_incident_filters import latency_failure_max_age_seconds

        max_age = latency_failure_max_age_seconds()
        raw = json.loads(_LATENCY_PATH.read_text(encoding="utf-8"))
        if not isinstance(raw, list):
            return []
        rows = [row for row in raw if isinstance(row, dict)]
        cutoff = time.time() - max_age
        recent_rows: list[dict[str, Any]] = []
        for row in rows:
            ts_raw = str(row.get("timestamp") or "").strip()
            if ts_raw:
                try:
                    ts_text = ts_raw[:-1] + "+00:00" if ts_raw.endswith("Z") else ts_raw
                    ts = datetime.fromisoformat(ts_text).timestamp()
                    if ts < cutoff:
                        continue
                except ValueError:
                    pass
            recent_rows.append(row)
        failures = [
            row
            for row in recent_rows
            if row.get("event") == "voice_query"
            and (
                str(row.get("status") or "").lower() in {"error", "failed", "fail"}
                or str(row.get("reply_path") or "").lower() in {"error", "failed", "none"}
                or bool(row.get("error"))
            )
        ]
        try:
            from intelligent_mode.voice_incident_filters import is_benign_voice_latency_row

            failures = [
                row for row in failures if not is_benign_voice_latency_row(row, snapshot)
            ]
        except Exception:
            pass
        return failures[-limit:]
    except (OSError, json.JSONDecodeError):
        return []


@dataclass(frozen=True)
class DetectionCandidate:
    device_id: str
    display_name: str
    subsystem: str
    severity: str
    tier: int
    error: str
    snapshot_hint: dict[str, Any]


class GraceTracker:
    """Require a condition to persist before opening an incident."""

    def __init__(self, grace_seconds: int) -> None:
        self._grace_seconds = grace_seconds
        self._first_seen: dict[str, float] = {}

    def ready(self, key: str, active: bool, *, grace_seconds: int | None = None) -> bool:
        now = time.time()
        if not active:
            self._first_seen.pop(key, None)
            return False
        first = self._first_seen.setdefault(key, now)
        threshold = grace_seconds if grace_seconds is not None else self._grace_seconds
        return (now - first) >= threshold

    def reset(self, key: str) -> None:
        self._first_seen.pop(key, None)


def probe_bot_http(base_url: str, *, timeout: float = 3.0) -> tuple[bool, str]:
    ok, err, _code = probe_bot_snapshot(base_url, timeout=timeout)
    return ok, err


def detect_anomalies(
    snapshot: dict[str, Any],
    *,
    grace: GraceTracker,
    camera_grace_seconds: int | None = None,
    llm_grace_seconds: int | None = None,
    voice_active_fn: VoiceActiveFn = None,
    baseline_enabled: bool = True,
    baseline_sigma: float = 3.0,
    baseline_min_samples: int = 20,
    baseline_grace_seconds: int = 120,
) -> list[DetectionCandidate]:
    out: list[DetectionCandidate] = []
    devices = snapshot.get("devices") or {}
    device_rows = devices.get("devices") if isinstance(devices, dict) else None
    if not isinstance(device_rows, list):
        device_rows = []

    cameras = snapshot.get("cameras") or {}
    if not isinstance(cameras, dict):
        cameras = {}

    for row in device_rows:
        if not isinstance(row, dict):
            continue
        device_id = str(row.get("device_id") or "").strip()
        if not device_id:
            continue
        display_name = str(row.get("display_name") or device_id)
        cam = cameras.get(device_id) if isinstance(cameras, dict) else None
        if not isinstance(cam, dict):
            cam = snapshot.get("camera") if device_id == snapshot.get("device_id") else {}
        if not isinstance(cam, dict):
            cam = {}

        cam_key = f"{device_id}:camera"
        cam_fault = camera_stream_fault(
            snapshot,
            device_id,
            cam,
            voice_active_fn=voice_active_fn,
        )
        cam_error = str(cam.get("last_error") or "").strip()
        if grace.ready(cam_key, cam_fault, grace_seconds=camera_grace_seconds):
            out.append(
                DetectionCandidate(
                    device_id=device_id,
                    display_name=display_name,
                    subsystem="camera",
                    severity="critical" if cam_error else "warning",
                    tier=0,
                    error=cam_error or "Camera stream disconnected during voice session",
                    snapshot_hint={"camera": cam},
                )
            )

        base_url = str(row.get("base_url") or row.get("effective_base_url") or "").strip()
        if not base_url:
            play = str(row.get("play_wav_url") or row.get("camera_url") or "")
            if play.startswith("http"):
                from urllib.parse import urlparse

                parsed = urlparse(play)
                if parsed.scheme and parsed.netloc:
                    base_url = f"{parsed.scheme}://{parsed.netloc}"

        if base_url:
            bot_key = f"{device_id}:bot"
            expects_stream = camera_expects_stream(
                snapshot, device_id, voice_active_fn=voice_active_fn
            )
            ok, err, http_code = probe_bot_snapshot(base_url)
            bot_fault = not snapshot_acceptable(
                ok, err, http_code, expects_stream=expects_stream
            )
            if grace.ready(bot_key, bot_fault):
                out.append(
                    DetectionCandidate(
                        device_id=device_id,
                        display_name=display_name,
                        subsystem="bot",
                        severity="critical",
                        tier=1,
                        error=err or "Robot HTTP unreachable",
                        snapshot_hint={"base_url": base_url},
                    )
                )
        else:
            missing_key = f"{device_id}:bot_url"
            if grace.ready(missing_key, True):
                out.append(
                    DetectionCandidate(
                        device_id=device_id,
                        display_name=display_name,
                        subsystem="discovery",
                        severity="warning",
                        tier=1,
                        error="No base URL configured for robot",
                        snapshot_hint={"device": row},
                    )
                )

    llm = snapshot.get("llm") or {}
    if isinstance(llm, dict):
        llm_key = "server:llm"
        llm_down = not bool(llm.get("reachable"))
        llm_error = str(llm.get("warning") or "").strip()
        if grace.ready(llm_key, llm_down, grace_seconds=llm_grace_seconds):
            out.append(
                DetectionCandidate(
                    device_id="server",
                    display_name="NiNO Server",
                    subsystem="llm",
                    severity="critical",
                    tier=0,
                    error=llm_error or "Ollama LLM unreachable",
                    snapshot_hint={"llm": llm},
                )
            )

    memory = snapshot.get("memory") or {}
    if isinstance(memory, dict) and memory.get("database_url_set") and not memory.get("ready"):
        mem_key = "server:memory"
        mem_error = str(memory.get("last_error") or "PostgreSQL memory unavailable")
        if grace.ready(mem_key, True):
            out.append(
                DetectionCandidate(
                    device_id="server",
                    display_name="NiNO Server",
                    subsystem="memory",
                    severity="warning",
                    tier=1,
                    error=mem_error,
                    snapshot_hint={"memory": memory},
                )
            )

    stt = snapshot.get("stt") or {}
    if isinstance(stt, dict) and str(stt.get("provider") or "") == "whisper":
        stt_key = "server:stt"
        stt_down = not bool(stt.get("loaded"))
        if grace.ready(stt_key, stt_down):
            out.append(
                DetectionCandidate(
                    device_id="server",
                    display_name="NiNO Server",
                    subsystem="stt",
                    severity="warning",
                    tier=0,
                    error="Whisper STT model not loaded",
                    snapshot_hint={"stt": stt},
                )
            )

    tts = snapshot.get("tts") or {}
    if isinstance(tts, dict):
        tts_error = str(tts.get("last_error") or "").strip()
        tts_key = "server:tts"
        if grace.ready(tts_key, bool(tts_error)):
            out.append(
                DetectionCandidate(
                    device_id="server",
                    display_name="NiNO Server",
                    subsystem="tts",
                    severity="warning",
                    tier=1,
                    error=tts_error,
                    snapshot_hint={"tts": tts},
                )
            )

    discovery = snapshot.get("discovery") or {}
    if isinstance(discovery, dict):
        disc_error = str(discovery.get("last_error") or "").strip()
        disc_key = "server:discovery"
        if grace.ready(disc_key, bool(disc_error)):
            out.append(
                DetectionCandidate(
                    device_id="server",
                    display_name="NiNO Server",
                    subsystem="discovery",
                    severity="warning",
                    tier=1,
                    error=disc_error,
                    snapshot_hint={"discovery": discovery},
                )
            )

    voice_failures = _recent_voice_failures(snapshot=snapshot)
    if voice_failures:
        latest = voice_failures[-1]
        device_id = str(latest.get("device_id") or "server").strip() or "server"
        err = str(
            latest.get("error")
            or latest.get("reply_path")
            or "Voice pipeline error in recent latency log"
        ).strip()
        from intelligent_mode.voice_incident_filters import (
            is_stt_empty_error,
            should_open_stt_empty_incident,
        )

        if is_stt_empty_error(err) and device_id != "server":
            if not should_open_stt_empty_incident(device_id):
                voice_failures = []
        if voice_failures:
            voice_key = f"{device_id}:voice:{err[:48]}"
            if grace.ready(voice_key, True):
                out.append(
                    DetectionCandidate(
                        device_id=device_id,
                        display_name=device_id if device_id != "server" else "NiNO Server",
                        subsystem="voice",
                        severity="warning",
                        tier=1,
                        error=err,
                        snapshot_hint={"voice_failure": latest},
                    )
                )

    if baseline_enabled:
        try:
            from intelligent_mode.baselines import detect_baseline_anomalies

            out.extend(
                detect_baseline_anomalies(
                    snapshot,
                    grace=grace,
                    sigma=baseline_sigma,
                    min_samples=baseline_min_samples,
                    grace_seconds=baseline_grace_seconds,
                )
            )
        except Exception:
            pass

    return out


def candidate_to_incident(candidate: DetectionCandidate, snapshot: dict[str, Any]) -> Incident:
    return Incident(
        device_id=candidate.device_id,
        display_name=candidate.display_name,
        subsystem=candidate.subsystem,
        severity=candidate.severity,
        tier=candidate.tier,
        error=candidate.error,
        signature=Incident.make_signature(
            candidate.device_id, candidate.subsystem, candidate.error
        ),
        before_snapshot={
            "hint": candidate.snapshot_hint,
            "device_id": snapshot.get("device_id"),
        },
    )

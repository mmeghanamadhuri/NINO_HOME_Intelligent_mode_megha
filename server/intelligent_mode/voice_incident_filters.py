"""Suppress recurring false-positive voice incidents (CPU Ollama, wake reject, soak skip)."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_LATENCY_PATH = Path(__file__).resolve().parent.parent / "data" / "latency_log.json"

_BENIGN_REPLY_PATHS = frozenset(
    {
        "wake_reject",
        "session_confirm",
    }
)

_STT_EMPTY_PATHS = frozenset({"stt_empty", "stt_silent", "stt_rejected"})

_STT_EMPTY_ERROR_TOKENS = (
    "no speech",
    "stt empty",
    "stt_empty",
    "stt path=stt_empty",
    "stt path=stt_silent",
    "stt path=stt_rejected",
)


def stt_empty_incident_threshold() -> int:
    try:
        return max(2, int(os.environ.get("INTELLIGENT_STT_EMPTY_THRESHOLD", "3")))
    except (TypeError, ValueError):
        return 3


def stt_empty_window_seconds() -> int:
    try:
        return max(60, int(os.environ.get("INTELLIGENT_STT_EMPTY_WINDOW_SECONDS", "600")))
    except (TypeError, ValueError):
        return 600


def latency_failure_max_age_seconds() -> int:
    try:
        return max(60, int(os.environ.get("INTELLIGENT_LATENCY_FAILURE_MAX_AGE_SECONDS", "900")))
    except (TypeError, ValueError):
        return 900


def is_stt_empty_error(text: str) -> bool:
    lowered = str(text or "").lower()
    return any(token in lowered for token in _STT_EMPTY_ERROR_TOKENS)


def is_stt_empty_latency_row(row: dict[str, Any]) -> bool:
    path = str(row.get("reply_path") or "").lower()
    if path in _STT_EMPTY_PATHS:
        return True
    return is_stt_empty_error(str(row.get("error") or ""))


def _parse_latency_timestamp(raw: object) -> float | None:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except ValueError:
        return None


def _latency_rows(*, max_age_seconds: int | None = None) -> list[dict[str, Any]]:
    if not _LATENCY_PATH.is_file():
        return []
    try:
        raw = json.loads(_LATENCY_PATH.read_text(encoding="utf-8"))
        if not isinstance(raw, list):
            return []
        rows = [row for row in raw if isinstance(row, dict)]
    except (OSError, json.JSONDecodeError):
        return []

    if max_age_seconds is None:
        return rows

    cutoff = datetime.now(timezone.utc).timestamp() - max_age_seconds
    out: list[dict[str, Any]] = []
    for row in rows:
        ts = _parse_latency_timestamp(row.get("timestamp"))
        if ts is None or ts >= cutoff:
            out.append(row)
    return out


def count_recent_stt_empty(
    device_id: str | None = None,
    *,
    window_seconds: int | None = None,
) -> int:
    """Count STT-empty voice_query rows in the latency log within the window."""
    window = window_seconds if window_seconds is not None else stt_empty_window_seconds()
    cutoff = datetime.now(timezone.utc).timestamp() - window
    device = str(device_id or "").strip().lower()
    count = 0
    for row in _latency_rows():
        if str(row.get("event") or "") != "voice_query":
            continue
        ts = _parse_latency_timestamp(row.get("timestamp"))
        if ts is not None and ts < cutoff:
            continue
        if device:
            row_device = str(row.get("device_id") or "").strip().lower()
            if row_device and row_device != device:
                continue
        if is_stt_empty_latency_row(row):
            count += 1
    return count


def device_has_recent_stt_empty(
    device_id: str,
    *,
    window_seconds: int = 300,
) -> bool:
    return count_recent_stt_empty(device_id, window_seconds=window_seconds) > 0


def should_open_stt_empty_incident(device_id: str) -> bool:
    """Open STT-empty incidents only when failures repeat — not for one-off silence."""
    return count_recent_stt_empty(device_id) >= stt_empty_incident_threshold()


def ollama_gpu_healthy(snapshot: dict[str, Any] | None) -> bool:
    llm = (snapshot or {}).get("llm") if isinstance(snapshot, dict) else None
    if not isinstance(llm, dict):
        return False
    return bool(llm.get("reachable"))


def is_ollama_cpu_port_error(text: str) -> bool:
    """True when the error is only about CPU Ollama on 11434 (optional fallback)."""
    lowered = str(text or "").lower()
    if "11434" not in lowered:
        return False
    return any(
        token in lowered
        for token in (
            "connection refused",
            "max retries exceeded",
            "failed to establish a new connection",
            "network request failed",
        )
    )


def is_soak_live_session_skip(error: str) -> bool:
    lowered = str(error or "").lower()
    return "skipped" in lowered and "live voice session active" in lowered


def is_benign_voice_latency_row(
    row: dict[str, Any],
    snapshot: dict[str, Any] | None = None,
) -> bool:
    """Latency log rows that must not open voice incidents."""
    path = str(row.get("reply_path") or "").lower()
    if path in _BENIGN_REPLY_PATHS:
        return True

    error = str(row.get("error") or "")
    if is_ollama_cpu_port_error(error) and ollama_gpu_healthy(snapshot):
        return True

    if path in {"error", "failed", "none"} and not error:
        return True

    if is_stt_empty_latency_row(row):
        device_id = str(row.get("device_id") or "").strip()
        if device_id and not should_open_stt_empty_incident(device_id):
            return True

    return False


def is_suppressed_voice_error(error: str, snapshot: dict[str, Any] | None = None) -> bool:
    """Errors that look like voice failures but are benign in this environment."""
    raw = str(error or "").strip()
    if not raw:
        return False

    lowered = raw.lower()
    if "wake_reject" in lowered:
        return True
    if is_soak_live_session_skip(raw):
        return True
    if is_ollama_cpu_port_error(raw) and ollama_gpu_healthy(snapshot):
        return True

    return False


def should_suppress_voice_incident(
    error: str,
    subsystem: str,
    snapshot: dict[str, Any] | None = None,
    *,
    device_id: str | None = None,
) -> bool:
    if str(subsystem or "").lower() != "voice":
        return False
    if is_suppressed_voice_error(error, snapshot):
        return True
    if is_stt_empty_error(error):
        dev = str(device_id or "").strip()
        if dev and not should_open_stt_empty_incident(dev):
            return True
    return False

"""Rolling metric baselines and statistical anomaly detection."""

from __future__ import annotations

import json
import logging
import math
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from intelligent_mode.detectors import DetectionCandidate, GraceTracker

logger = logging.getLogger(__name__)

_BASELINE_PATH = Path(__file__).resolve().parent.parent / "data" / "intelligent_baselines.json"
_LATENCY_PATH = Path(__file__).resolve().parent.parent / "data" / "latency_log.json"
_MAX_SAMPLES = 400


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class BaselineStats:
    key: str
    count: int
    mean: float
    std: float
    latest: float

    def z_score(self, value: float) -> float:
        if self.count < 2 or self.std <= 0:
            return 0.0
        return (value - self.mean) / self.std


class BaselineStore:
    def __init__(self, path: Path | None = None) -> None:
        self._path = path or _BASELINE_PATH
        self._lock = threading.RLock()
        self._series: dict[str, list[float]] = {}
        self._reload()

    def _reload(self) -> None:
        if not self._path.is_file():
            self._series = {}
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            series = raw.get("series") if isinstance(raw, dict) else {}
            if not isinstance(series, dict):
                self._series = {}
                return
            cleaned: dict[str, list[float]] = {}
            for key, values in series.items():
                if not isinstance(values, list):
                    continue
                nums = [float(v) for v in values if isinstance(v, (int, float))]
                cleaned[str(key)] = nums[-_MAX_SAMPLES:]
            self._series = cleaned
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            self._series = {}

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "updated_at": _utc_now(),
            "series": {key: values[-_MAX_SAMPLES:] for key, values in self._series.items()},
        }
        self._path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def record(self, key: str, value: float) -> None:
        if not math.isfinite(value) or value < 0:
            return
        with self._lock:
            bucket = self._series.setdefault(key, [])
            bucket.append(float(value))
            if len(bucket) > _MAX_SAMPLES:
                del bucket[: len(bucket) - _MAX_SAMPLES]
            self._save()

    def stats(self, key: str) -> BaselineStats | None:
        with self._lock:
            values = list(self._series.get(key) or [])
        if not values:
            return None
        count = len(values)
        mean = sum(values) / count
        if count < 2:
            return BaselineStats(key=key, count=count, mean=mean, std=0.0, latest=values[-1])
        variance = sum((v - mean) ** 2 for v in values) / count
        std = math.sqrt(variance)
        return BaselineStats(key=key, count=count, mean=mean, std=std, latest=values[-1])

    def is_anomaly(
        self,
        key: str,
        value: float,
        *,
        sigma: float = 3.0,
        min_samples: int = 20,
    ) -> tuple[bool, BaselineStats | None, float]:
        stats = self.stats(key)
        if stats is None or stats.count < min_samples:
            return False, stats, 0.0
        z = stats.z_score(value)
        return z >= sigma, stats, z


_STORE = BaselineStore()


def get_baseline_store() -> BaselineStore:
    return _STORE


def _recent_latency_rows(*, limit: int = 40) -> list[dict[str, Any]]:
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


def record_from_snapshot(
    snapshot: dict[str, Any],
    *,
    smoke_run: dict[str, Any] | None = None,
    store: BaselineStore | None = None,
) -> None:
    """Append recent metric samples from live status and smoke results."""
    target = store or get_baseline_store()

    for row in _recent_latency_rows(limit=12):
        if row.get("event") != "voice_query":
            continue
        device_id = str(row.get("device_id") or "server").strip() or "server"
        total = row.get("server_total_seconds") or row.get("process_total_seconds")
        stt = row.get("stt_seconds")
        if isinstance(total, (int, float)):
            target.record(f"{device_id}:voice_total_s", float(total))
        if isinstance(stt, (int, float)):
            target.record(f"{device_id}:voice_stt_s", float(stt))

    if isinstance(smoke_run, dict):
        for result in smoke_run.get("results") or []:
            if not isinstance(result, dict):
                continue
            test_id = str(result.get("test_id") or "")
            duration_ms = result.get("duration_ms")
            if not test_id or not isinstance(duration_ms, (int, float)):
                continue
            device_id = str(result.get("device_id") or "server")
            target.record(f"{device_id}:smoke:{test_id}:ms", float(duration_ms))

        total = smoke_run.get("total")
        failed = smoke_run.get("failed")
        if isinstance(total, int) and isinstance(failed, int) and total > 0:
            target.record("server:smoke_fail_ratio", failed / total)

    devices = (snapshot.get("devices") or {}).get("devices") or []
    for row in devices:
        if not isinstance(row, dict):
            continue
        device_id = str(row.get("device_id") or "").strip()
        base_url = str(row.get("base_url") or row.get("effective_base_url") or "").strip()
        if not device_id or not base_url:
            continue
        try:
            from intelligent_mode.session_camera import probe_bot_snapshot

            start = time.perf_counter()
            ok, _err, _code = probe_bot_snapshot(base_url, timeout=2.5)
            elapsed_ms = (time.perf_counter() - start) * 1000.0
            if ok:
                target.record(f"{device_id}:bot_probe_ms", elapsed_ms)
        except Exception:
            pass


def detect_baseline_anomalies(
    snapshot: dict[str, Any],
    *,
    grace: GraceTracker,
    store: BaselineStore | None = None,
    sigma: float = 3.0,
    min_samples: int = 20,
    grace_seconds: int = 120,
) -> list[DetectionCandidate]:
    """Flag metrics that exceed per-device rolling baselines."""
    target = store or get_baseline_store()
    out: list[DetectionCandidate] = []

    devices = (snapshot.get("devices") or {}).get("devices") or []
    device_names = {
        str(row.get("device_id") or ""): str(row.get("display_name") or row.get("device_id") or "")
        for row in devices
        if isinstance(row, dict)
    }

    for row in _recent_latency_rows(limit=8):
        if row.get("event") != "voice_query":
            continue
        device_id = str(row.get("device_id") or "server").strip() or "server"
        total = row.get("server_total_seconds") or row.get("process_total_seconds")
        if not isinstance(total, (int, float)):
            continue
        key = f"{device_id}:voice_total_s"
        is_bad, stats, z = target.is_anomaly(
            key, float(total), sigma=sigma, min_samples=min_samples
        )
        grace_key = f"baseline:{key}"
        if is_bad and stats is not None and grace.ready(grace_key, True, grace_seconds=grace_seconds):
            display = device_names.get(device_id) or (
                "NiNO Server" if device_id == "server" else device_id
            )
            out.append(
                DetectionCandidate(
                    device_id=device_id,
                    display_name=display,
                    subsystem="voice",
                    severity="warning",
                    tier=1,
                    error=(
                        f"Voice latency baseline anomaly: {float(total):.2f}s "
                        f"(mean={stats.mean:.2f}s, z={z:.1f})"
                    ),
                    snapshot_hint={"baseline_key": key, "z_score": z, "value": float(total)},
                )
            )
        elif not is_bad:
            grace.reset(grace_key)

    fail_stats = target.stats("server:smoke_fail_ratio")
    if fail_stats and fail_stats.count >= min_samples:
        current = fail_stats.latest
        is_bad, _stats, z = target.is_anomaly(
            "server:smoke_fail_ratio", current, sigma=sigma, min_samples=min_samples
        )
        grace_key = "baseline:server:smoke_fail_ratio"
        if is_bad and grace.ready(grace_key, True, grace_seconds=grace_seconds):
            out.append(
                DetectionCandidate(
                    device_id="server",
                    display_name="NiNO Server",
                    subsystem="server",
                    severity="warning",
                    tier=1,
                    error=(
                        f"Smoke failure ratio baseline anomaly: {current:.2f} "
                        f"(mean={fail_stats.mean:.2f}, z={z:.1f})"
                    ),
                    snapshot_hint={"baseline_key": "server:smoke_fail_ratio", "z_score": z},
                )
            )
        elif not is_bad:
            grace.reset(grace_key)

    for row in devices:
        if not isinstance(row, dict):
            continue
        device_id = str(row.get("device_id") or "").strip()
        base_url = str(row.get("base_url") or row.get("effective_base_url") or "").strip()
        if not device_id or not base_url:
            continue
        try:
            from intelligent_mode.session_camera import probe_bot_snapshot

            start = time.perf_counter()
            ok, err, _code = probe_bot_snapshot(base_url, timeout=2.5)
            elapsed_ms = (time.perf_counter() - start) * 1000.0
        except Exception as exc:
            ok, err, elapsed_ms = False, str(exc), 0.0

        key = f"{device_id}:bot_probe_ms"
        if ok:
            is_bad, stats, z = target.is_anomaly(
                key, elapsed_ms, sigma=sigma, min_samples=min_samples
            )
            grace_key = f"baseline:{key}"
            if is_bad and stats is not None and grace.ready(grace_key, True, grace_seconds=grace_seconds):
                display = device_names.get(device_id) or device_id
                out.append(
                    DetectionCandidate(
                        device_id=device_id,
                        display_name=display,
                        subsystem="bot",
                        severity="warning",
                        tier=1,
                        error=(
                            f"Bot HTTP probe latency anomaly: {elapsed_ms:.0f}ms "
                            f"(mean={stats.mean:.0f}ms, z={z:.1f})"
                        ),
                        snapshot_hint={"baseline_key": key, "z_score": z, "probe_ms": elapsed_ms},
                    )
                )
            elif not is_bad:
                grace.reset(grace_key)
        else:
            grace.reset(f"baseline:{key}")

    return out

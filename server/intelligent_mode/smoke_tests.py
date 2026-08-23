"""Active smoke tests — proactively probe each bot and server subsystem."""

from __future__ import annotations

import time
import uuid
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from intelligent_mode.detectors import DetectionCandidate, probe_bot_http
from intelligent_mode.session_camera import (
    camera_expects_stream,
    camera_stream_fault,
    classify_camera_state,
    probe_bot_snapshot,
    snapshot_acceptable,
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class SmokeTestResult:
    test_id: str
    name: str
    device_id: str
    subsystem: str
    passed: bool
    message: str
    duration_ms: float = 0.0
    severity: str = "warning"
    tier: int = 1
    skipped: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SmokeTestRun:
    run_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    started_at: str = field(default_factory=_utc_now)
    finished_at: str = ""
    passed: int = 0
    failed: int = 0
    total: int = 0
    results: list[SmokeTestResult] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "passed": self.passed,
            "failed": self.failed,
            "total": self.total,
            "ok": self.failed == 0,
            "results": [r.to_dict() for r in self.results],
        }


def _http_get_ok(url: str, *, timeout: float = 3.0) -> tuple[bool, str]:
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if 200 <= resp.status < 300:
                return True, ""
            return False, f"HTTP {resp.status}"
    except urllib.error.HTTPError as exc:
        return False, f"HTTP {exc.code}"
    except Exception as exc:
        return False, str(exc)


def _run(name: str, fn: Callable[[], tuple[bool | None, str]], **meta: Any) -> SmokeTestResult:
    started = time.perf_counter()
    outcome, message = fn()
    skipped = outcome is None
    passed = bool(outcome) if not skipped else False
    elapsed = (time.perf_counter() - started) * 1000.0
    return SmokeTestResult(
        test_id=str(meta.get("test_id") or name),
        name=name,
        device_id=str(meta.get("device_id") or "server"),
        subsystem=str(meta.get("subsystem") or "server"),
        passed=passed,
        message=message if message else ("ok" if passed else "failed"),
        duration_ms=round(elapsed, 2),
        severity=str(meta.get("severity") or ("warning" if passed else "critical")),
        tier=int(meta.get("tier") or 1),
        skipped=skipped,
    )


def run_smoke_suite(
    snapshot: dict[str, Any],
    *,
    voice_active_fn: Callable[[str | None], bool] | None = None,
) -> SmokeTestRun:
    """Run all smoke tests against a live status snapshot."""
    run = SmokeTestRun()
    results: list[SmokeTestResult] = []

    llm = snapshot.get("llm") or {}
    if isinstance(llm, dict):
        base = str(llm.get("base_url") or "").strip()

        def _ollama_reachable() -> tuple[bool, str]:
            if llm.get("reachable"):
                return True, f"Ollama reachable at {base or 'configured URL'}"
            return False, str(llm.get("warning") or "Ollama unreachable")

        results.append(
            _run(
                "server:ollama_reachable",
                _ollama_reachable,
                test_id="server:ollama_reachable",
                device_id="server",
                subsystem="llm",
                severity="critical",
                tier=0,
            )
        )

        def _ollama_loaded() -> tuple[bool, str]:
            if not llm.get("reachable"):
                return False, "skipped — Ollama unreachable"
            if llm.get("loaded"):
                return True, f"Model {llm.get('model')} loaded"
            return False, str(llm.get("warning") or "Model not loaded yet")

        results.append(
            _run(
                "server:ollama_model_loaded",
                _ollama_loaded,
                test_id="server:ollama_model_loaded",
                device_id="server",
                subsystem="llm",
                severity="warning",
                tier=0,
            )
        )

    stt = snapshot.get("stt") or {}
    if isinstance(stt, dict) and str(stt.get("provider") or "") == "whisper":

        def _whisper_loaded() -> tuple[bool, str]:
            if stt.get("loaded"):
                return True, f"Whisper {stt.get('model')} on {stt.get('device')}"
            return False, "Whisper model not loaded"

        results.append(
            _run(
                "server:whisper_loaded",
                _whisper_loaded,
                test_id="server:whisper_loaded",
                device_id="server",
                subsystem="stt",
                severity="warning",
                tier=0,
            )
        )

    memory = snapshot.get("memory") or {}
    if isinstance(memory, dict) and memory.get("database_url_set"):

        def _memory_ready() -> tuple[bool, str]:
            if memory.get("ready"):
                return True, "PostgreSQL memory ready"
            return False, str(memory.get("last_error") or "Memory not ready")

        results.append(
            _run(
                "server:memory_ready",
                _memory_ready,
                test_id="server:memory_ready",
                device_id="server",
                subsystem="memory",
                severity="warning",
                tier=1,
            )
        )

    tts = snapshot.get("tts") or {}
    if isinstance(tts, dict):

        def _tts_clean() -> tuple[bool, str]:
            err = str(tts.get("last_error") or "").strip()
            if err:
                return False, err
            return True, "No TTS errors"

        results.append(
            _run(
                "server:tts_healthy",
                _tts_clean,
                test_id="server:tts_healthy",
                device_id="server",
                subsystem="tts",
                severity="warning",
                tier=1,
            )
        )

    def _voice_ws_configured() -> tuple[bool, str]:
        from network_util import voice_ws_url_for_esp

        url = voice_ws_url_for_esp()
        if url:
            return True, url
        return False, "VOICE_WS_URL / NINO_SERVER_LAN_HOST not configured"

    results.append(
        _run(
            "server:voice_ws_url",
            _voice_ws_configured,
            test_id="server:voice_ws_url",
            device_id="server",
            subsystem="voice",
            severity="warning",
            tier=1,
        )
    )

    devices = snapshot.get("devices") or {}
    device_rows = devices.get("devices") if isinstance(devices, dict) else []
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
        display = str(row.get("display_name") or device_id)
        base_url = str(row.get("base_url") or "").strip()
        camera_url = str(row.get("camera_url") or "").strip()
        play_url = str(row.get("play_wav_url") or "").strip()

        def _urls_ok(did: str = device_id, cam: str = camera_url, play: str = play_url, base: str = base_url) -> tuple[bool, str]:
            missing = []
            if not cam and not base:
                missing.append("camera_url")
            if not play and not base:
                missing.append("play_wav_url")
            if missing:
                return False, f"Missing: {', '.join(missing)}"
            return True, f"URLs configured for {did}"

        results.append(
            _run(
                f"bot:{device_id}:urls",
                _urls_ok,
                test_id=f"bot:{device_id}:urls",
                device_id=device_id,
                subsystem="discovery",
                severity="warning",
                tier=1,
            )
        )

        if base_url:
            expects_stream = camera_expects_stream(
                snapshot, device_id, voice_active_fn=voice_active_fn
            )

            def _snapshot_http(
                url: str = base_url,
                label: str = display,
                expects: bool = expects_stream,
            ) -> tuple[bool, str]:
                ok, err, http_code = probe_bot_snapshot(url)
                if snapshot_acceptable(ok, err, http_code, expects_stream=expects):
                    if ok:
                        return True, f"{label} snapshot OK"
                    return True, f"{label} snapshot idle (503 expected, no voice session)"
                return False, err or "snapshot failed"

            results.append(
                _run(
                    f"bot:{device_id}:snapshot_http",
                    _snapshot_http,
                    test_id=f"bot:{device_id}:snapshot_http",
                    device_id=device_id,
                    subsystem="bot",
                    severity="critical",
                    tier=1,
                )
            )

        cam = cameras.get(device_id) if isinstance(cameras, dict) else None
        if not isinstance(cam, dict):
            cam = {}

        expects_stream = camera_expects_stream(
            snapshot, device_id, voice_active_fn=voice_active_fn
        )

        def _camera_connected(
            status: dict[str, Any] = cam,
            label: str = display,
            expects: bool = expects_stream,
        ) -> tuple[bool, str]:
            state = classify_camera_state(
                snapshot,
                device_id,
                status,
                voice_active_fn=voice_active_fn,
            )
            if state == "live":
                age = status.get("last_frame_age_seconds")
                return True, f"{label} camera live (age={age}s)"
            if state == "idle":
                return True, f"{label} camera idle (session off)"
            if state == "in_session":
                return False, f"{label} camera session active but no frames yet"
            err = str(status.get("last_error") or "").strip()
            if expects and camera_stream_fault(
                snapshot, device_id, status, voice_active_fn=voice_active_fn
            ):
                return False, err or f"{label} camera disconnected during session"
            return False, err or f"{label} camera fault"

        results.append(
            _run(
                f"bot:{device_id}:camera_live",
                _camera_connected,
                test_id=f"bot:{device_id}:camera_live",
                device_id=device_id,
                subsystem="camera",
                severity="critical",
                tier=0,
            )
        )

        if play_url or base_url:

            def _playback_route(url: str = play_url, base: str = base_url) -> tuple[bool, str]:
                target = url or (base.rstrip("/") + "/play_wav" if base else "")
                if not target:
                    return False, "No playback URL"
                ok, err = _http_get_ok(target)
                if ok:
                    return True, "Playback route reachable"
                if err and "405" in err:
                    return True, "Playback route exists (POST-only)"
                return False, err or "Playback route unreachable"

            results.append(
                _run(
                    f"bot:{device_id}:playback_route",
                    _playback_route,
                    test_id=f"bot:{device_id}:playback_route",
                    device_id=device_id,
                    subsystem="tts",
                    severity="warning",
                    tier=1,
                )
            )

    run.results = results
    run.total = len(results)
    run.passed = sum(1 for r in results if r.passed)
    run.failed = run.total - run.passed
    run.finished_at = _utc_now()
    return run


def failures_to_candidates(
    run: SmokeTestRun,
    *,
    device_names: dict[str, str] | None = None,
) -> list[DetectionCandidate]:
    """Convert failed smoke tests into incident candidates."""
    names = device_names or {}
    out: list[DetectionCandidate] = []
    for result in run.results:
        if result.passed or result.skipped:
            continue
        from intelligent_mode.incident_filters import is_test_skip_message

        if is_test_skip_message(result.message):
            continue
        display = names.get(result.device_id) or (
            "NiNO Server" if result.device_id == "server" else result.device_id
        )
        out.append(
            DetectionCandidate(
                device_id=result.device_id,
                display_name=display,
                subsystem=result.subsystem,
                severity=result.severity,
                tier=result.tier,
                error=f"[smoke:{result.test_id}] {result.message}",
                snapshot_hint={"smoke_test": result.to_dict()},
            )
        )
    return out


def run_smoke_for_device(
    snapshot: dict[str, Any],
    device_id: str,
    *,
    voice_active_fn: Callable[[str | None], bool] | None = None,
) -> SmokeTestRun:
    """Run only server-global + one bot's smoke tests (post-fix verification)."""
    full = run_smoke_suite(snapshot, voice_active_fn=voice_active_fn)
    filtered = [
        r
        for r in full.results
        if r.device_id in {device_id, "server"}
        and (
            r.device_id == "server"
            or r.test_id.startswith(f"bot:{device_id}:")
        )
    ]
    run = SmokeTestRun(
        run_id=full.run_id,
        started_at=full.started_at,
        finished_at=full.finished_at,
    )
    run.results = filtered
    run.total = len(filtered)
    run.passed = sum(1 for r in filtered if r.passed)
    run.failed = run.total - run.passed
    return run


def device_smoke_passed(
    snapshot: dict[str, Any],
    device_id: str,
    *,
    voice_active_fn: Callable[[str | None], bool] | None = None,
    subsystem: str | None = None,
) -> bool:
    if device_id == "server":
        run = run_smoke_suite(snapshot, voice_active_fn=voice_active_fn)
        return run.failed == 0
    run = run_smoke_for_device(snapshot, device_id, voice_active_fn=voice_active_fn)
    server_results = [r for r in run.results if r.device_id == "server"]
    bot_results = [r for r in run.results if r.device_id == device_id]
    sub = str(subsystem or "").lower()
    needs_server = sub in {"voice", "llm", "stt", "tts"}
    if needs_server and any(not r.passed for r in server_results):
        return False
    if not bot_results:
        return True if not needs_server else not run.failed
    bot_ok = all(r.passed for r in bot_results)
    if needs_server:
        return bot_ok and all(r.passed for r in server_results)
    return bot_ok

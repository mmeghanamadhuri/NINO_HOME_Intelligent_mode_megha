"""Specialist workers — each handles one subsystem with whitelisted fixes."""

from __future__ import annotations

import logging
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from intelligent_mode.context import IntelligentContext, get_context
from intelligent_mode.incidents import FixAttempt, Incident
from intelligent_mode.recovery import (
    RECOVERY_FIX_ACTIONS,
    chain_exhausted,
    dispatch_recovery_action,
    next_recovery_action,
)
from intelligent_mode.session_camera import (
    camera_expects_stream,
    camera_stream_fault,
    probe_bot_snapshot,
    snapshot_acceptable,
)

logger = logging.getLogger(__name__)

# Whitelisted fix action names — LLM cannot add new actions; workers only.
ALLOWED_FIX_ACTIONS = RECOVERY_FIX_ACTIONS


@dataclass
class FixResult:
    action: str
    success: bool
    detail: str


class BaseWorker:
    name: str = "base"
    subsystems: tuple[str, ...] = ()

    def handles(self, incident: Incident) -> bool:
        return incident.subsystem in self.subsystems

    def try_fix(self, incident: Incident, ctx: IntelligentContext) -> FixResult:
        raise NotImplementedError


class CameraWorker(BaseWorker):
    name = "camera"
    subsystems = ("camera",)

    def try_fix(self, incident: Incident, ctx: IntelligentContext) -> FixResult:
        device_id = incident.device_id
        snapshot = ctx.collect_status()
        voice_active_fn = ctx.voice_active_fn
        if not camera_expects_stream(snapshot, device_id, voice_active_fn=voice_active_fn):
            return FixResult(
                "none",
                True,
                f"[{device_id}] camera idle by design (no voice session); no fix needed",
            )
        record = ctx.registry.get(device_id)
        if record is None:
            return FixResult("camera_restart", False, f"Device {device_id} not in registry")
        source = record.effective_camera_url()
        if not source:
            return FixResult("camera_restart", False, f"No camera URL for {device_id}")
        try:
            ctx.cameras.restart(device_id, source)
            cam = ctx.cameras.status(device_id)
            ok = bool(cam.get("connected"))
            return FixResult(
                "camera_restart",
                ok,
                f"[{device_id}] restarted {source}; connected={ok}",
            )
        except Exception as exc:
            return FixResult("camera_restart", False, str(exc))


class DiscoveryWorker(BaseWorker):
    name = "discovery"
    subsystems = ("discovery", "bot")

    def try_fix(self, incident: Incident, ctx: IntelligentContext) -> FixResult:
        from device_discovery import discover_once

        device_id = incident.device_id
        try:
            found = discover_once(replace_registry=False)
            if ctx.on_registry_updated:
                ctx.on_registry_updated()
            else:
                ctx.cameras.configure_from_registry()

            ids = [d.device_id for d in found]
            detail = f"LAN discovery found {len(ids)} device(s)"

            if device_id != "server":
                record = ctx.registry.get(device_id)
                if record and record.effective_camera_url():
                    try:
                        ctx.cameras.restart(device_id, record.effective_camera_url())
                        detail += f"; restarted camera for {device_id}"
                    except Exception as exc:
                        detail += f"; camera restart failed: {exc}"

                base = record.effective_base_url() if record else ""
                if base:
                    expects = camera_expects_stream(
                        ctx.collect_status(), device_id, voice_active_fn=ctx.voice_active_fn
                    )
                    ok, err, http_code = probe_bot_snapshot(base)
                    probe_ok = snapshot_acceptable(
                        ok, err, http_code, expects_stream=expects
                    )
                    return FixResult(
                        "lan_discovery",
                        probe_ok,
                        f"[{device_id}] {detail}; probe={probe_ok}",
                    )
                return FixResult("lan_discovery", False, f"[{device_id}] {detail}; no base URL")

            return FixResult("lan_discovery", bool(ids), detail)
        except Exception as exc:
            return FixResult("lan_discovery", False, str(exc))


class LlmWorker(BaseWorker):
    name = "llm"
    subsystems = ("llm",)

    def try_fix(self, incident: Incident, ctx: IntelligentContext) -> FixResult:
        from llm_service import (
            ollama_runtime_status,
            resolve_ollama_api_url,
            set_ollama_env_url,
            try_start_gpu_ollama,
            warm_ollama_model,
        )

        try:
            try_start_gpu_ollama()
            api_url = set_ollama_env_url(resolve_ollama_api_url())
            warm_ollama_model(timeout_s=30, api_url=api_url)
            status = ollama_runtime_status(api_url=api_url)
            ok = bool(status.get("reachable"))
            return FixResult(
                "ollama_restart_warm",
                ok,
                f"Ollama reachable={ok} at {api_url}; loaded={status.get('loaded')}",
            )
        except Exception as exc:
            return FixResult("ollama_restart_warm", False, str(exc))


class MemoryWorker(BaseWorker):
    name = "memory"
    subsystems = ("memory",)

    def try_fix(self, incident: Incident, ctx: IntelligentContext) -> FixResult:
        from memory_service import get_memory_service

        try:
            service = get_memory_service()
            service.startup()
            status = service.status()
            ok = bool(status.get("ready"))
            return FixResult(
                "memory_reconnect",
                ok,
                f"Memory ready={ok}; error={status.get('last_error') or 'none'}",
            )
        except Exception as exc:
            return FixResult("memory_reconnect", False, str(exc))


class SttWorker(BaseWorker):
    name = "stt"
    subsystems = ("stt",)

    def try_fix(self, incident: Incident, ctx: IntelligentContext) -> FixResult:
        from voice_service import preload_whisper_model, whisper_runtime_status

        status = whisper_runtime_status()
        provider = str(status.get("provider") or "")
        if provider in {"openai_whisper", "openai", "whisper_api", "elevenlabs"}:
            key_ok = bool(os.environ.get("OPENAI_API_KEY") or os.environ.get("ELEVENLABS_API_KEY"))
            return FixResult(
                "whisper_preload",
                key_ok,
                f"Cloud STT ({provider}); API key configured={key_ok}",
            )
        try:
            preload_whisper_model()
            status = whisper_runtime_status()
            ok = bool(status.get("loaded"))
            return FixResult(
                "whisper_preload",
                ok,
                f"Whisper loaded={ok}; device={status.get('device')}",
            )
        except Exception as exc:
            return FixResult("whisper_preload", False, str(exc))


class VoiceWorker(BaseWorker):
    name = "voice"
    subsystems = ("voice",)

    def try_fix(self, incident: Incident, ctx: IntelligentContext) -> FixResult:
        from esp_playback import clear_device_busy

        device_id = incident.device_id
        actions: list[str] = []
        try:
            if device_id != "server":
                clear_device_busy(device_id)
                actions.append(f"cleared_busy:{device_id}")
            else:
                clear_device_busy(None)
                actions.append("cleared_global_busy")

            ui_id = ctx.registry.ui_device_id()
            if device_id in {"server", ui_id} and hasattr(ctx.face_registration, "reset_to_idle"):
                ctx.face_registration.reset_to_idle()
                actions.append("face_registration_reset")

            return FixResult("voice_state_reset", True, "; ".join(actions))
        except Exception as exc:
            return FixResult("voice_state_reset", False, str(exc))


class TtsWorker(BaseWorker):
    name = "tts"
    subsystems = ("tts",)

    def try_fix(self, incident: Incident, ctx: IntelligentContext) -> FixResult:
        from esp_playback import clear_device_busy
        from tts_service import ensure_piper_model

        device_id = incident.device_id
        try:
            if device_id != "server":
                clear_device_busy(device_id)
            else:
                clear_device_busy(None)
            ok, detail = ensure_piper_model()
            tts_status = ctx.tts.status()
            err = str(tts_status.get("last_error") or "")
            if ok and not err:
                return FixResult(
                    "piper_model_download",
                    True,
                    f"[{device_id}] {detail}; tts_error=none",
                )
            return FixResult(
                "piper_model_download",
                ok and not err,
                f"[{device_id}] {detail}; tts_error={err or 'none'}",
            )
        except Exception as exc:
            return FixResult("piper_model_download", False, str(exc))


def _worker_chain() -> list[BaseWorker]:
    return [
        CameraWorker(),
        DiscoveryWorker(),
        LlmWorker(),
        MemoryWorker(),
        SttWorker(),
        TtsWorker(),
        VoiceWorker(),
    ]


WORKERS = _worker_chain()


def select_worker(incident: Incident) -> BaseWorker | None:
    for worker in WORKERS:
        if worker.handles(incident):
            return worker
    return None


def apply_fix(incident: Incident, *, action: str | None = None) -> FixAttempt:
    ctx = get_context()
    if action:
        logger.info(
            "Intelligent mode: recovery action %s for %s/%s",
            action,
            incident.device_id,
            incident.subsystem,
        )
        return dispatch_recovery_action(incident, action, ctx)

    worker = select_worker(incident)
    if worker is None:
        result = FixResult("none", False, f"No worker for subsystem={incident.subsystem}")
    else:
        logger.info(
            "Intelligent mode: %s fixing %s/%s (%s)",
            worker.name,
            incident.device_id,
            incident.subsystem,
            incident.error[:120],
        )
        result = worker.try_fix(incident, ctx)
    if result.action not in ALLOWED_FIX_ACTIONS:
        result = FixResult("none", False, f"Blocked non-whitelisted action: {result.action}")
    return FixAttempt(action=result.action, success=result.success, detail=result.detail)


def verify_incident_cleared(
    incident: Incident,
    snapshot: dict[str, Any],
    *,
    voice_active_fn: Any = None,
) -> bool:
    device_id = incident.device_id
    subsystem = incident.subsystem

    if subsystem == "camera":
        cams = snapshot.get("cameras") or {}
        cam = cams.get(device_id) if isinstance(cams, dict) else snapshot.get("camera")
        if not isinstance(cam, dict):
            cam = {}
        if not camera_expects_stream(snapshot, device_id, voice_active_fn=voice_active_fn):
            return True
        return not camera_stream_fault(
            snapshot, device_id, cam, voice_active_fn=voice_active_fn
        )

    if subsystem == "bot":
        devices = (snapshot.get("devices") or {}).get("devices") or []
        row = next(
            (d for d in devices if isinstance(d, dict) and d.get("device_id") == device_id),
            None,
        )
        base = ""
        if isinstance(row, dict):
            base = str(row.get("base_url") or row.get("effective_base_url") or "")
        if not base:
            return False
        expects = camera_expects_stream(snapshot, device_id, voice_active_fn=voice_active_fn)
        ok, err, http_code = probe_bot_snapshot(base)
        return snapshot_acceptable(ok, err, http_code, expects_stream=expects)

    if subsystem == "llm":
        llm = snapshot.get("llm") or {}
        return bool(isinstance(llm, dict) and llm.get("reachable"))

    if subsystem == "memory":
        mem = snapshot.get("memory") or {}
        return not (
            isinstance(mem, dict) and mem.get("database_url_set") and not mem.get("ready")
        )

    if subsystem == "stt":
        stt = snapshot.get("stt") or {}
        provider = str(stt.get("provider") or "")
        if provider in {"openai_whisper", "openai", "whisper_api", "elevenlabs"}:
            return True
        return bool(isinstance(stt, dict) and stt.get("loaded"))

    if subsystem == "tts":
        tts = snapshot.get("tts") or {}
        return not bool(isinstance(tts, dict) and str(tts.get("last_error") or "").strip())

    if subsystem == "voice":
        from intelligent_mode.voice_incident_filters import (
            device_has_recent_stt_empty,
            is_stt_empty_error,
        )
        from network_util import voice_ws_url_for_esp

        llm = snapshot.get("llm") or {}
        if isinstance(llm, dict) and not llm.get("reachable"):
            return False
        if not voice_ws_url_for_esp():
            return False
        if is_stt_empty_error(incident.error) and device_has_recent_stt_empty(
            device_id, window_seconds=300
        ):
            return False
        stt = snapshot.get("stt") or {}
        if isinstance(stt, dict) and str(stt.get("provider") or "") == "whisper":
            if not stt.get("loaded"):
                return False
        return True

    if subsystem == "discovery":
        disc = snapshot.get("discovery") or {}
        if isinstance(disc, dict) and str(disc.get("last_error") or "").strip():
            return False
        if device_id == "server":
            return True
        devices = (snapshot.get("devices") or {}).get("devices") or []
        row = next(
            (d for d in devices if isinstance(d, dict) and d.get("device_id") == device_id),
            None,
        )
        if not isinstance(row, dict):
            return False
        return bool(row.get("base_url") or row.get("camera_url") or row.get("play_wav_url"))

    return False


def verify_incident_with_smoke(incident: Incident, snapshot: dict[str, Any]) -> bool:
    voice_active_fn = None
    try:
        voice_active_fn = get_context().voice_active_fn
    except RuntimeError:
        pass
    from intelligent_mode.verification_agent import verify_incident_resolution

    result = verify_incident_resolution(
        incident,
        snapshot,
        live_probes=True,
        voice_active_fn=voice_active_fn,
        mode="post_fix",
    )
    incident.verification_report = result.to_dict()
    return result.passed

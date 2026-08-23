"""Intelligent Mode recovery — ordered fix chains and action dispatch."""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path
from typing import Any

from intelligent_mode.incidents import FixAttempt, Incident

logger = logging.getLogger(__name__)

# Ordered remediation chains per subsystem (safe, reversible ops only).
RECOVERY_CHAINS: dict[str, tuple[str, ...]] = {
    "memory": ("postgres_start", "memory_reconnect"),
    "llm": ("ollama_restart_warm", "ollama_cpu_fallback"),
    "stt": ("whisper_reload_cuda", "whisper_reload_cpu", "whisper_preload"),
    "tts": ("piper_reload_cpu", "piper_model_download"),
    "voice": ("voice_state_reset", "voice_pipeline_recovery"),
    "camera": ("lan_discovery", "camera_restart"),
    "bot": ("lan_discovery", "camera_restart"),
    "discovery": ("lan_discovery",),
}

# Extended whitelist for Intelligent Mode recovery actions.
RECOVERY_FIX_ACTIONS = frozenset(
    {
        "camera_restart",
        "lan_discovery",
        "ollama_restart_warm",
        "ollama_cpu_fallback",
        "memory_reconnect",
        "postgres_start",
        "whisper_preload",
        "whisper_reload_cuda",
        "whisper_reload_cpu",
        "voice_state_reset",
        "voice_pipeline_recovery",
        "piper_model_download",
        "piper_reload_cpu",
        "none",
    }
)


def recovery_chain_for(subsystem: str) -> tuple[str, ...]:
    return RECOVERY_CHAINS.get(subsystem, ())


def tried_actions(incident: Incident) -> set[str]:
    return {fix.action for fix in incident.fixes if fix.action}


def ordered_recovery_chain(
    incident: Incident,
    *,
    use_history: bool = True,
    min_history_samples: int = 2,
    verified_only: bool = False,
) -> tuple[str, ...]:
    """Recovery chain for this incident, optionally reordered by historical success."""
    base = recovery_chain_for(incident.subsystem)
    tried = tried_actions(incident)
    if not use_history or not base:
        return tuple(action for action in base if action not in tried)
    from intelligent_mode.fix_history import order_chain_by_success_rate

    return order_chain_by_success_rate(
        base,
        incident.subsystem,
        exclude=tried,
        min_samples=min_history_samples,
        verified_only=verified_only,
    )


def next_recovery_action(
    incident: Incident,
    *,
    use_history: bool = True,
    min_history_samples: int = 2,
    verified_only: bool = False,
) -> str | None:
    """Return the next untried action in the subsystem recovery chain."""
    chain = ordered_recovery_chain(
        incident,
        use_history=use_history,
        min_history_samples=min_history_samples,
        verified_only=verified_only,
    )
    return chain[0] if chain else None


def chain_exhausted(incident: Incident) -> bool:
    chain = recovery_chain_for(incident.subsystem)
    if not chain:
        return True
    return tried_actions(incident).issuperset(set(chain))


def _postgres_start() -> tuple[bool, str]:
    try:
        import shutil

        if shutil.which("pg_isready"):
            proc = subprocess.run(
                ["pg_isready", "-h", "127.0.0.1", "-p", "5432"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if proc.returncode == 0:
                return True, "PostgreSQL already accepting connections"
    except Exception as exc:
        logger.debug("pg_isready check failed: %s", exc)

    script = (
        Path(__file__).resolve().parent.parent / "scripts" / "start_postgres.sh"
    )
    if not script.is_file():
        return False, f"Missing script: {script}"

    for cmd in (
        ["systemctl", "start", "postgresql"],
        ["sudo", "-n", "systemctl", "start", "postgresql"],
        ["bash", str(script)],
    ):
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=120,
            )
            if proc.returncode == 0:
                return True, f"Started PostgreSQL via {' '.join(cmd[:2])}"
        except (OSError, subprocess.TimeoutExpired) as exc:
            logger.debug("postgres start attempt failed (%s): %s", cmd[0], exc)

    return False, "Could not start PostgreSQL (systemctl/start_postgres.sh)"


def _ollama_cpu_fallback() -> tuple[bool, str]:
    from llm_service import (
        DEFAULT_OLLAMA_CPU_URL,
        ollama_runtime_status,
        warm_ollama_model,
    )

    cpu_url = os.environ.get("OLLAMA_CPU_URL", DEFAULT_OLLAMA_CPU_URL).strip()
    os.environ["OLLAMA_URL"] = cpu_url
    try:
        warm_ollama_model(timeout_s=45, api_url=cpu_url)
        status = ollama_runtime_status(api_url=cpu_url)
        ok = bool(status.get("reachable"))
        return (
            ok,
            f"CPU Ollama fallback reachable={ok} at {cpu_url}; on_gpu={status.get('on_gpu')}",
        )
    except Exception as exc:
        return False, str(exc)


def _whisper_reload(*, device: str, compute_type: str) -> tuple[bool, str]:
    from voice_service import configure_from_environ, preload_whisper_model, whisper_runtime_status

    os.environ["WHISPER_DEVICE"] = device
    os.environ["WHISPER_COMPUTE_TYPE"] = compute_type
    configure_from_environ()
    try:
        preload_whisper_model()
        status = whisper_runtime_status()
        ok = bool(status.get("loaded")) and str(status.get("device")) == device
        return (
            ok,
            f"Whisper reload device={status.get('device')} compute={status.get('compute_type')} loaded={ok}",
        )
    except Exception as exc:
        return False, str(exc)


def _piper_reload_cpu() -> tuple[bool, str]:
    from tts_service import preload_piper_voice, reload_piper_voice

    os.environ["PIPER_USE_CUDA"] = "0"
    os.environ["TTS_USE_CUDA"] = "0"
    try:
        reload_piper_voice(use_cuda=False)
        ok = preload_piper_voice()
        return ok, f"Piper reloaded on CPU; preloaded={ok}"
    except Exception as exc:
        return False, str(exc)


def _voice_pipeline_recovery(incident: Incident, ctx: Any) -> tuple[bool, str]:
    from esp_playback import clear_device_busy
    from llm_service import (
        ollama_runtime_status,
        resolve_ollama_api_url,
        set_ollama_env_url,
        try_start_gpu_ollama,
        warm_ollama_model,
    )
    from voice_service import configure_from_environ, preload_whisper_model

    device_id = incident.device_id
    steps: list[str] = []
    try:
        clear_device_busy(device_id if device_id != "server" else None)
        steps.append("cleared_busy")
        if hasattr(ctx.face_registration, "reset_to_idle"):
            ctx.face_registration.reset_to_idle()
            steps.append("face_registration_reset")
        try_start_gpu_ollama()
        api_url = set_ollama_env_url(resolve_ollama_api_url())
        warm_ollama_model(timeout_s=20, api_url=api_url)
        llm_status = ollama_runtime_status(api_url=api_url)
        steps.append(f"ollama_warm(reachable={bool(llm_status.get('reachable'))})")
        if not llm_status.get("reachable"):
            return False, "; ".join(steps)
        configure_from_environ()
        if preload_whisper_model():
            steps.append("whisper_preload")
        from tts_service import preload_piper_voice

        if preload_piper_voice():
            steps.append("piper_preload")
        return True, "; ".join(steps)
    except Exception as exc:
        return False, f"{' ; '.join(steps)}; error={exc}"


def dispatch_recovery_action(
    incident: Incident,
    action: str,
    ctx: Any,
) -> FixAttempt:
    """Run a single whitelisted recovery action."""
    from intelligent_mode.workers import (
        CameraWorker,
        DiscoveryWorker,
        LlmWorker,
        MemoryWorker,
        SttWorker,
        TtsWorker,
        VoiceWorker,
        FixResult,
    )

    result: FixResult
    if action == "postgres_start":
        ok, detail = _postgres_start()
        result = FixResult("postgres_start", ok, detail)
    elif action == "ollama_cpu_fallback":
        ok, detail = _ollama_cpu_fallback()
        result = FixResult("ollama_cpu_fallback", ok, detail)
    elif action == "whisper_reload_cuda":
        ok, detail = _whisper_reload(device="cuda", compute_type="int8")
        result = FixResult("whisper_reload_cuda", ok, detail)
    elif action == "whisper_reload_cpu":
        ok, detail = _whisper_reload(device="cpu", compute_type="int8")
        result = FixResult("whisper_reload_cpu", ok, detail)
    elif action == "piper_reload_cpu":
        ok, detail = _piper_reload_cpu()
        result = FixResult("piper_reload_cpu", ok, detail)
    elif action == "voice_pipeline_recovery":
        ok, detail = _voice_pipeline_recovery(incident, ctx)
        result = FixResult("voice_pipeline_recovery", ok, detail)
    elif action in {"camera_restart", "lan_discovery", "ollama_restart_warm", "memory_reconnect", "whisper_preload", "voice_state_reset", "piper_model_download"}:
        worker_map = {
            "camera_restart": CameraWorker(),
            "lan_discovery": DiscoveryWorker(),
            "ollama_restart_warm": LlmWorker(),
            "memory_reconnect": MemoryWorker(),
            "whisper_preload": SttWorker(),
            "voice_state_reset": VoiceWorker(),
            "piper_model_download": TtsWorker(),
        }
        worker = worker_map[action]
        result = worker.try_fix(incident, ctx)
        if result.action != action:
            result = FixResult(action, result.success, result.detail)
    else:
        result = FixResult("none", False, f"Unknown recovery action: {action}")

    if result.action not in RECOVERY_FIX_ACTIONS:
        result = FixResult("none", False, f"Blocked recovery action: {result.action}")
    return FixAttempt(action=result.action, success=result.success, detail=result.detail)

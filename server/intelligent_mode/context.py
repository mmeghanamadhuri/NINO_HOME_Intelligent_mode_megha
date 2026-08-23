"""Runtime hooks from app.py — avoids circular imports."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


@dataclass
class IntelligentContext:
    registry: Any
    cameras: Any
    tts: Any
    face_registration: Any
    collect_status: Callable[[], dict[str, Any]]
    on_registry_updated: Callable[[], None] | None = None
    voice_active_fn: Callable[[str | None], bool] | None = None
    faces: Any = None


_CTX: IntelligentContext | None = None


def configure_context(ctx: IntelligentContext) -> None:
    global _CTX
    _CTX = ctx


def get_context() -> IntelligentContext:
    if _CTX is None:
        raise RuntimeError("Intelligent mode context is not configured")
    return _CTX


def build_default_status_collector(
    *,
    registry: Any,
    cameras: Any,
    tts: Any,
    face_registration: Any,
    faces: Any = None,
) -> Callable[[], dict[str, Any]]:
    def _collect() -> dict[str, Any]:
        from device_registry import resolve_device_id
        from device_discovery import bot_runtime_status, discovery_status, poll_bot_runtime
        from llm_service import ollama_runtime_status
        from memory_service import get_memory_service
        from voice_service import whisper_runtime_status

        active = resolve_device_id(None)
        runtime = bot_runtime_status()
        try:
            runtime = poll_bot_runtime(registry) or runtime
        except Exception:
            pass

        face_stats: dict[str, Any] = {}
        if faces is not None and hasattr(faces, "stats"):
            try:
                face_stats = faces.stats()
            except Exception:
                pass

        return {
            "device_id": active,
            "devices": registry.status(),
            "discovery": discovery_status(),
            "bot_runtime": runtime,
            "faces": face_stats,
            "face_registration": face_registration.status(),
            "camera": cameras.status(active),
            "cameras": cameras.status(),
            "tts": tts.status(),
            "stt": whisper_runtime_status(),
            "llm": ollama_runtime_status(
                model=__import__("os").environ.get("OLLAMA_MODEL"),
                api_url=__import__("os").environ.get("OLLAMA_URL"),
            ),
            "memory": get_memory_service().status(),
        }

    return _collect

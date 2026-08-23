"""End-to-end bot Q&A tests — ask questions, verify replies, feed intelligent mode."""

from __future__ import annotations

import io
import time
import uuid
import wave
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

import numpy as np

from intelligent_mode.smoke_tests import SmokeTestResult, _run


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class E2eVoiceRun:
    run_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    started_at: str = field(default_factory=_utc_now)
    finished_at: str = ""
    passed: int = 0
    failed: int = 0
    skipped: int = 0
    total: int = 0
    results: list[SmokeTestResult] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "passed": self.passed,
            "failed": self.failed,
            "skipped": self.skipped,
            "total": self.total,
            "ok": self.failed == 0,
            "results": [r.to_dict() for r in self.results],
        }


def _mono_wav(samples: np.ndarray, rate: int = 16000) -> bytes:
    pcm = np.asarray(samples, dtype=np.int16)
    bio = io.BytesIO()
    with wave.open(bio, "wb") as wo:
        wo.setnchannels(1)
        wo.setsampwidth(2)
        wo.setframerate(rate)
        wo.writeframes(pcm.tobytes())
    return bio.getvalue()


def _speech_like_wav(*, seconds: float = 1.2, rate: int = 16000) -> bytes:
    n = max(rate, int(rate * seconds))
    t = np.arange(n, dtype=np.float64) / rate
    tone = (6000 * np.sin(2 * np.pi * 220 * t)).astype(np.int16)
    return _mono_wav(tone, rate=rate)


# (question, acceptable substrings in reply)
E2E_LLM_QUESTIONS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("What is 2 plus 2?", ("4", "four")),
    ("Reply with one word: hello", ("hello", "hi")),
    ("What color is the sky on a clear day?", ("blue",)),
)


def _llm_question_checks() -> list[tuple[str, Callable[[], tuple[bool, str]], dict[str, Any]]]:
    checks: list[tuple[str, Callable[[], tuple[bool, str]], dict[str, Any]]] = []

    def _make(question: str, expected: tuple[str, ...]) -> Callable[[], tuple[bool, str]]:
        def _check() -> tuple[bool, str]:
            from llm_service import ollama_generate

            try:
                reply = ollama_generate(question, timeout_s=45, num_predict=48).strip()
            except Exception as exc:
                return False, str(exc)[:120]
            if not reply:
                return False, "empty reply"
            lowered = reply.lower()
            if any(token.lower() in lowered for token in expected):
                return True, reply[:120]
            return False, f"unexpected reply: {reply[:120]}"

        return _check

    for question, expected in E2E_LLM_QUESTIONS:
        slug = question.lower().split()[0:3]
        name = "e2e:llm:" + "_".join(slug)
        checks.append(
            (
                name,
                _make(question, expected),
                {
                    "test_id": name,
                    "device_id": "server",
                    "subsystem": "llm",
                    "severity": "critical",
                    "tier": 0,
                },
            )
        )
    return checks


def _voice_pipeline_check() -> tuple[bool, str]:
    """Run STT→LLM→TTS in-process with mocked transcription (no audio to physical bot)."""
    from unittest.mock import patch

    from voice_service import process_voice_wav, voice_error_tts_enabled

    wav = _speech_like_wav()
    with patch("voice_service.transcribe_wav", return_value=("what is 2 plus 2", "e2e-mock")):
        out, meta = process_voice_wav(
            wav,
            device_id="e2e-test",
            session_kind="continue",
            session_id="e2e-session",
        )
    reply = str(meta.timings.get("reply_text") or "").strip()
    path = str(meta.timings.get("reply_path") or "")
    if path in {"stt_empty", "stt_silent", "stt_rejected"}:
        return False, f"STT path={path}"
    if not reply:
        return False, f"empty reply path={path}"
    if "language model" in reply.lower() or "could not reach" in reply.lower():
        return False, f"LLM error spoken: {reply[:120]}"
    if not out:
        return False, "no output wav"
    if not voice_error_tts_enabled() and len(out) < 500:
        # Silent recovery is acceptable while LLM is down; test reports LLM issue separately.
        return False, f"silent/short recovery wav path={path}"
    lowered = reply.lower()
    if "4" in lowered or "four" in lowered:
        return True, f"path={path} reply={reply[:80]}"
    return False, f"path={path} reply={reply[:120]}"


def run_e2e_voice_suite(snapshot: dict[str, Any]) -> E2eVoiceRun:
    """Ask scripted questions and verify the bot brain responds correctly."""
    run = E2eVoiceRun()
    results: list[SmokeTestResult] = []

    llm = snapshot.get("llm") or {}
    llm_up = bool(isinstance(llm, dict) and llm.get("reachable"))

    if not llm_up:
        run.skipped = len(E2E_LLM_QUESTIONS) + 1
        run.total = run.skipped
        run.finished_at = _utc_now()
        results.append(
            SmokeTestResult(
                test_id="e2e:llm:skipped",
                name="e2e:llm:skipped",
                device_id="server",
                subsystem="llm",
                passed=False,
                message=str(llm.get("warning") or "Ollama unreachable — E2E skipped"),
                severity="critical",
                tier=0,
                skipped=True,
            )
        )
        run.results = results
        run.failed = 0
        return run

    for name, fn, meta in _llm_question_checks():
        results.append(_run(name, fn, **meta))

    results.append(
        _run(
            "e2e:voice_pipeline",
            _voice_pipeline_check,
            test_id="e2e:voice_pipeline",
            device_id="server",
            subsystem="voice",
            severity="critical",
            tier=0,
        )
    )

    run.results = results
    run.total = len(results)
    run.passed = sum(1 for r in results if r.passed)
    run.failed = sum(1 for r in results if not r.passed)
    run.finished_at = _utc_now()
    return run


def failures_to_e2e_candidates(
    run: E2eVoiceRun,
    *,
    device_names: dict[str, str] | None = None,
) -> list:
    from intelligent_mode.detectors import DetectionCandidate

    names = device_names or {}
    out: list[DetectionCandidate] = []
    for result in run.results:
        if result.passed or result.skipped:
            continue
        from intelligent_mode.incident_filters import is_test_skip_message

        if is_test_skip_message(result.message):
            continue
        device_id = result.device_id or "server"
        display = names.get(device_id, device_id)
        out.append(
            DetectionCandidate(
                device_id=device_id,
                display_name=display,
                subsystem=result.subsystem,
                severity=result.severity,
                tier=result.tier,
                error=f"[e2e:{result.test_id}] {result.message}",
                snapshot_hint={"e2e_test": result.to_dict()},
            )
        )
    return out

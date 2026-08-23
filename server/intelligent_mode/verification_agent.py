"""Cross-check agent — verify fixes actually worked before marking resolved."""

from __future__ import annotations

import logging
import re
from dataclasses import asdict, dataclass, field
from typing import Any

from intelligent_mode.incidents import Incident
from intelligent_mode.workers import verify_incident_cleared

logger = logging.getLogger(__name__)

_BRAIN_SUBSYSTEMS = frozenset({"voice", "llm", "stt", "tts"})


@dataclass
class VerificationCheck:
    name: str
    passed: bool
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class VerificationResult:
    passed: bool
    checks: list[VerificationCheck] = field(default_factory=list)
    summary: str = ""
    verified_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "checks": [c.to_dict() for c in self.checks],
            "summary": self.summary,
            "verified_at": self.verified_at,
        }


def _error_needs_ollama_probe(error: str) -> bool:
    lowered = str(error or "").lower()
    return any(
        token in lowered
        for token in (
            "11434",
            "11435",
            "/api/generate",
            "ollama",
            "language model",
            "connection refused",
            "max retries exceeded",
        )
    )


def _probe_ollama_generate() -> tuple[bool, str]:
    try:
        from llm_service import ollama_generate, ollama_is_reachable, resolve_ollama_api_url

        api_url = resolve_ollama_api_url()
        if not ollama_is_reachable(api_url=api_url, timeout_s=5):
            return False, f"Ollama unreachable at {api_url}"
        reply = ollama_generate("Reply with exactly: ok", num_predict=8, temperature=0.0, timeout_s=20)
        text = str(reply or "").strip()
        if not text:
            return False, "Ollama generate returned empty reply"
        return True, f"Ollama live probe ok ({api_url})"
    except Exception as exc:
        return False, str(exc)


def _agent_auto_resolve_still_valid(incident: Incident) -> tuple[bool, str]:
    try:
        from intelligent_mode.agent_remediation import classify_agent_remediation

        plan = classify_agent_remediation(incident)
        if plan is None:
            return False, "No agent auto-resolve pattern applies anymore"
        if plan.recovery_actions:
            return False, "Issue still needs recovery actions — not safe to auto-resolve"
        reason = str(plan.auto_resolve_reason or plan.summary or "").strip()
        return True, reason or "Agent pattern still valid"
    except Exception as exc:
        return False, f"Agent pattern check failed: {exc}"


def verify_incident_resolution(
    incident: Incident,
    snapshot: dict[str, Any],
    *,
    live_probes: bool = True,
    voice_active_fn: Any = None,
    mode: str = "post_fix",
) -> VerificationResult:
    """Run real-time checks before an incident may be marked resolved.

    mode:
      - post_fix: after Intelligent Mode attempted recovery
      - auto_resolve: stale/false-alarm auto-resolve without a fix chain
    """
    from datetime import datetime, timezone

    checks: list[VerificationCheck] = []
    subsystem = str(incident.subsystem or "").lower()
    error = str(incident.error or "")

    cleared = verify_incident_cleared(
        incident, snapshot, voice_active_fn=voice_active_fn
    )
    checks.append(
        VerificationCheck(
            name="subsystem_health",
            passed=cleared,
            detail="Subsystem health check passed" if cleared else "Subsystem still unhealthy",
        )
    )

    if subsystem in _BRAIN_SUBSYSTEMS or _error_needs_ollama_probe(error):
        llm = snapshot.get("llm") if isinstance(snapshot.get("llm"), dict) else {}
        llm_ok = bool(isinstance(llm, dict) and llm.get("reachable"))
        checks.append(
            VerificationCheck(
                name="llm_snapshot",
                passed=llm_ok,
                detail=(
                    f"LLM reachable at {llm.get('base_url') or 'configured URL'}"
                    if llm_ok
                    else str(llm.get("warning") or "LLM unreachable in snapshot")
                ),
            )
        )
        if live_probes:
            ok, detail = _probe_ollama_generate()
            checks.append(
                VerificationCheck(
                    name="ollama_live_generate",
                    passed=ok,
                    detail=detail,
                )
            )

    from intelligent_mode.smoke_tests import device_smoke_passed

    smoke_ok = device_smoke_passed(
        snapshot,
        incident.device_id,
        voice_active_fn=voice_active_fn,
        subsystem=subsystem,
    )
    checks.append(
        VerificationCheck(
            name="smoke_tests",
            passed=smoke_ok,
            detail="Smoke tests passed" if smoke_ok else "Smoke tests still failing",
        )
    )

    if mode == "auto_resolve" and not incident.fixes and incident.fix_attempts == 0:
        auto_ok, auto_detail = _agent_auto_resolve_still_valid(incident)
        checks.append(
            VerificationCheck(
                name="auto_resolve_valid",
                passed=auto_ok,
                detail=auto_detail,
            )
        )
        if auto_ok:
            try:
                from intelligent_mode.agent_remediation import classify_agent_remediation

                plan = classify_agent_remediation(incident)
                if plan is not None and not plan.recovery_actions:
                    summary = (
                        "Verification agent: benign auto-resolve pattern confirmed — "
                        f"{plan.pattern_id}"
                    )
                    return VerificationResult(
                        passed=True,
                        checks=checks,
                        summary=summary,
                        verified_at=datetime.now(timezone.utc).isoformat(),
                    )
            except Exception:
                pass

    passed = all(check.passed for check in checks)
    failed = [check.name for check in checks if not check.passed]
    if passed:
        summary = "Verification agent: all checks passed — issue is actually fixed"
    else:
        summary = f"Verification agent: NOT resolved — failed: {', '.join(failed)}"

    result = VerificationResult(
        passed=passed,
        checks=checks,
        summary=summary,
        verified_at=datetime.now(timezone.utc).isoformat(),
    )
    if not passed:
        logger.info(
            "Verification failed for %s/%s (%s): %s",
            incident.device_id,
            incident.subsystem,
            incident.incident_id,
            summary,
        )
    return result

"""Master orchestrator — detect, assign workers, verify, report, email."""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any

from intelligent_mode.config import IntelligentConfig, load_config
from intelligent_mode.context import IntelligentContext, configure_context, get_context
from intelligent_mode.detectors import GraceTracker, candidate_to_incident, detect_anomalies
from intelligent_mode.digest import EmailDigestQueue
from intelligent_mode.emailer import email_configured, send_incident_email
from intelligent_mode.incidents import Incident, IncidentStore
from intelligent_mode.debugger import build_debug_report
from intelligent_mode.recovery import chain_exhausted, next_recovery_action, tried_actions
from intelligent_mode.reporter import build_report
from intelligent_mode.smoke_tests import failures_to_candidates, run_smoke_suite
from intelligent_mode.e2e_voice_test import failures_to_e2e_candidates, run_e2e_voice_suite
from intelligent_mode.test_store import SmokeTestStore
from intelligent_mode.workers import apply_fix, verify_incident_with_smoke

logger = logging.getLogger(__name__)

_ORCHESTRATOR: IntelligentOrchestrator | None = None


def _log(event: str, **detail: object) -> None:
    try:
        from pipeline_log import pipeline_log

        pipeline_log("AGENT", event, detail=" ".join(f"{k}={v}" for k, v in detail.items()))
    except Exception:
        logger.info("Intelligent mode %s %s", event, detail)


class IntelligentOrchestrator:
    def __init__(
        self,
        config: IntelligentConfig,
        store: IncidentStore | None = None,
    ) -> None:
        self.config = config
        self.store = store or IncidentStore(store_max=config.incidents_store_max)
        self.smoke_store = SmokeTestStore()
        self._grace = GraceTracker(config.grace_seconds)
        self._digest = EmailDigestQueue(config)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.RLock()
        self._last_tick_at: str | None = None
        self._last_tick_summary: dict[str, Any] = {}

    def start(self) -> None:
        if not self.config.enabled:
            logger.info("Intelligent mode disabled (INTELLIGENT_MODE=0)")
            return
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop,
            daemon=True,
            name="intelligent-mode",
        )
        self._thread.start()
        if self.config.prune_resolved_on_start:
            stats = self.store.prune_resolved(
                keep_recent=self.config.incidents_keep_resolved,
            )
            if stats.get("removed"):
                _log("PRUNE", **stats)
                logger.info(
                    "Intelligent mode pruned %d resolved incident(s); %d remaining",
                    stats["removed"],
                    stats["remaining"],
                )
        _log(
            "START",
            poll=self.config.poll_seconds,
            grace=self.config.grace_seconds,
            camera_grace=self.config.camera_grace_seconds,
            llm_grace=self.config.llm_grace_seconds,
            email=self.config.email_mode,
        )
        logger.info(
            "Intelligent mode started (poll=%ss, grace=%ss, camera_grace=%ss, llm_grace=%ss, email=%s)",
            self.config.poll_seconds,
            self.config.grace_seconds,
            self.config.camera_grace_seconds,
            self.config.llm_grace_seconds,
            self.config.email_mode,
        )

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5.0)
        for inc in self._digest.flush_now():
            self.store.update(inc)

    def status(self) -> dict[str, Any]:
        with self._lock:
            open_count = sum(
                1
                for inc in self.store.list_incidents(limit=500)
                if inc.status in {"open", "fixing", "escalated"}
            )
            return {
                "enabled": self.config.enabled,
                "running": bool(self._thread and self._thread.is_alive()),
                "poll_seconds": self.config.poll_seconds,
                "grace_seconds": self.config.grace_seconds,
                "camera_grace_seconds": self.config.camera_grace_seconds,
                "fix_cooldown_seconds": self.config.fix_cooldown_seconds,
                "max_fix_attempts_per_hour": self.config.max_fix_attempts_per_hour,
                "max_auto_fix_tier": self.config.max_auto_fix_tier,
                "autonomous_recovery_enabled": self.config.autonomous_recovery_enabled,
                "recovery_chain_max_steps": self.config.recovery_chain_max_steps,
                "autonomous_max_fix_tier": self.config.autonomous_max_fix_tier,
                "retry_escalated": self.config.retry_escalated,
                "email_mode": self.config.email_mode,
                "email_digest_seconds": self.config.email_digest_seconds,
                "email_pending": self._digest.pending_count(),
                "email_configured": email_configured(self.config),
                "email_to": self.config.email_to,
                "open_incidents": open_count,
                "smoke_tests_enabled": self.config.smoke_tests_enabled,
                "e2e_tests_enabled": self.config.e2e_tests_enabled,
                "self_debug_enabled": self.config.self_debug_enabled,
                "skip_fix_during_voice": self.config.skip_fix_during_voice,
                "email_code_bugs": self.config.email_code_bugs,
                "auto_ota_on_code_bug": self.config.auto_ota_on_code_bug,
                "coding_agent_enabled": self.config.coding_agent_enabled,
                "fix_history_enabled": self.config.fix_history_enabled,
                "llm_fix_selection": self.config.llm_fix_selection,
                "llm_fix_min_confidence": self.config.llm_fix_min_confidence,
                "baseline_anomaly_enabled": self.config.baseline_anomaly_enabled,
                "baseline_sigma_threshold": self.config.baseline_sigma_threshold,
                "last_smoke_run": self.smoke_store.last_run_dict(),
                "last_tick_at": self._last_tick_at,
                "last_tick": self._last_tick_summary,
            }

    def run_once(self) -> dict[str, Any]:
        with self._lock:
            return self._tick()

    def run_smoke_tests(self) -> dict[str, Any]:
        with self._lock:
            ctx = get_context()
            snapshot = ctx.collect_status()
            run = run_smoke_suite(snapshot, voice_active_fn=ctx.voice_active_fn)
            self.smoke_store.save_run(run)
            return run.to_dict()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception:
                logger.exception("Intelligent mode tick failed")
            self._stop.wait(self.config.poll_seconds)

    def _tick(self) -> dict[str, Any]:
        ctx = get_context()
        snapshot = ctx.collect_status()
        smoke_summary: dict[str, Any] | None = None
        e2e_summary: dict[str, Any] | None = None
        candidates = detect_anomalies(
            snapshot,
            grace=self._grace,
            camera_grace_seconds=self.config.camera_grace_seconds,
            llm_grace_seconds=self.config.llm_grace_seconds,
            voice_active_fn=ctx.voice_active_fn,
            baseline_enabled=self.config.baseline_anomaly_enabled,
            baseline_sigma=self.config.baseline_sigma_threshold,
            baseline_min_samples=self.config.baseline_min_samples,
            baseline_grace_seconds=self.config.baseline_grace_seconds,
        )

        smoke_run = None
        if self.config.smoke_tests_enabled:
            smoke_run = run_smoke_suite(snapshot, voice_active_fn=ctx.voice_active_fn)
            self.smoke_store.save_run(smoke_run)
            try:
                from intelligent_mode.baselines import record_from_snapshot

                record_from_snapshot(snapshot, smoke_run=smoke_run.to_dict())
            except Exception:
                logger.debug("Baseline metric recording skipped", exc_info=True)
            smoke_summary = {
                "run_id": smoke_run.run_id,
                "passed": smoke_run.passed,
                "failed": smoke_run.failed,
                "total": smoke_run.total,
                "ok": smoke_run.failed == 0,
            }
            device_names = {
                str(row.get("device_id") or ""): str(row.get("display_name") or "")
                for row in (snapshot.get("devices") or {}).get("devices") or []
                if isinstance(row, dict)
            }
            for sc in failures_to_candidates(smoke_run, device_names=device_names):
                smoke_key = f"smoke:{Incident.make_signature(sc.device_id, sc.subsystem, sc.error)}"
                if self._grace.ready(smoke_key, True):
                    candidates.append(sc)

        if self.config.e2e_tests_enabled and not (
            self.config.skip_e2e_during_voice and self._any_voice_active(ctx)
        ):
            e2e_run = run_e2e_voice_suite(snapshot)
            e2e_summary = {
                "run_id": e2e_run.run_id,
                "passed": e2e_run.passed,
                "failed": e2e_run.failed,
                "skipped": e2e_run.skipped,
                "total": e2e_run.total,
                "ok": e2e_run.failed == 0,
            }
            device_names = {
                str(row.get("device_id") or ""): str(row.get("display_name") or "")
                for row in (snapshot.get("devices") or {}).get("devices") or []
                if isinstance(row, dict)
            }
            for ec in failures_to_e2e_candidates(e2e_run, device_names=device_names):
                e2e_key = f"e2e:{Incident.make_signature(ec.device_id, ec.subsystem, ec.error)}"
                if self._grace.ready(e2e_key, True):
                    candidates.append(ec)
        elif self.config.e2e_tests_enabled and self._any_voice_active(ctx):
            e2e_summary = {"skipped": True, "reason": "voice_session_active"}
        elif self.config.baseline_anomaly_enabled and not self.config.smoke_tests_enabled:
            try:
                from intelligent_mode.baselines import record_from_snapshot

                record_from_snapshot(snapshot)
            except Exception:
                logger.debug("Baseline metric recording skipped", exc_info=True)

        active_signatures = {
            candidate_to_incident(candidate, snapshot).signature for candidate in candidates
        }
        open_signatures = {
            inc.signature
            for inc in self.store.list_incidents(limit=500)
            if inc.status in {"open", "fixing", "escalated"}
        }
        from intelligent_mode.incident_filters import (
            dedupe_detection_candidates,
            should_open_incident,
            should_suppress_incident,
        )

        candidates = dedupe_detection_candidates(
            candidates, open_signatures=open_signatures
        )
        opened = 0
        fix_events = 0
        resolved = 0
        emailed = 0

        for candidate in candidates:
            if not should_open_incident(
                candidate, snapshot, open_signatures=open_signatures
            ):
                reason = (
                    "benign_pattern"
                    if should_suppress_incident(
                        candidate.error,
                        candidate.subsystem,
                        snapshot,
                        device_id=candidate.device_id,
                    )
                    else "no_open_rule"
                )
                _log(
                    "SKIP_DETECT",
                    bot=candidate.device_id,
                    subsystem=candidate.subsystem,
                    reason=reason,
                )
                continue
            incident = candidate_to_incident(candidate, snapshot)
            incident, created = self.store.open_incident(incident)
            if created:
                opened += 1
                _log(
                    "DETECT",
                    bot=incident.device_id,
                    subsystem=incident.subsystem,
                    tier=incident.tier,
                )
            self._debug_incident(incident, snapshot)
            before_fixes = len(incident.fixes)
            self._maybe_fix(incident, snapshot)
            if len(incident.fixes) > before_fixes:
                fix_events += 1
                self._debug_incident(incident, snapshot)
            if self._pending_stt_recovery(incident):
                self.store.update(incident)
                continue
            if self._verify_and_finalize(incident):
                resolved += 1
            if self._maybe_email(incident):
                emailed += 1
            self.store.update(incident)

        self.store.reload()
        for incident in self.store.list_incidents(limit=500):
            if incident.status not in {"open", "fixing", "escalated"}:
                continue
            from intelligent_mode.soak_test import stale_soak_incident_resolution
            from intelligent_mode.agent_remediation import auto_resolve_reason

            stale_reason = stale_soak_incident_resolution(incident.error) or auto_resolve_reason(
                incident
            )
            if stale_reason:
                ctx = get_context()
                after = ctx.collect_status()
                incident.after_snapshot = after
                if self._try_resolve(incident, after, stale_reason):
                    self.store.update(incident)
                    resolved += 1
                    if self._maybe_email(incident):
                        emailed += 1
                    _log(
                        "AUTO_RESOLVE",
                        id=incident.incident_id,
                        bot=incident.device_id,
                        reason="stale_soak_false_positive",
                    )
                continue
            if incident.signature in active_signatures:
                continue
            if self._is_code_bug(incident):
                continue
            ctx = get_context()
            after = ctx.collect_status()
            incident.after_snapshot = after
            if self._try_resolve(
                incident,
                after,
                "Auto-resolved: health check no longer reports this problem.",
            ):
                if self._maybe_email(incident):
                    emailed += 1
                self.store.update(incident)
                resolved += 1
                _log("AUTO_RESOLVE", id=incident.incident_id, bot=incident.device_id)

        digested_batch = self._digest.flush_due()
        for inc in digested_batch:
            self.store.update(inc)
        emailed += len(digested_batch)

        summary = {
            "candidates": len(candidates),
            "opened": opened,
            "fix_events": fix_events,
            "resolved": resolved,
            "emailed": emailed,
            "smoke_tests": smoke_summary,
            "e2e_tests": e2e_summary,
        }
        self._last_tick_at = datetime.now(timezone.utc).isoformat()
        self._last_tick_summary = summary
        if candidates or fix_events or digested_batch:
            logger.info("Intelligent mode tick: %s", summary)
        return summary

    def _voice_active(self, ctx: IntelligentContext, device_id: str | None = None) -> bool:
        fn = ctx.voice_active_fn
        if fn is None:
            return False
        try:
            return bool(fn(device_id))
        except Exception:
            return False

    def _any_voice_active(self, ctx: IntelligentContext) -> bool:
        if self._voice_active(ctx, None):
            return True
        devices = (ctx.collect_status().get("devices") or {}).get("devices") or []
        for row in devices:
            if isinstance(row, dict) and self._voice_active(ctx, str(row.get("device_id") or "")):
                return True
        return False

    def _debug_incident(self, incident: Incident, snapshot: dict[str, Any]) -> None:
        if not self.config.self_debug_enabled:
            return
        try:
            incident.debug_report = build_debug_report(
                incident,
                snapshot=snapshot,
                use_llm=self.config.llm_debug_analysis,
            )
            report = incident.debug_report or {}
            code_bug = report.get("code_bug") if isinstance(report.get("code_bug"), dict) else {}
            if code_bug.get("is_code_bug") and self.config.auto_ota_on_code_bug:
                from intelligent_mode.code_bug_analyzer import (
                    CodeBugAnalysis,
                    try_firmware_ota_for_incident,
                )

                analysis = CodeBugAnalysis.from_dict(code_bug)
                analysis = try_firmware_ota_for_incident(incident, analysis)
                report["code_bug"] = analysis.to_dict()
                incident.debug_report = report
                if analysis.ota_detail:
                    _log(
                        "OTA",
                        bot=incident.device_id,
                        deployed=analysis.ota_deployed,
                        detail=str(analysis.ota_detail)[:80],
                    )
            _log(
                "DEBUG",
                bot=incident.device_id,
                category=report.get("category"),
                cause=str(report.get("root_cause", ""))[:80],
                code_bug=bool(code_bug.get("is_code_bug")),
            )
        except Exception:
            logger.exception("Self-debug failed for incident %s", incident.incident_id)

    def _is_code_bug(self, incident: Incident) -> bool:
        try:
            from intelligent_mode.code_bug_analyzer import is_code_bug_incident

            return is_code_bug_incident(incident)
        except Exception:
            return False

    def _effective_max_tier(self) -> int:
        if self.config.autonomous_recovery_enabled:
            return max(self.config.max_auto_fix_tier, self.config.autonomous_max_fix_tier)
        return self.config.max_auto_fix_tier

    def _record_learning_outcomes(self, incident: Incident, verified: bool) -> None:
        if not incident.fixes:
            return
        last = incident.fixes[-1]
        action = str(last.action or "").strip()
        if not action or action == "none":
            return

        if self.config.learn_from_verification:
            last.success = bool(last.success) and verified

        if self.config.experience_playbook_enabled:
            try:
                from intelligent_mode.experience_playbook import record_incident_outcome

                record_incident_outcome(incident, action, verified=verified)
            except Exception:
                logger.debug("Experience playbook record failed", exc_info=True)

        if self.config.fix_history_enabled or self.config.experience_playbook_enabled:
            try:
                from intelligent_mode.fix_history import invalidate_stats_cache

                invalidate_stats_cache()
            except Exception:
                pass

    def _choose_recovery_action(
        self,
        incident: Incident,
        snapshot: dict[str, Any],
        *,
        allow_llm: bool,
    ) -> str | None:
        if not self.config.autonomous_recovery_enabled:
            return None

        tried = tried_actions(incident)
        agent_chain = self._agent_recovery_actions(incident, tried)
        verified_only = self.config.learn_from_verification

        run_llm = (
            allow_llm
            and self.config.llm_fix_selection
            and (len(agent_chain) > 1 or not agent_chain)
        )
        if run_llm:
            try:
                from intelligent_mode.llm_fix_selector import select_fix_action

                selection = select_fix_action(
                    incident,
                    snapshot,
                    tried_actions=tried,
                    min_confidence=self.config.llm_fix_min_confidence,
                    allowed_actions=list(agent_chain) if agent_chain else None,
                    verified_only=verified_only,
                )
                if selection:
                    debug = dict(incident.debug_report or {})
                    debug["fix_selection"] = selection.to_dict()
                    incident.debug_report = debug
                    if selection.action and selection.action not in tried:
                        _log(
                            "LLM_FIX",
                            action=selection.action,
                            confidence=selection.confidence,
                            bot=incident.device_id,
                        )
                        return selection.action
            except Exception:
                logger.debug("LLM fix selection failed", exc_info=True)

        if agent_chain:
            action = agent_chain[0]
            _log("AGENT_FIX", action=action, bot=incident.device_id)
            return action

        return next_recovery_action(
            incident,
            use_history=self.config.fix_history_enabled,
            min_history_samples=self.config.min_fix_history_samples,
            verified_only=verified_only,
        )

    def _agent_recovery_actions(self, incident: Incident, tried: set[str]) -> tuple[str, ...]:
        try:
            from intelligent_mode.agent_remediation import preferred_recovery_actions

            return tuple(
                action
                for action in preferred_recovery_actions(incident)
                if action and action not in tried
            )
        except Exception:
            logger.debug("Agent recovery actions lookup failed", exc_info=True)
            return ()

    def _pending_stt_recovery(self, incident: Incident) -> bool:
        """STT-empty incidents must run recovery (or clear from log) before resolving."""
        try:
            from intelligent_mode.agent_remediation import classify_agent_remediation

            plan = classify_agent_remediation(incident)
            if plan is None or plan.pattern_id != "voice_stt_recovery":
                return False
            if incident.status not in {"open", "fixing", "escalated"}:
                return False
            has_successful_fix = any(
                fix.success and fix.action and fix.action != "none"
                for fix in incident.fixes
            )
            return not has_successful_fix
        except Exception:
            return False

    def _maybe_fix(self, incident: Incident, snapshot: dict[str, Any]) -> None:
        if self.config.autonomous_recovery_enabled and incident.status == "escalated":
            if self.config.retry_escalated and not chain_exhausted(incident):
                incident.status = "open"
                _log("RETRY", reason="recovery_chain", bot=incident.device_id)

        if incident.status not in {"open", "escalated"}:
            return
        try:
            from intelligent_mode.agent_remediation import classify_agent_remediation

            plan = classify_agent_remediation(incident)
            if plan is not None and not plan.recovery_actions:
                reason = plan.auto_resolve_reason or plan.summary
                if self._try_resolve(incident, snapshot, reason):
                    _log("AUTO_RESOLVE", reason=plan.pattern_id, bot=incident.device_id)
                return
        except Exception:
            logger.debug("Agent auto-resolve check skipped", exc_info=True)
        ctx = get_context()
        if self.config.skip_fix_during_voice and self._voice_active(ctx, incident.device_id):
            _log("SKIP_FIX", reason="voice_active", bot=incident.device_id)
            return
        if self._is_code_bug(incident):
            if incident.status in {"open", "fixing"}:
                incident.status = "escalated"
                incident.resolved_at = None
                _log("ESCALATE", reason="code_bug", bot=incident.device_id)
            return

        debug = incident.debug_report or {}
        if debug and debug.get("fixable_by_agent") is False:
            if incident.status == "open" and incident.fix_attempts == 0:
                incident.status = "escalated"
                _log("ESCALATE", reason="not_fixable", bot=incident.device_id)
                return
            elif not self.config.autonomous_recovery_enabled:
                if incident.status == "open" and incident.fix_attempts == 0:
                    incident.status = "escalated"
                    _log("ESCALATE", reason="not_fixable", bot=incident.device_id)
                return
            elif chain_exhausted(incident):
                if incident.status == "open":
                    incident.status = "escalated"
                    _log("ESCALATE", reason="chain_exhausted", bot=incident.device_id)
                return
        if incident.tier > self._effective_max_tier():
            incident.status = "escalated"
            _log("ESCALATE", reason="tier", tier=incident.tier, bot=incident.device_id)
            return
        recent = self.store.recent_fix_count(incident.signature)
        if recent >= self.config.max_fix_attempts_per_hour:
            incident.status = "escalated"
            _log("ESCALATE", reason="fix_cap", bot=incident.device_id)
            return
        if incident.fixes:
            try:
                last_at = datetime.fromisoformat(
                    incident.fixes[-1].at.replace("Z", "+00:00")
                ).timestamp()
                if (time.time() - last_at) < self.config.fix_cooldown_seconds:
                    return
            except ValueError:
                pass

        steps = (
            self.config.recovery_chain_max_steps
            if self.config.autonomous_recovery_enabled
            else 1
        )
        for step_idx in range(steps):
            action = self._choose_recovery_action(
                incident,
                snapshot,
                allow_llm=(
                    step_idx == 0
                    or bool(incident.fixes and not incident.fixes[-1].success)
                ),
            )
            incident.status = "fixing"
            fix = apply_fix(incident, action=action)
            incident.fixes.append(fix)
            incident.fix_attempts += 1
            incident.before_snapshot = snapshot
            _log(
                "FIX",
                bot=incident.device_id,
                action=fix.action,
                ok=fix.success,
            )
            if fix.action and fix.action != "none":
                try:
                    from intelligent_mode.fix_history import invalidate_stats_cache

                    invalidate_stats_cache()
                except Exception:
                    pass
            if not fix.success:
                break
            if action is None or chain_exhausted(incident):
                break
            time.sleep(min(3, self.config.verify_delay_seconds))
        if incident.fixes and incident.fixes[-1].success:
            time.sleep(self.config.verify_delay_seconds)

    def _verify_and_finalize(self, incident: Incident) -> bool:
        if incident.status not in {"fixing", "open", "escalated"}:
            return incident.status == "resolved"

        if self._is_code_bug(incident):
            incident.status = "escalated"
            incident.resolved_at = None
            _log("ESCALATE", reason="code_bug_unresolved", bot=incident.device_id)
            return False

        ctx = get_context()
        after = ctx.collect_status()
        incident.after_snapshot = after
        if verify_incident_with_smoke(incident, after):
            incident.report = (
                str(incident.verification_report.get("summary") or "")
                if isinstance(incident.verification_report, dict)
                else ""
            ) or incident.report
            incident.status = "resolved"
            incident.resolved_at = datetime.now(timezone.utc).isoformat()
            self._grace.reset(incident.signature)
            self._grace.reset(f"smoke:{incident.signature}")
            self._record_learning_outcomes(incident, verified=True)
            _log("VERIFIED", bot=incident.device_id, subsystem=incident.subsystem)
            return True

        self._record_learning_outcomes(incident, verified=False)

        if incident.fix_attempts >= self.config.max_fix_attempts_per_hour:
            incident.status = "escalated"
        elif incident.status == "fixing":
            incident.status = "open"
        return False

    def _try_resolve(
        self,
        incident: Incident,
        snapshot: dict[str, Any],
        reason: str,
        *,
        mode: str = "auto_resolve",
    ) -> bool:
        """Mark resolved only if the verification agent confirms the fix is real."""
        if incident.status == "resolved":
            return True
        from intelligent_mode.verification_agent import verify_incident_resolution

        voice_active_fn = None
        try:
            voice_active_fn = get_context().voice_active_fn
        except RuntimeError:
            pass
        result = verify_incident_resolution(
            incident,
            snapshot,
            live_probes=self.config.verification_live_probes,
            voice_active_fn=voice_active_fn,
            mode=mode,
        )
        incident.verification_report = result.to_dict()
        if not result.passed:
            self._record_learning_outcomes(incident, verified=False)
            _log(
                "VERIFY_FAIL",
                bot=incident.device_id,
                subsystem=incident.subsystem,
                id=incident.incident_id,
            )
            if incident.status == "fixing":
                incident.status = "open"
            return False
        self._record_learning_outcomes(incident, verified=True)
        incident.status = "resolved"
        incident.resolved_at = datetime.now(timezone.utc).isoformat()
        incident.report = reason
        self._grace.reset(incident.signature)
        self._grace.reset(f"smoke:{incident.signature}")
        _log(
            "VERIFIED",
            bot=incident.device_id,
            subsystem=incident.subsystem,
            reason="cross_check",
        )
        return True

    def _maybe_email(self, incident: Incident) -> bool:
        if incident.email_sent:
            return False
        from intelligent_mode.incident_filters import should_email_incident

        if not should_email_incident(incident):
            return False
        debug = incident.debug_report if isinstance(incident.debug_report, dict) else {}
        code_bug = debug.get("code_bug") if isinstance(debug.get("code_bug"), dict) else {}
        is_code_bug = bool(code_bug.get("is_code_bug"))
        should_send = False
        if incident.status == "escalated" and self.config.email_on_escalate:
            should_send = True
        if incident.status == "resolved" and self.config.email_on_resolve:
            if not self._is_code_bug(incident):
                vr = incident.verification_report if isinstance(incident.verification_report, dict) else {}
                if vr.get("passed") is True:
                    should_send = True
        if incident.status == "open" and incident.severity == "critical":
            should_send = True
        if is_code_bug and self.config.email_code_bugs:
            should_send = True
        if str(debug.get("category") or "") in {"logic_bug", "regression"} and self.config.email_code_bugs:
            should_send = True
        if not should_send:
            return False
        if not email_configured(self.config):
            return False

        use_llm = self.config.llm_reports
        if incident.severity == "critical" or incident.status == "escalated":
            use_llm = False
        incident.report = build_report(incident, use_llm=use_llm)
        if self._digest.enqueue(incident):
            return False
        ok, _detail = send_incident_email(incident, incident.report, config=self.config)
        if ok:
            incident.email_sent = True
        return ok

    def run_remediation_pass(
        self,
        snapshot: dict[str, Any],
        extra_candidates: list | None = None,
    ) -> dict[str, Any]:
        """Open incidents, Intelligent Mode fixes, verify, and email for test/detect failures."""
        ctx = get_context()
        candidates = list(extra_candidates or [])
        candidates.extend(
            detect_anomalies(
                snapshot,
                grace=self._grace,
                camera_grace_seconds=self.config.camera_grace_seconds,
                llm_grace_seconds=self.config.llm_grace_seconds,
                voice_active_fn=ctx.voice_active_fn,
                baseline_enabled=self.config.baseline_anomaly_enabled,
                baseline_sigma=self.config.baseline_sigma_threshold,
                baseline_min_samples=self.config.baseline_min_samples,
                baseline_grace_seconds=self.config.baseline_grace_seconds,
            )
        )

        open_signatures = {
            inc.signature
            for inc in self.store.list_incidents(limit=500)
            if inc.status in {"open", "fixing", "escalated"}
        }
        from intelligent_mode.incident_filters import (
            dedupe_detection_candidates,
            should_open_incident,
            should_suppress_incident,
        )

        candidates = dedupe_detection_candidates(
            candidates, open_signatures=open_signatures
        )

        opened = 0
        fix_events = 0
        resolved = 0
        emailed = 0

        for candidate in candidates:
            if not should_open_incident(
                candidate, snapshot, open_signatures=open_signatures
            ):
                reason = (
                    "benign_pattern"
                    if should_suppress_incident(
                        candidate.error,
                        candidate.subsystem,
                        snapshot,
                        device_id=candidate.device_id,
                    )
                    else "no_open_rule"
                )
                _log(
                    "SKIP_DETECT",
                    bot=candidate.device_id,
                    subsystem=candidate.subsystem,
                    reason=reason,
                )
                continue
            incident = candidate_to_incident(candidate, snapshot)
            incident, created = self.store.open_incident(incident)
            if created:
                opened += 1
                _log(
                    "DETECT",
                    bot=incident.device_id,
                    subsystem=incident.subsystem,
                    tier=incident.tier,
                )
            self._debug_incident(incident, snapshot)
            before_fixes = len(incident.fixes)
            self._maybe_fix(incident, snapshot)
            if len(incident.fixes) > before_fixes:
                fix_events += 1
                self._debug_incident(incident, snapshot)
            if self._pending_stt_recovery(incident):
                self.store.update(incident)
                continue
            if self._verify_and_finalize(incident):
                resolved += 1
            if self._maybe_email(incident):
                emailed += 1
            self.store.update(incident)

        digested = self._digest.flush_due()
        for inc in digested:
            self.store.update(inc)
            emailed += 1

        summary = {
            "candidates": len(candidates),
            "opened": opened,
            "fix_events": fix_events,
            "resolved": resolved,
            "emailed": emailed,
            "digest_sent": len(digested),
        }
        return summary


def get_orchestrator() -> IntelligentOrchestrator | None:
    return _ORCHESTRATOR


def start_intelligent_mode(
    ctx: IntelligentContext,
    *,
    config: IntelligentConfig | None = None,
) -> IntelligentOrchestrator:
    global _ORCHESTRATOR
    configure_context(ctx)
    cfg = config or load_config()
    _ORCHESTRATOR = IntelligentOrchestrator(cfg)
    _ORCHESTRATOR.start()
    return _ORCHESTRATOR


def stop_intelligent_mode() -> None:
    global _ORCHESTRATOR
    if _ORCHESTRATOR is not None:
        _ORCHESTRATOR.stop()
        _ORCHESTRATOR = None


def reload_intelligent_mode(
    ctx: IntelligentContext | None = None,
    *,
    config: IntelligentConfig | None = None,
    prune: bool = False,
) -> IntelligentOrchestrator | None:
    """Reload config + incident store without restarting the whole NiNO server."""
    global _ORCHESTRATOR
    cfg = config or load_config()
    existing_store = _ORCHESTRATOR.store if _ORCHESTRATOR is not None else IncidentStore()
    was_running = (
        _ORCHESTRATOR is not None
        and _ORCHESTRATOR._thread is not None
        and _ORCHESTRATOR._thread.is_alive()
    )
    if _ORCHESTRATOR is not None:
        _ORCHESTRATOR.stop()
    existing_store.reload()
    if prune and cfg.prune_resolved_on_start:
        existing_store.prune_resolved(keep_recent=cfg.incidents_keep_resolved)
    if not cfg.enabled:
        _ORCHESTRATOR = None
        return None
    if ctx is not None:
        configure_context(ctx)
    else:
        try:
            get_context()
        except RuntimeError:
            logger.warning("Intelligent mode reload: context not configured")
            _ORCHESTRATOR = None
            return None
    _ORCHESTRATOR = IntelligentOrchestrator(cfg, store=existing_store)
    _ORCHESTRATOR.start()
    logger.info("Intelligent mode reloaded (was_running=%s)", was_running)
    return _ORCHESTRATOR

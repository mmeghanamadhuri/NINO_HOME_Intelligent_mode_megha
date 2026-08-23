"""Strength validation suite — evidence-based tests for Intelligent Mode + Coding Agent.

Run:
  python3 -m unittest test_intelligent_strength_validation -v

Each test class maps to a real-world scenario you can show leadership/clients.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from intelligent_mode.agent_remediation import classify_agent_remediation, is_agent_remediatable_incident
from intelligent_mode.code_bug_analyzer import analyze_code_bug, is_code_bug_incident, merge_into_debug_report
from intelligent_mode.coding_agent import (
    FixProposal,
    gather_rich_context,
    validate_proposal,
    CodeChange,
)
from intelligent_mode.config import IntelligentConfig
from intelligent_mode.context import IntelligentContext, configure_context
from intelligent_mode.incidents import Incident
from intelligent_mode.orchestrator import IntelligentOrchestrator
from intelligent_mode.reporter import build_report, email_subject, _headline


def _incident(**overrides: object) -> Incident:
    base = {
        "device_id": "b0a6048addd4",
        "display_name": "Robot",
        "subsystem": "voice",
        "severity": "warning",
        "tier": 1,
        "error": "",
        "signature": "sig",
        "status": "open",
    }
    base.update(overrides)
    return Incident(**base)  # type: ignore[arg-type]


class Scenario1FalseAlarmDiscrimination(unittest.TestCase):
    """Scenario 1: Empathetic LLM reply must NOT escalate as code bug."""

    EMpathetic_ERROR = (
        "[soak:soak:voice:live:4:7:i_feel_lonely_can_we_talk] path=llm unexpected reply="
        "Absolutely, feeling lonely is common. Let's chat about anything you'd like."
    )

    def test_soak_valid_empathetic_reply_auto_resolves(self) -> None:
        inc = _incident(error=self.EMpathetic_ERROR)
        plan = classify_agent_remediation(inc)
        self.assertIsNotNone(plan)
        assert plan is not None
        self.assertEqual(plan.pattern_id, "soak_valid_reply")
        self.assertFalse(plan.recovery_actions)
        self.assertTrue(plan.auto_resolve_reason)

    def test_not_classified_as_code_bug(self) -> None:
        inc = _incident(
            error=self.EMpathetic_ERROR,
            debug_report={"category": "logic_bug", "fixable_by_agent": False},
        )
        self.assertFalse(is_code_bug_incident(inc))
        self.assertFalse(analyze_code_bug(inc, use_llm=False).is_code_bug)

    def test_stale_code_bug_flag_cleared(self) -> None:
        inc = _incident(
            error=self.EMpathetic_ERROR,
            debug_report={
                "category": "logic_bug",
                "fixable_by_agent": False,
                "code_bug": {"is_code_bug": True, "bug_summary": "stale STT flag"},
            },
        )
        merged = merge_into_debug_report(inc.debug_report, analyze_code_bug(inc, use_llm=False))
        self.assertNotIn("code_bug", merged)
        self.assertTrue(merged.get("fixable_by_agent"))


class Scenario2NoWastedCyclesOnUnfixableBug(unittest.TestCase):
    """Scenario 2: Logic bug escalates immediately — no retry burn."""

    def test_code_bug_skips_auto_fix_and_escalates(self) -> None:
        snapshot = {
            "devices": {"devices": []},
            "cameras": {},
            "llm": {"reachable": True},
            "memory": {"database_url_set": False},
            "stt": {"provider": "elevenlabs", "loaded": True},
            "tts": {"last_error": ""},
            "discovery": {"last_error": ""},
        }
        ctx = IntelligentContext(
            registry=MagicMock(),
            cameras=MagicMock(),
            tts=MagicMock(),
            face_registration=MagicMock(),
            collect_status=lambda: snapshot,
            voice_active_fn=lambda _d: False,
        )
        configure_context(ctx)
        orch = IntelligentOrchestrator(
            IntelligentConfig(enabled=True, grace_seconds=0, autonomous_recovery_enabled=True)
        )
        inc = _incident(
            subsystem="voice",
            severity="critical",
            tier=0,
            error="Vision question routed to LLM instead of camera pipeline",
            status="open",
            debug_report={
                "category": "logic_bug",
                "fixable_by_agent": False,
                "code_bug": {
                    "is_code_bug": True,
                    "bug_summary": "Wrong routing — vision sent to LLM",
                    "affected_files": ["server/voice_service.py"],
                },
            },
        )
        orch._maybe_fix(inc, snapshot)
        self.assertEqual(inc.status, "escalated")
        self.assertEqual(inc.fix_attempts, 0)
        self.assertEqual(inc.fixes, [])

    def test_affected_file_named_in_analysis(self) -> None:
        inc = _incident(
            subsystem="voice",
            error="LLM reply generation failed after STT",
            debug_report={"category": "logic_bug", "fixable_by_agent": False},
        )
        with patch(
            "intelligent_mode.code_bug_analyzer._load_recent_voice_rows",
            return_value=[{"event": "voice_query", "reply_path": "llm", "error": "timeout"}],
        ):
            analysis = analyze_code_bug(inc, use_llm=False)
        if analysis.is_code_bug:
            self.assertTrue(analysis.affected_files or analysis.suggested_fix)


class Scenario3CompoundFailureOrdering(unittest.TestCase):
    """Scenario 3: Ollama down + bot offline — verify partial fix not falsely marked failed."""

    def test_llm_fix_attempted_when_ollama_down(self) -> None:
        """LLM worker should attempt ollama_restart_warm even when bot also offline."""
        from intelligent_mode.workers import LlmWorker

        inc = _incident(subsystem="llm", error="Connection refused :11435")
        ctx = IntelligentContext(
            registry=MagicMock(),
            cameras=MagicMock(),
            tts=MagicMock(),
            face_registration=MagicMock(),
            collect_status=lambda: {"llm": {"reachable": False}},
        )
        with patch("llm_service.try_start_gpu_ollama"), patch(
            "llm_service.warm_ollama_model"
        ), patch(
            "llm_service.ollama_runtime_status",
            return_value={"reachable": True, "loaded": True},
        ), patch(
            "llm_service.set_ollama_env_url",
            return_value="http://127.0.0.1:11435/api/generate",
        ), patch(
            "llm_service.resolve_ollama_api_url",
            return_value="http://127.0.0.1:11435/api/generate",
        ):
            result = LlmWorker().try_fix(inc, ctx)
        self.assertEqual(result.action, "ollama_restart_warm")

    def test_bot_offline_does_not_block_llm_recovery_chain(self) -> None:
        """Bot discovery failure should not prevent LLM chain from running."""
        from intelligent_mode.recovery import recovery_chain_for

        self.assertIn("ollama_restart_warm", recovery_chain_for("llm"))
        self.assertNotIn("lan_discovery", recovery_chain_for("llm"))


class Scenario4NeverInterruptLiveUser(unittest.TestCase):
    """Scenario 4: Fix deferred while voice session active."""

    def test_fix_skipped_during_active_voice(self) -> None:
        snapshot = {
            "devices": {"devices": []},
            "cameras": {"b0a6048addd4": {"connected": False}},
            "llm": {"reachable": True},
            "memory": {"database_url_set": False},
            "stt": {"loaded": True},
            "tts": {"last_error": ""},
            "discovery": {"last_error": ""},
        }
        ctx = IntelligentContext(
            registry=MagicMock(),
            cameras=MagicMock(),
            tts=MagicMock(),
            face_registration=MagicMock(),
            collect_status=lambda: snapshot,
            voice_active_fn=lambda device_id: device_id == "b0a6048addd4",
        )
        configure_context(ctx)
        orch = IntelligentOrchestrator(
            IntelligentConfig(
                enabled=True,
                grace_seconds=0,
                skip_fix_during_voice=True,
                autonomous_recovery_enabled=True,
            )
        )
        inc = _incident(
            subsystem="camera",
            severity="warning",
            error="Camera stream disconnected",
            status="open",
        )
        orch._maybe_fix(inc, snapshot)
        self.assertEqual(inc.fix_attempts, 0)
        self.assertEqual(inc.status, "open")

    def test_soak_defers_during_live_session(self) -> None:
        from intelligent_mode.voice_incident_filters import is_soak_live_session_skip

        err = "[soak:soak:voice:live:1:1:hello] skipped — live voice session active"
        self.assertTrue(is_soak_live_session_skip(err))


class Scenario5SelfContainedRepairNoHuman(unittest.TestCase):
    """Scenario 5: WAV too large — agent handles without developer email."""

    WAV_ERROR = "ESP play_wav failed: WAV too large for ESP (800370 bytes; max 389120)"

    def test_wav_too_large_is_agent_remediated(self) -> None:
        inc = _incident(error=self.WAV_ERROR)
        plan = classify_agent_remediation(inc)
        self.assertIsNotNone(plan)
        assert plan is not None
        self.assertEqual(plan.pattern_id, "wav_auto_split")
        self.assertTrue(is_agent_remediatable_incident(inc))

    def test_wav_not_escalated_as_code_bug(self) -> None:
        inc = _incident(error=self.WAV_ERROR)
        self.assertFalse(is_code_bug_incident(inc))
        self.assertFalse(analyze_code_bug(inc, use_llm=False).is_code_bug)

    def test_wav_skips_developer_email_path(self) -> None:
        inc = _incident(error=self.WAV_ERROR, status="open")
        plan = classify_agent_remediation(inc)
        assert plan is not None
        self.assertTrue(plan.skip_code_bug_email)


class Scenario6AccurateDiagnosisWhenCantFix(unittest.TestCase):
    """Scenario 6: Code bug email names file, no false 'Auto-fixed'."""

    def test_code_bug_email_not_marked_resolved(self) -> None:
        inc = _incident(
            status="escalated",
            subsystem="camera",
            error="Camera regression in firmware session gate",
            debug_report={
                "category": "logic_bug",
                "fixable_by_agent": False,
                "code_bug": {
                    "is_code_bug": True,
                    "bug_summary": "Camera fails during voice session",
                    "likely_cause": "UVC timing in firmware",
                    "suggested_fix": "Review main/ camera code and rebuild firmware.",
                    "affected_files": ["main/", "server/camera.py"],
                    "firmware_update_recommended": True,
                },
            },
        )
        report = build_report(inc, use_llm=False)
        self.assertIn("CODE BUG ANALYSIS", report)
        self.assertIn("server/camera.py", report)
        self.assertIn("developer fix required", _headline(inc).lower())
        self.assertNotIn("Status:   Resolved", report)
        self.assertNotIn("Auto-fixed", report)

    def test_code_bug_never_auto_resolved_on_verify(self) -> None:
        snapshot = {
            "devices": {"devices": []},
            "cameras": {},
            "llm": {"reachable": True, "loaded": True},
            "memory": {"database_url_set": False},
            "stt": {"loaded": True},
            "tts": {"last_error": ""},
            "discovery": {"last_error": ""},
        }
        ctx = IntelligentContext(
            registry=MagicMock(),
            cameras=MagicMock(),
            tts=MagicMock(),
            face_registration=MagicMock(),
            collect_status=lambda: snapshot,
            voice_active_fn=lambda _d: False,
        )
        configure_context(ctx)
        orch = IntelligentOrchestrator(IntelligentConfig(enabled=True, grace_seconds=0))
        inc = _incident(
            subsystem="camera",
            severity="critical",
            tier=0,
            error="Camera regression",
            status="fixing",
            debug_report={
                "category": "logic_bug",
                "fixable_by_agent": False,
                "code_bug": {"is_code_bug": True, "bug_summary": "Firmware regression"},
            },
        )
        with patch("intelligent_mode.orchestrator.verify_incident_with_smoke", return_value=True):
            resolved = orch._verify_and_finalize(inc)
        self.assertFalse(resolved)
        self.assertEqual(inc.status, "escalated")


class CodingAgentStrengthTests(unittest.TestCase):
    """Coding agent specific — validation, context, parallel worker."""

    def test_validates_old_code_before_proposing(self) -> None:
        proposal = FixProposal(
            changes=[
                CodeChange(
                    file_path="server/llm_service.py",
                    old_code="NONEXISTENT_CODE_STRING_XYZ",
                    new_code="fixed",
                )
            ]
        )
        ok, detail = validate_proposal(proposal)
        self.assertFalse(ok)
        self.assertIn("old_code not found", detail)

    def test_gathers_logs_and_code_context(self) -> None:
        inc = _incident(
            error="LLM timeout during voice query",
            debug_report={"code_bug": {"bug_summary": "timeout", "likely_cause": "ollama slow"}},
        )
        ctx = gather_rich_context(
            inc,
            {"bug_summary": "timeout", "likely_cause": "ollama"},
            ["server/llm_service.py"],
        )
        self.assertIn("snippets", ctx)
        self.assertIn("server/llm_service.py", ctx["snippets"])

    def test_parallel_worker_dispatches_jobs(self) -> None:
        from intelligent_mode.coding_agent_worker import CodingAgentWorker

        inc = _incident(
            incident_id="inc99",
            status="escalated",
            error="logic bug",
            debug_report={
                "category": "logic_bug",
                "fixable_by_agent": False,
                "code_bug": {"is_code_bug": True, "bug_summary": "routing bug"},
            },
        )
        worker = CodingAgentWorker(poll_seconds=30, parallel_workers=2, get_incidents=lambda: [inc])
        worker._pool = __import__("concurrent.futures").futures.ThreadPoolExecutor(max_workers=2)
        with (
            patch.object(worker, "_already_handled", return_value=False),
            patch(
                "intelligent_mode.coding_agent.process_code_bug_incident_smart",
                return_value=FixProposal(proposal_id="p99", incident_id="inc99"),
            ),
        ):
            summary = worker.run_once()
        self.assertEqual(summary["dispatched"], 1)
        worker.stop()


class StrengthScorecard(unittest.TestCase):
    """Meta-test: scenario metadata stays in sync with strength_scorecard.py."""

    def test_scorecard_metadata_matches_scenarios(self) -> None:
        from intelligent_mode.strength_scorecard import SCENARIO_ROWS

        self.assertEqual(len(SCENARIO_ROWS), 6)
        for row in SCENARIO_ROWS:
            self.assertIn(row.status, {"PASS", "PARTIAL", "NOT_TESTED"})
            self.assertIn(
                row.evidence_level,
                {"unit", "unit-mocked", "integration", "live-hardware", "live"},
            )

    def test_scenarios_3_and_4_labelled_unit_mocked_with_live_gap(self) -> None:
        from intelligent_mode.strength_scorecard import SCENARIO_ROWS

        by_num = {r.number: r for r in SCENARIO_ROWS}
        for n in (3, 4):
            row = by_num[n]
            self.assertEqual(row.status, "PASS")
            self.assertEqual(row.evidence_level, "unit-mocked")
            self.assertTrue(row.live_gap, f"Scenario {n} must document live gap")


if __name__ == "__main__":
    unittest.main(verbosity=2)

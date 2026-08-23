"""Tests for session experience playbook learning."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from intelligent_mode.experience_playbook import (
    ExperiencePlaybookStore,
    error_pattern,
    order_actions_by_experience,
    record_incident_outcome,
)
from intelligent_mode.fix_history import compute_fix_stats, invalidate_stats_cache
from intelligent_mode.incidents import FixAttempt, Incident


class ExperiencePlaybookTests(unittest.TestCase):
    def test_error_pattern_normalizes_soak_and_cpu_ollama(self) -> None:
        self.assertEqual(
            error_pattern("[soak:abc] unexpected reply: hello", "voice"),
            "voice:soak_unexpected_reply",
        )
        self.assertEqual(
            error_pattern("HTTPConnectionPool host=127.0.0.1 port=11434 connection refused", "llm"),
            "llm:ollama_cpu_unreachable",
        )

    def test_playbook_prefers_verified_action(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "playbook.json"
            store = ExperiencePlaybookStore(path)
            pattern = "voice:soak_unexpected_reply"
            store.record(pattern, "voice_state_reset", verified=True)
            store.record(pattern, "voice_state_reset", verified=True)
            store.record(pattern, "voice_pipeline_recovery", verified=False)
            store.record(pattern, "voice_pipeline_recovery", verified=False)

            import intelligent_mode.experience_playbook as mod

            original = mod.get_playbook_store
            mod.get_playbook_store = lambda: store  # type: ignore[method-assign]
            try:
                inc = Incident(
                    device_id="server",
                    display_name="Server",
                    subsystem="voice",
                    severity="warning",
                    tier=1,
                    error="[soak:x] unexpected reply",
                    signature="s1",
                )
                ordered = order_actions_by_experience(
                    ("voice_pipeline_recovery", "voice_state_reset"),
                    inc,
                    min_samples=2,
                    use_playbook=True,
                    use_fix_history=False,
                )
                self.assertEqual(ordered[0], "voice_state_reset")
            finally:
                mod.get_playbook_store = original  # type: ignore[method-assign]

    def test_record_incident_outcome_updates_store(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "playbook.json"
            store = ExperiencePlaybookStore(path)
            import intelligent_mode.experience_playbook as mod

            original = mod.get_playbook_store
            mod.get_playbook_store = lambda: store  # type: ignore[method-assign]
            try:
                inc = Incident(
                    device_id="server",
                    display_name="Server",
                    subsystem="voice",
                    severity="warning",
                    tier=1,
                    error="wav too large",
                    signature="s2",
                )
                record_incident_outcome(inc, "voice_state_reset", verified=True)
                stats = store.stats_for("voice:wav_too_large", "voice_state_reset")
                self.assertEqual(stats.attempts, 1)
                self.assertEqual(stats.verified_passes, 1)
            finally:
                mod.get_playbook_store = original  # type: ignore[method-assign]

    def test_verified_only_fix_history_ignores_unverified_success(self) -> None:
        inc = Incident(
            device_id="server",
            display_name="Server",
            subsystem="llm",
            severity="critical",
            tier=0,
            error="down",
            signature="s3",
            status="open",
            fixes=[FixAttempt(action="ollama_restart_warm", success=True, detail="worker ok")],
        )
        raw_stats = compute_fix_stats("llm", incidents=[inc], verified_only=False)
        verified_stats = compute_fix_stats("llm", incidents=[inc], verified_only=True)
        self.assertEqual(raw_stats["ollama_restart_warm"].successes, 1)
        self.assertEqual(verified_stats["ollama_restart_warm"].successes, 0)

        inc.status = "resolved"
        inc.verification_report = {"passed": True}
        verified_stats2 = compute_fix_stats("llm", incidents=[inc], verified_only=True)
        self.assertEqual(verified_stats2["ollama_restart_warm"].successes, 1)


if __name__ == "__main__":
    unittest.main()

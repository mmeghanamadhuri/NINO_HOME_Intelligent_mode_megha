"""Tests for fix-history learning and chain reordering."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from intelligent_mode.fix_history import (
    compute_fix_stats,
    invalidate_stats_cache,
    order_chain_by_success_rate,
)
from intelligent_mode.incidents import FixAttempt, Incident
from intelligent_mode.recovery import next_recovery_action, ordered_recovery_chain


class FixHistoryTests(unittest.TestCase):
    def test_order_chain_prefers_higher_success_rate(self) -> None:
        invalidate_stats_cache()
        incidents = [
            Incident(
                device_id="server",
                display_name="Server",
                subsystem="camera",
                severity="warning",
                tier=1,
                error="down",
                signature="s1",
                fixes=[
                    FixAttempt(action="camera_restart", success=False, detail="fail"),
                    FixAttempt(action="camera_restart", success=False, detail="fail"),
                    FixAttempt(action="lan_discovery", success=True, detail="ok"),
                    FixAttempt(action="lan_discovery", success=True, detail="ok"),
                ],
            )
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "incidents.json"
            path.write_text(
                json.dumps({"incidents": [inc.to_dict() for inc in incidents]}),
                encoding="utf-8",
            )
            ordered = order_chain_by_success_rate(
                ("camera_restart", "lan_discovery"),
                "camera",
                exclude=set(),
                min_samples=2,
                path=path,
            )
            self.assertEqual(ordered[0], "lan_discovery")

    def test_next_action_uses_history_order(self) -> None:
        invalidate_stats_cache()
        inc = Incident(
            device_id="server",
            display_name="Server",
            subsystem="llm",
            severity="critical",
            tier=0,
            error="down",
            signature="server:llm:down",
        )
        chain = ordered_recovery_chain(inc, use_history=False)
        self.assertEqual(chain, ("ollama_restart_warm", "ollama_cpu_fallback"))
        self.assertEqual(next_recovery_action(inc, use_history=False), "ollama_restart_warm")

    def test_compute_fix_stats(self) -> None:
        inc = Incident(
            device_id="server",
            display_name="Server",
            subsystem="voice",
            severity="warning",
            tier=1,
            error="stuck",
            signature="server:voice:stuck",
            fixes=[
                FixAttempt(action="voice_state_reset", success=True, detail="ok"),
                FixAttempt(action="voice_pipeline_recovery", success=False, detail="no"),
            ],
        )
        stats = compute_fix_stats("voice", incidents=[inc])
        self.assertEqual(stats["voice_state_reset"].success_rate, 1.0)
        self.assertEqual(stats["voice_pipeline_recovery"].success_rate, 0.0)


if __name__ == "__main__":
    unittest.main()

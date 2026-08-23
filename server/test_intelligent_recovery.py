"""Tests for Intelligent Mode recovery chains."""

from __future__ import annotations

import unittest

from intelligent_mode.incidents import Incident
from intelligent_mode.recovery import (
    RECOVERY_FIX_ACTIONS,
    chain_exhausted,
    next_recovery_action,
    recovery_chain_for,
)


class RecoveryChainTests(unittest.TestCase):
    def test_memory_chain_order(self) -> None:
        self.assertEqual(
            recovery_chain_for("memory"),
            ("postgres_start", "memory_reconnect"),
        )

    def test_next_action_skips_tried(self) -> None:
        from intelligent_mode.incidents import FixAttempt

        inc = Incident(
            device_id="server",
            display_name="Server",
            subsystem="memory",
            severity="warning",
            tier=1,
            error="db down",
            signature="server:memory:db",
        )
        inc.fixes.append(
            FixAttempt(action="postgres_start", success=True, detail="ok")
        )
        self.assertEqual(next_recovery_action(inc), "memory_reconnect")

    def test_chain_exhausted(self) -> None:
        from intelligent_mode.incidents import FixAttempt

        inc = Incident(
            device_id="server",
            display_name="Server",
            subsystem="llm",
            severity="critical",
            tier=0,
            error="down",
            signature="server:llm:down",
        )
        for action in recovery_chain_for("llm"):
            inc.fixes.append(FixAttempt(action=action, success=False, detail="x"))
        self.assertTrue(chain_exhausted(inc))

    def test_extended_whitelist_includes_recovery_actions(self) -> None:
        self.assertIn("postgres_start", RECOVERY_FIX_ACTIONS)
        self.assertIn("voice_pipeline_recovery", RECOVERY_FIX_ACTIONS)
        self.assertIn("ollama_cpu_fallback", RECOVERY_FIX_ACTIONS)


if __name__ == "__main__":
    unittest.main()

"""Tests for LLM fix selection helpers."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from intelligent_mode.incidents import Incident
from intelligent_mode.llm_fix_selector import (
    FixSelection,
    confidence_meets,
    select_fix_action,
)


class LlmFixSelectorTests(unittest.TestCase):
    def test_confidence_meets(self) -> None:
        self.assertTrue(confidence_meets("high", "medium"))
        self.assertTrue(confidence_meets("medium", "medium"))
        self.assertFalse(confidence_meets("low", "medium"))

    def test_select_fix_action_parses_json(self) -> None:
        incident = Incident(
            device_id="server",
            display_name="Server",
            subsystem="llm",
            severity="critical",
            tier=0,
            error="Ollama unreachable",
            signature="server:llm:down",
        )
        snapshot = {"llm": {"reachable": False}, "stt": {}, "memory": {}}
        payload = (
            '{"action":"ollama_restart_warm","confidence":"high",'
            '"reasoning":"Ollama is down; warm restart is the first safe step."}'
        )
        with patch("llm_service.ollama_is_reachable", return_value=True):
            with patch("llm_service.ollama_generate", return_value=payload):
                selection = select_fix_action(
                    incident,
                    snapshot,
                    tried_actions=set(),
                    min_confidence="medium",
                )
        self.assertIsNotNone(selection)
        assert selection is not None
        self.assertEqual(selection.action, "ollama_restart_warm")
        self.assertEqual(selection.confidence, "high")

    def test_rejects_non_whitelisted_action(self) -> None:
        incident = Incident(
            device_id="server",
            display_name="Server",
            subsystem="llm",
            severity="critical",
            tier=0,
            error="Ollama unreachable",
            signature="server:llm:down",
        )
        payload = '{"action":"rm_rf_everything","confidence":"high","reasoning":"bad"}'
        with patch("llm_service.ollama_is_reachable", return_value=True):
            with patch("llm_service.ollama_generate", return_value=payload):
                selection = select_fix_action(
                    incident,
                    {},
                    tried_actions=set(),
                    min_confidence="medium",
                )
        self.assertIsNotNone(selection)
        assert selection is not None
        self.assertIsNone(selection.action)

    def test_fix_selection_to_dict(self) -> None:
        item = FixSelection(action="voice_state_reset", confidence="medium", reasoning="reset")
        self.assertEqual(item.to_dict()["action"], "voice_state_reset")


if __name__ == "__main__":
    unittest.main()

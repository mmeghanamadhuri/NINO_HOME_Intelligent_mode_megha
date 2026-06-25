"""Tests for LLM memory turn JSON parsing."""

from __future__ import annotations

import unittest

from llm_service import MemoryTurnDecision, _parse_memory_turn_json


class LlmMemoryTurnTests(unittest.TestCase):
    def test_parse_store_action(self) -> None:
        raw = """
        {"action":"store","recall_keys":[],"store":[
          {"key":"favorite_drink","memory":"Chakri's favorite drink is coffee","importance":8}
        ]}
        """
        decision = _parse_memory_turn_json(raw)
        self.assertEqual(decision.action, "store")
        self.assertEqual(len(decision.store), 1)
        self.assertEqual(decision.store[0]["key"], "favorite_drink")

    def test_parse_recall_action(self) -> None:
        raw = '{"action":"recall","recall_keys":["birthdate","favorite_drink"],"store":[]}'
        decision = _parse_memory_turn_json(raw)
        self.assertEqual(decision.action, "recall")
        self.assertEqual(decision.recall_keys, ["birthdate", "favorite_drink"])

    def test_invalid_json_defaults_to_chat(self) -> None:
        decision = _parse_memory_turn_json("not json at all")
        self.assertEqual(decision.action, "chat")
        self.assertIsInstance(decision, MemoryTurnDecision)


if __name__ == "__main__":
    unittest.main()

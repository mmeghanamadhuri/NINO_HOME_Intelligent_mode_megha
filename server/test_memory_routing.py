"""Tests for memory store/recall routing (no hardcoded empty recall)."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from memory_filters import is_memory_recall_question, is_preference_update_statement
from memory_service import MemoryService


class MemoryRoutingTests(unittest.TestCase):
    def test_lemon_rice_is_store_not_recall(self) -> None:
        text = "My favorite food is lemon rice."
        self.assertTrue(is_preference_update_statement(text))
        self.assertFalse(is_memory_recall_question(text))

    def test_what_is_favorite_food_is_recall(self) -> None:
        text = "What is my favorite food?"
        self.assertTrue(is_memory_recall_question(text))
        self.assertFalse(is_preference_update_statement(text))

    @patch("memory_service.MemoryService.upsert_preference_from_utterance", return_value="favorite_food")
    @patch("memory_service.MemoryService.get_memory_text_by_key", return_value="lemon rice is my favorite food")
    @patch("llm_service.answer_memory_store_ack", return_value="Got it, I'll remember lemon rice.")
    def test_handle_turn_stores_preference_before_llm(
        self,
        mock_ack: MagicMock,
        mock_get: MagicMock,
        mock_upsert: MagicMock,
    ) -> None:
        svc = MemoryService.__new__(MemoryService)
        svc._ready = True

        path, reply = svc.handle_llm_memory_turn(
            1,
            "My favorite food is lemon rice.",
            person_name="Uday",
            model="test",
            api_url="http://test",
        )

        self.assertEqual(path, "memory_llm_store")
        self.assertEqual(reply, "Got it, I'll remember lemon rice.")
        mock_upsert.assert_called_once()
        mock_ack.assert_called_once()

    @patch("llm_service.analyze_memory_turn")
    def test_fragment_favorite_does_not_hit_recall_path(self, mock_analyze: MagicMock) -> None:
        from llm_service import MemoryTurnDecision

        mock_analyze.return_value = MemoryTurnDecision(
            action="recall", recall_keys=["favorite_food"]
        )
        svc = MemoryService.__new__(MemoryService)
        svc._ready = True
        svc.list_memory_keys_for_user = MagicMock(return_value=[])

        result = svc.handle_llm_memory_turn(1, "favorite", person_name="Uday")
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()

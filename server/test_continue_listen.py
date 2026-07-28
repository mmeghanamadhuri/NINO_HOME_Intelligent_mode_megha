"""Tests for post-LLM-reply continue-listen (mic wake without wake word)."""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from voice_service import (
    conversation_goodbye_reply,
    is_conversation_goodbye,
    should_continue_listen_after_reply,
)


class ConversationGoodbyeTests(unittest.TestCase):
    def test_goodbye_phrases(self) -> None:
        for text in (
            "goodbye",
            "Good bye",
            "bye",
            "Bye",
            "Bye.",
            "ok bye",
            "bye bye",
            "see you later",
            "talk to you later",
            "that's all",
            "I'm done",
            "stop listening",
            "end the conversation",
        ):
            with self.subTest(text=text):
                self.assertTrue(is_conversation_goodbye(text))

    def test_not_goodbye(self) -> None:
        for text in (
            "what is a microcontroller",
            "tell me a joke",
            "who am I",
            "continue",
            "nearby shops",
        ):
            with self.subTest(text=text):
                self.assertFalse(is_conversation_goodbye(text))

    def test_goodbye_reply_has_no_follow_up(self) -> None:
        reply = conversation_goodbye_reply()
        self.assertTrue(reply.strip())
        lower = reply.lower()
        self.assertFalse("?" in reply)
        self.assertNotIn("what's", lower)
        self.assertNotIn("how can i", lower)


class ContinueListenGateTests(unittest.TestCase):
    def test_llm_paths_continue(self) -> None:
        for path in (
            "llm",
            "identity_llm",
            "memory_llm_store",
            "memory_llm_recall",
            "recap",
            "recap_answer",
            "recap_not_found",
            "joke",
        ):
            with self.subTest(path=path):
                self.assertTrue(
                    should_continue_listen_after_reply(path, "tell me more")
                )

    def test_non_llm_paths_skip(self) -> None:
        for path in ("volume", "alarm", "servo_360", "weather", "local_time", "face_registration"):
            with self.subTest(path=path):
                self.assertFalse(
                    should_continue_listen_after_reply(path, "set volume to 50")
                )

    def test_goodbye_stops_listen(self) -> None:
        self.assertFalse(should_continue_listen_after_reply("llm", "goodbye"))
        self.assertFalse(should_continue_listen_after_reply("llm", "Bye"))
        self.assertFalse(should_continue_listen_after_reply("goodbye", "bye"))

    def test_env_disable(self) -> None:
        with patch.dict(os.environ, {"VOICE_CONTINUE_LISTEN": "0"}):
            self.assertFalse(
                should_continue_listen_after_reply("llm", "tell me more")
            )


if __name__ == "__main__":
    unittest.main()

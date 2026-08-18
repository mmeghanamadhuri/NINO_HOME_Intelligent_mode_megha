"""Tests for per-device voice session context."""

from __future__ import annotations

import unittest

from device_session import (
    append_device_session_turn,
    clear_device_session,
    format_device_session_prompt,
    get_device_session_turns,
)
from memory_filters import query_needs_recent_context


class DeviceSessionTests(unittest.TestCase):
    def setUp(self) -> None:
        clear_device_session("test-device")

    def tearDown(self) -> None:
        clear_device_session("test-device")

    def test_session_accumulates_until_goodbye(self) -> None:
        append_device_session_turn(
            "test-device",
            "2 times 4",
            "Four times two is eight.",
        )
        append_device_session_turn(
            "test-device",
            "Let's do divisions.",
            "What are the two numbers?",
        )
        turns = get_device_session_turns("test-device")
        self.assertEqual(len(turns), 2)
        self.assertEqual(turns[-1][0], "Let's do divisions.")

        clear_device_session("test-device")
        self.assertEqual(get_device_session_turns("test-device"), [])

    def test_session_prompt_mentions_continue(self) -> None:
        append_device_session_turn(
            "test-device",
            "6 and 2",
            "placeholder",
        )
        block = format_device_session_prompt(
            get_device_session_turns("test-device"),
        )
        self.assertIn("Current voice session", block)
        self.assertIn("6 and 2", block)
        self.assertIn("goodbye", block.lower())


class ShortSessionAnswerTests(unittest.TestCase):
    def test_numeric_pair_needs_context(self) -> None:
        self.assertTrue(query_needs_recent_context("6 and 2"))
        self.assertTrue(query_needs_recent_context("100 divided by 5"))

    def test_yes_no_needs_context(self) -> None:
        self.assertTrue(query_needs_recent_context("yes"))
        self.assertTrue(query_needs_recent_context("sure"))


if __name__ == "__main__":
    unittest.main()

"""Time-of-day greetings follow the PC system clock, not the user's wording."""

from __future__ import annotations

import unittest
from datetime import datetime
from unittest.mock import patch

from alarm_time import day_part, day_part_greeting
from memory_filters import query_needs_recent_context
from voice_service import (
    is_howareyou_question,
    is_time_of_day_greeting,
    is_wellbeing_status_reply,
    time_of_day_greeting_reply,
    wellbeing_status_reply,
)


class DayPartGreetingTests(unittest.TestCase):
    def test_day_parts(self) -> None:
        self.assertEqual(day_part(datetime(2026, 7, 29, 8, 0, 0)), "morning")
        self.assertEqual(day_part(datetime(2026, 7, 29, 14, 0, 0)), "afternoon")
        self.assertEqual(day_part(datetime(2026, 7, 29, 18, 42, 0)), "evening")
        self.assertEqual(day_part(datetime(2026, 7, 29, 23, 0, 0)), "night")

    def test_evening_greeting_phrase(self) -> None:
        self.assertEqual(
            day_part_greeting(datetime(2026, 7, 29, 18, 42, 0)),
            "Good evening",
        )

    def test_detects_morning_greeting_phrases(self) -> None:
        self.assertTrue(is_time_of_day_greeting("Good morning, everyone"))
        self.assertTrue(is_time_of_day_greeting("Morning"))
        self.assertTrue(is_time_of_day_greeting("hi"))
        self.assertFalse(is_time_of_day_greeting("Good morning, what's the weather?"))
        self.assertFalse(is_time_of_day_greeting("Tell me about Mars"))

    @patch("alarm_time.system_now", return_value=datetime(2026, 7, 29, 18, 42, 0))
    def test_reply_uses_evening_when_user_says_morning(self, _now) -> None:
        reply = time_of_day_greeting_reply("Chakri")
        self.assertTrue(reply.startswith("Good evening Chakri"))
        self.assertNotIn("morning", reply.lower())

    def test_wellbeing_smalltalk(self) -> None:
        self.assertTrue(is_howareyou_question("How are you?"))
        self.assertTrue(is_wellbeing_status_reply("I am great"))
        self.assertTrue(query_needs_recent_context("I am great"))
        reply = wellbeing_status_reply("Kartik")
        self.assertNotIn("assist", reply.lower())
        self.assertNotIn("how can i help", reply.lower())


if __name__ == "__main__":
    unittest.main()

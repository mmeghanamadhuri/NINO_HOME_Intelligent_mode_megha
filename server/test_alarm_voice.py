"""Tests for alarm voice command routing."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from alarm_medical import is_medical_set_command, looks_like_medicine_reminder_set
from alarm_voice import handle_alarm_voice, is_set_alarm_command, parse_alarm_datetime


class AlarmVoiceRoutingTests(unittest.TestCase):
    def test_set_alarm_phrases_recognized(self) -> None:
        for text in (
            "Alarm at 6 12 p.m. Today",
            "alarm at 6.13 p.m. today.",
        ):
            self.assertTrue(is_set_alarm_command(text), msg=text)

    def test_parse_whisper_time_phrases(self) -> None:
        for phrase in ("6 12 p.m. Today", "6:13 p.m. today"):
            parsed = parse_alarm_datetime(phrase)
            self.assertIsNone(parsed.error, msg=phrase)
            self.assertIsNotNone(parsed.fire_at, msg=phrase)

    @patch("alarm_voice._save_alarm")
    def test_set_alarm_not_treated_as_followup(self, save_alarm) -> None:
        from alarm_voice import AlarmVoiceResult

        save_alarm.return_value = AlarmVoiceResult(
            handled=True, reply="OK Chakri, alarm set for 6:12 PM today."
        )
        result = handle_alarm_voice(
            "Alarm at 6 12 p.m. Today",
            person_name="Chakri",
            user_id=1,
        )
        self.assertTrue(result.handled)
        self.assertIn("alarm set", result.reply.lower())
        save_alarm.assert_called_once()

    def test_whisper_garbled_medical_reminders_detected(self) -> None:
        phrases = (
            "Find me to take medicines at 6.22pm today.",
            "me to take medicines at 6.22pm today.",
            "to take medicines at 6.23 pm today.",
        )
        for text in phrases:
            with self.subTest(text=text):
                self.assertTrue(looks_like_medicine_reminder_set(text), msg=text)
                self.assertTrue(is_medical_set_command(text), msg=text)

    @patch("alarm_voice._save_alarm")
    def test_whisper_garbled_medical_reminder_sets_alarm(self, save_alarm) -> None:
        from alarm_voice import AlarmVoiceResult

        save_alarm.return_value = AlarmVoiceResult(
            handled=True,
            reply="OK Chakri, I will remind you to take medicines at 6:22 PM today.",
        )
        result = handle_alarm_voice(
            "Find me to take medicines at 6.22pm today.",
            person_name="Chakri",
            user_id=1,
        )
        self.assertTrue(result.handled)
        self.assertIn("remind you", result.reply.lower())
        save_alarm.assert_called_once()
        _, kwargs = save_alarm.call_args
        self.assertTrue(kwargs.get("force_medical"))


if __name__ == "__main__":
    unittest.main()

"""Regression tests for speaker volume voice commands."""

from __future__ import annotations

import unittest

from voice_service import parse_volume_command


class VolumeCommandTests(unittest.TestCase):
    def test_garbled_speech_does_not_trigger_volume(self) -> None:
        self.assertIsNone(
            parse_volume_command("would love to drink comes up all the time.")
        )
        self.assertIsNone(parse_volume_command("comes up all the time"))

    def test_real_volume_commands_still_parse(self) -> None:
        self.assertEqual(parse_volume_command("turn volume up"), ("delta", 10))
        self.assertEqual(parse_volume_command("increase volume"), ("delta", 10))
        self.assertEqual(parse_volume_command("volume down"), ("delta", -10))
        self.assertEqual(parse_volume_command("set volume to fifty"), ("set", 50))


if __name__ == "__main__":
    unittest.main()

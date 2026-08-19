"""TTS context -> Dynamixel action list."""

from __future__ import annotations

import unittest

from servo_tts_motion import motion_actions_for_reply


class ServoTtsMotionTests(unittest.TestCase):
    def test_greet(self) -> None:
        self.assertEqual(
            motion_actions_for_reply("Hey Hari, how can I help you", reply_path="session_greet"),
            ["greet"],
        )

    def test_goodbye_nod(self) -> None:
        self.assertEqual(motion_actions_for_reply("Bye! See you soon.", reply_path="goodbye"), ["nod"])

    def test_question_looks(self) -> None:
        self.assertEqual(
            motion_actions_for_reply("Want to hear a joke?", reply_path="llm"),
            ["look_left", "look_right", "nod"],
        )

    def test_no_shakes(self) -> None:
        self.assertEqual(motion_actions_for_reply("No, I cannot do that.", reply_path="llm"), ["shake"])

    def test_default_talk(self) -> None:
        self.assertEqual(motion_actions_for_reply("Iron is a metal.", reply_path="llm"), ["talk"])


if __name__ == "__main__":
    unittest.main()

"""TTS context -> Dynamixel action list."""

from __future__ import annotations

import unittest

from servo_tts_motion import motion_actions_for_reply, parse_repeat_yes_no_command


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

    def test_story_uses_talk(self) -> None:
        self.assertEqual(
            motion_actions_for_reply(
                "Once upon a time a robot told a very long story about the moon.",
                reply_path="llm",
            ),
            ["talk"],
        )

    def test_long_story_keeps_talk_despite_question_or_yes(self) -> None:
        story = (
            "Once upon a time a curious robot walked across the moon and told "
            "every crater a story. Yes, it took all night, and would you like "
            "to hear what happened next when the sun came up?"
        )
        self.assertGreaterEqual(len(story.split()), 24)
        self.assertEqual(motion_actions_for_reply(story, reply_path="llm"), ["talk"])

    def test_triple_no_yes_motion(self) -> None:
        self.assertEqual(
            motion_actions_for_reply("No, no, no.", reply_path="say_no3"),
            ["shake3"],
        )
        self.assertEqual(
            motion_actions_for_reply("Yes, yes, yes.", reply_path="say_yes3"),
            ["nod3"],
        )

    def test_parse_repeat_yes_no_command(self) -> None:
        self.assertEqual(parse_repeat_yes_no_command("say no no no"), "no")
        self.assertEqual(parse_repeat_yes_no_command("no, no, no"), "no")
        self.assertEqual(parse_repeat_yes_no_command("say yes, yes, yes"), "yes")
        self.assertEqual(parse_repeat_yes_no_command("yes yes yes"), "yes")
        self.assertIsNone(parse_repeat_yes_no_command("No I cannot do that"))
        self.assertIsNone(parse_repeat_yes_no_command("tell a story"))

    def test_register_offer_has_no_curious_motion(self) -> None:
        self.assertEqual(
            motion_actions_for_reply(
                "Looks like you are a new user, can I register you",
                reply_path="session_register_offer",
            ),
            [],
        )
        self.assertEqual(
            motion_actions_for_reply("What should I call you?", reply_path="face_registration"),
            [],
        )


if __name__ == "__main__":
    unittest.main()

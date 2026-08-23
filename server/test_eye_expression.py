"""Regression tests for LLM reply -> eye expression tagging."""

from __future__ import annotations

import unittest

from eye_expression import (
    EYE_EXPRESSIONS,
    EyeContext,
    emotion_eye_from_context,
    infer_eye_expression,
    infer_eye_expression_for_response,
    normalize_eye_expression,
    spatial_eye_from_text,
)


class EyeExpressionTests(unittest.TestCase):
    def test_normalize_valid_tags(self) -> None:
        for tag in EYE_EXPRESSIONS:
            self.assertEqual(normalize_eye_expression(tag), tag)
            self.assertEqual(normalize_eye_expression(f"  {tag.upper()}  "), tag)

    def test_normalize_heart(self) -> None:
        self.assertEqual(normalize_eye_expression("heart"), "heart")
        self.assertEqual(normalize_eye_expression("  HEART  "), "heart")

    def test_session_greet_is_heart(self) -> None:
        self.assertEqual(
            infer_eye_expression_for_response(
                "Hey Hari, how can I help you",
                reply_path="session_greet",
            ),
            "heart",
        )
        self.assertIsNone(normalize_eye_expression("idle"))
        self.assertIsNone(normalize_eye_expression("thinking"))
        self.assertIsNone(normalize_eye_expression(""))

    def test_session_register_offer_has_no_eye(self) -> None:
        self.assertIsNone(
            infer_eye_expression_for_response(
                "Looks like you are a new user, can I register you",
                reply_path="session_register_offer",
            )
        )
        self.assertIsNone(
            infer_eye_expression_for_response(
                "What should I call you?",
                reply_path="face_registration",
            )
        )

    def test_non_llm_paths_omit_tag(self) -> None:
        self.assertIsNone(
            infer_eye_expression_for_response("Volume set.", reply_path="volume")
        )
        self.assertIsNone(
            infer_eye_expression_for_response("Alarm set.", reply_path="alarm")
        )
        self.assertIsNone(
            infer_eye_expression_for_response("Okay.", reply_path="servo_360")
        )

    def test_llm_sad_user_mood(self) -> None:
        tag = infer_eye_expression(
            "I'm sorry to hear that. Let's find something fun to brighten your day!",
            user_text="I feel sad today.",
            reply_path="llm",
        )
        self.assertEqual(tag, "sad")

    def test_llm_happy_joke(self) -> None:
        tag = infer_eye_expression(
            "Why don't scientists trust atoms? Because they make up everything!",
            user_text="tell me a joke",
            reply_path="llm",
        )
        self.assertEqual(tag, "happy")

    def test_llm_recalling_recap_path(self) -> None:
        tag = infer_eye_expression_for_response(
            "You asked about weather and music.",
            user_text="recap our chat",
            reply_path="recap",
        )
        self.assertEqual(tag, "recalling")

    def test_llm_neutral_info(self) -> None:
        tag = infer_eye_expression(
            "It is a metal element with atomic number 26.",
            user_text="what is iron",
            reply_path="llm",
        )
        self.assertEqual(tag, "curious")

    def test_unicode_apostrophe(self) -> None:
        tag = infer_eye_expression(
            "I'm sorry to hear that.",
            user_text="I\u2019m feeling sad",
            reply_path="llm",
        )
        self.assertEqual(tag, "sad")

    def test_camera_emotion_biases_happy(self) -> None:
        tag = infer_eye_expression(
            "Nice to chat with you today.",
            user_text="hello",
            reply_path="llm",
            context=EyeContext(
                camera_emotion="Hari (the person you're speaking to) looks happy",
                reply_path="llm",
            ),
        )
        self.assertEqual(tag, "happy")

    def test_spatial_tv_from_user_question(self) -> None:
        tag = infer_eye_expression_for_response(
            "That screen on your left is a television.",
            user_text="what is that tv",
            reply_path="look_scan_llm",
            camera_scene="a television on the left",
        )
        self.assertEqual(tag, "tv")

    def test_spatial_bulb_from_scene(self) -> None:
        self.assertEqual(
            spatial_eye_from_text("a lamp on the desk"),
            "bulb",
        )

    def test_emotion_context_parser(self) -> None:
        self.assertEqual(
            emotion_eye_from_context("Hari looks sad"),
            "sad",
        )


if __name__ == "__main__":
    unittest.main()

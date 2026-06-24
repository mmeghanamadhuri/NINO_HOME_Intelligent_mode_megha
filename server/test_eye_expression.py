"""Regression tests for LLM reply -> eye expression tagging."""

from __future__ import annotations

import unittest

from eye_expression import (
    EYE_EXPRESSIONS,
    infer_eye_expression,
    infer_eye_expression_for_response,
    normalize_eye_expression,
)


class EyeExpressionTests(unittest.TestCase):
    def test_normalize_valid_tags(self) -> None:
        for tag in EYE_EXPRESSIONS:
            self.assertEqual(normalize_eye_expression(tag), tag)
            self.assertEqual(normalize_eye_expression(f"  {tag.upper()}  "), tag)

    def test_normalize_invalid(self) -> None:
        self.assertIsNone(normalize_eye_expression("idle"))
        self.assertIsNone(normalize_eye_expression("thinking"))
        self.assertIsNone(normalize_eye_expression(""))

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


if __name__ == "__main__":
    unittest.main()

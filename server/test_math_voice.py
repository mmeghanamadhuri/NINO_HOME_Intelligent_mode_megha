"""Tests for spoken math voice handling."""

from __future__ import annotations

import unittest

from math_voice import (
    format_math_reply,
    infer_session_math_operation,
    parse_spoken_math,
    try_spoken_math_reply,
)


class SpokenMathTests(unittest.TestCase):
    def test_explicit_addition(self) -> None:
        parsed = parse_spoken_math("2 plus 4")
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed.result, 6)

    def test_pair_with_session_addition(self) -> None:
        session = [
            ("Let's do some math.", "Sure! addition or subtraction?"),
            ("Let's start with simple math, maybe additions.", "What is the first number?"),
        ]
        parsed = parse_spoken_math("2 and 2", session_turns=session)
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed.operation, "add")
        self.assertEqual(parsed.result, 4)

    def test_pair_with_session_division(self) -> None:
        session = [
            ("Let's do divisions.", "What are the two numbers?"),
        ]
        parsed = parse_spoken_math("6 and 2", session_turns=session)
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed.operation, "divide")
        self.assertEqual(parsed.result, 3)

    def test_times(self) -> None:
        parsed = parse_spoken_math("2 times 4")
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed.result, 8)

    def test_reply_format(self) -> None:
        reply = try_spoken_math_reply("2 plus 4")
        self.assertIsNotNone(reply)
        assert reply is not None
        self.assertIn("six", reply.lower())

    def test_infer_addition_from_session(self) -> None:
        session = [
            ("Let's start with simple math, maybe additions.", "Great!"),
        ]
        self.assertEqual(infer_session_math_operation(session), "add")


if __name__ == "__main__":
    unittest.main()

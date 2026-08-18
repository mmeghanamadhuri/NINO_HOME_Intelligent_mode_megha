"""Tests for spoken math voice handling."""

from __future__ import annotations

import unittest

from math_voice import (
    format_math_reply,
    infer_session_math_operation,
    parse_spoken_math,
    try_math_quiz_reply,
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


class MathQuizTests(unittest.TestCase):
    def setUp(self) -> None:
        from math_voice import clear_math_quiz

        clear_math_quiz("quiz-device")

    def tearDown(self) -> None:
        from math_voice import clear_math_quiz

        clear_math_quiz("quiz-device")

    def test_learn_additions_asks_a_real_problem(self) -> None:
        reply = try_math_quiz_reply(
            "I want to learn additions. Ask me some questions.",
            device_id="quiz-device",
        )
        self.assertIsNotNone(reply)
        assert reply is not None
        self.assertRegex(reply.lower(), r"what is .+ plus .+\?")
        self.assertNotIn("[", reply)

    def test_give_me_two_numbers_asks_problem(self) -> None:
        session = [
            ("I want to learn additions. Ask me some questions.", "Sure!"),
        ]
        reply = try_math_quiz_reply(
            "you give me two numbers",
            device_id="quiz-device",
            session_turns=session,
        )
        self.assertIsNotNone(reply)
        assert reply is not None
        self.assertRegex(reply.lower(), r"what is .+ plus .+\?")
        self.assertNotIn("insert", reply.lower())

    def test_wrong_answer_then_next_question(self) -> None:
        from math_voice import _set_math_quiz, MathQuiz, get_math_quiz

        _set_math_quiz(
            "quiz-device",
            MathQuiz(operation="add", left=2, right=3, answer=5),
        )
        reply = try_math_quiz_reply("9", device_id="quiz-device")
        self.assertIsNotNone(reply)
        assert reply is not None
        self.assertIn("Not quite", reply)
        self.assertIn("plus", reply.lower())
        nxt = get_math_quiz("quiz-device")
        self.assertIsNotNone(nxt)
        assert nxt is not None
        self.assertRegex(reply.lower(), r"what is ")

    def test_i_dont_know_reveals_and_continues(self) -> None:
        from math_voice import MathQuiz, _set_math_quiz

        _set_math_quiz(
            "quiz-device",
            MathQuiz(operation="add", left=2, right=3, answer=5),
        )
        reply = try_math_quiz_reply("I don't know.", device_id="quiz-device")
        self.assertIsNotNone(reply)
        assert reply is not None
        self.assertIn("five", reply.lower())
        self.assertRegex(reply.lower(), r"what is ")

    def test_yeah_asks_next_when_quiz_active(self) -> None:
        from math_voice import MathQuiz, _set_math_quiz

        _set_math_quiz(
            "quiz-device",
            MathQuiz(operation="add", left=2, right=3, answer=5),
        )
        reply = try_math_quiz_reply("Yeah.", device_id="quiz-device")
        self.assertIsNotNone(reply)
        assert reply is not None
        self.assertRegex(reply.lower(), r"what is .+ plus .+\?")

    def test_yeah_ignored_without_quiz(self) -> None:
        self.assertIsNone(try_math_quiz_reply("Yeah.", device_id="quiz-device"))

    def test_can_you_give_me_two_numbers(self) -> None:
        reply = try_math_quiz_reply(
            "Can you give me two numbers?",
            device_id="quiz-device",
            session_turns=[("Let's start with additions.", "Sure!")],
        )
        self.assertIsNotNone(reply)
        assert reply is not None
        self.assertRegex(reply.lower(), r"what is .+ plus .+\?")

    def test_i_think_ten_is_correct(self) -> None:
        from math_voice import MathQuiz, _set_math_quiz

        _set_math_quiz(
            "quiz-device",
            MathQuiz(operation="add", left=7, right=3, answer=10),
        )
        reply = try_math_quiz_reply("I think 10", device_id="quiz-device")
        self.assertIsNotNone(reply)
        assert reply is not None
        self.assertIn("That's right", reply)

    def test_recover_llm_seven_and_three(self) -> None:
        session = [
            (
                "Can you give me two numbers?",
                "Sure! First number: 7, Second number: 3. What's their sum?",
            )
        ]
        reply = try_math_quiz_reply(
            "I think 10",
            device_id="quiz-device",
            session_turns=session,
        )
        self.assertIsNotNone(reply)
        assert reply is not None
        self.assertIn("That's right", reply)

    def test_didnt_answer_repeats_same_problem(self) -> None:
        from math_voice import MathQuiz, _set_math_quiz

        _set_math_quiz(
            "quiz-device",
            MathQuiz(operation="add", left=7, right=3, answer=10),
        )
        reply = try_math_quiz_reply(
            "I didn't answer 7 and 3.",
            device_id="quiz-device",
        )
        self.assertIsNotNone(reply)
        assert reply is not None
        self.assertIn("seven plus three", reply.lower())
        self.assertNotIn("That's right", reply)


if __name__ == "__main__":
    unittest.main()

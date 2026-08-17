"""Tests for randomized joke replies."""

from __future__ import annotations

import unittest

import voice_service
from voice_service import (
    JOKES,
    is_football_joke_request,
    is_joke_request,
    random_joke_reply,
    should_continue_listen_after_reply,
)


class JokeRequestTests(unittest.TestCase):
    def test_joke_requests_detected(self) -> None:
        for text in (
            "Tell me a joke.",
            "tell a joke",
            "Give me a joke",
            "Send me a joke.",
            "Crack a joke.",
            "a joke",
            "joke",
            "Another joke?",
            "make me laugh",
            "cheer me up",
            "something funny",
            "Got a joke?",
        ):
            with self.subTest(text=text):
                self.assertTrue(is_joke_request(text))

    def test_non_requests_rejected(self) -> None:
        for text in (
            "It's a joke.",
            "It's only a joke.",
            "just kidding",
            "I'm joking",
            "You're a joke.",
            "Not a joke.",
            "what is photosynthesis",
        ):
            with self.subTest(text=text):
                self.assertFalse(is_joke_request(text))

    def test_football_joke_not_general(self) -> None:
        text = "Tell me a football joke."
        self.assertTrue(is_football_joke_request(text))
        self.assertFalse(is_joke_request(text))


class JokeRandomizationTests(unittest.TestCase):
    def setUp(self) -> None:
        voice_service._joke_deck = []
        voice_service._last_joke = None

    def test_reply_uses_known_joke(self) -> None:
        reply = random_joke_reply()
        self.assertTrue(reply.startswith("Sure! "))
        self.assertIn(reply.removeprefix("Sure! "), JOKES)

    def test_no_immediate_repeat_across_full_deck(self) -> None:
        seen: list[str] = []
        for _ in range(len(JOKES)):
            joke = random_joke_reply().removeprefix("Sure! ")
            seen.append(joke)
        self.assertEqual(sorted(seen), sorted(JOKES))

        # Next cycle should not start with the last joke from the previous deck.
        next_joke = random_joke_reply().removeprefix("Sure! ")
        self.assertNotEqual(next_joke, seen[-1])

    def test_continue_listen_after_joke(self) -> None:
        self.assertTrue(should_continue_listen_after_reply("joke", "tell me a joke"))
        self.assertTrue(
            should_continue_listen_after_reply(
                "joke_and_time",
                "Tell me a joke and what is the time?",
            )
        )


class CompoundShortcutTests(unittest.TestCase):
    def test_joke_and_time_is_not_exclusive_time(self) -> None:
        from voice_service import (
            is_exclusive_local_time_question,
            is_joke_request,
            is_local_time_question,
        )

        text = "Tell me a joke and what is the time?"
        self.assertTrue(is_joke_request(text))
        self.assertTrue(is_local_time_question(text))
        self.assertFalse(is_exclusive_local_time_question(text))

    def test_plain_time_stays_exclusive(self) -> None:
        from voice_service import is_exclusive_local_time_question

        for text in (
            "What is the time?",
            "what time is it",
            "tell me the time please",
        ):
            with self.subTest(text=text):
                self.assertTrue(is_exclusive_local_time_question(text), msg=text)

    def test_topic_plus_time_falls_through_exclusive_time(self) -> None:
        from voice_service import is_exclusive_local_time_question, non_time_question_text

        text = "Tell me about Mars and what is the time?"
        self.assertFalse(is_exclusive_local_time_question(text))
        self.assertEqual(non_time_question_text(text), "Tell me about Mars?")

    def test_photosynthesis_and_time_strips_to_topic(self) -> None:
        from voice_service import (
            is_exclusive_local_time_question,
            non_time_question_text,
        )

        text = "What is photosynthesis and what is the time?"
        self.assertFalse(is_exclusive_local_time_question(text))
        self.assertEqual(non_time_question_text(text), "What is photosynthesis?")
        self.assertIsNone(non_time_question_text("What is the time?"))


if __name__ == "__main__":
    unittest.main()

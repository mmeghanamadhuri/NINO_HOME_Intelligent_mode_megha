"""Tests for spoken emotion labels and mid-session change questions."""

from __future__ import annotations

import unittest

from session_emotion import (
    greet_with_emotion,
    observe_viewer_emotion,
    phrase_person_looking,
    question_for_emotion_change,
    reset_session_emotions,
)


class PhraseTests(unittest.TestCase):
    def test_greet_skips_neutral_and_uncertain(self) -> None:
        self.assertEqual(greet_with_emotion("Hari"), "Hey Hari.")
        self.assertEqual(greet_with_emotion("Hari", "neutral"), "Hey Hari.")
        self.assertEqual(greet_with_emotion("Hari", "Uncertain"), "Hey Hari.")

    def test_greet_includes_speakable_emotion(self) -> None:
        self.assertEqual(
            greet_with_emotion("Hari", "Happy"),
            "Hey Hari, you look happy.",
        )
        self.assertEqual(
            greet_with_emotion("Hari", "sad"),
            "Hey Hari, you look a bit down.",
        )

    def test_person_looking_phrase(self) -> None:
        self.assertEqual(phrase_person_looking("Hari", None), "Hari")
        self.assertEqual(
            phrase_person_looking("Hari", "Happy"), "Hari, who looks happy"
        )


class ChangeQuestionTests(unittest.TestCase):
    def test_happy_to_sad_is_specific(self) -> None:
        self.assertIn(
            "happy earlier",
            (question_for_emotion_change("happy", "sad") or "").lower(),
        )

    def test_same_emotion_is_silent(self) -> None:
        self.assertIsNone(question_for_emotion_change("happy", "happy"))
        self.assertIsNone(question_for_emotion_change("happy", "uncertain"))


class ObserveTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_session_emotions("dev1")

    def tearDown(self) -> None:
        reset_session_emotions("dev1")

    def test_first_reading_sets_baseline(self) -> None:
        self.assertIsNone(observe_viewer_emotion("dev1", "Hari", "happy"))

    def test_asks_after_stable_change(self) -> None:
        observe_viewer_emotion("dev1", "Hari", "happy")
        self.assertIsNone(observe_viewer_emotion("dev1", "Hari", "sad"))
        ask = observe_viewer_emotion("dev1", "Hari", "sad")
        self.assertIsNotNone(ask)
        self.assertIn("happy earlier", (ask or "").lower())
        # Same transition is not asked twice.
        self.assertIsNone(observe_viewer_emotion("dev1", "Hari", "sad"))

    def test_ignores_guests_without_a_name(self) -> None:
        self.assertIsNone(observe_viewer_emotion("dev1", "", "sad"))


if __name__ == "__main__":
    unittest.main()

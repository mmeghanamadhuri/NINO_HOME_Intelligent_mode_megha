"""Tests for TTS text normalization (emoji / markdown stripping)."""

from __future__ import annotations

import unittest

from tts_service import _normalize_tts_text


class NormalizeTtsTextTests(unittest.TestCase):
    def test_strips_emojis(self) -> None:
        self.assertEqual(
            _normalize_tts_text("Hello 😊 there! 🎉"),
            "Hello there!",
        )

    def test_strips_emoji_shortcodes(self) -> None:
        self.assertEqual(
            _normalize_tts_text("Nice :smile: job :thumbs_up:"),
            "Nice job",
        )

    def test_strips_markdown_and_decoratives(self) -> None:
        self.assertEqual(
            _normalize_tts_text("**Bold** and *italic* with • bullets ✨"),
            "Bold and italic with bullets",
        )

    def test_normalizes_curly_quotes(self) -> None:
        self.assertEqual(
            _normalize_tts_text("It\u2019s fine"),
            "It's fine",
        )

    def test_preserves_plain_speech(self) -> None:
        self.assertEqual(
            _normalize_tts_text("Hi Yugandhar, the weather looks clear."),
            "Hi Yugandhar, the weather looks clear.",
        )

    def test_strips_nino_speaker_label_and_quotes(self) -> None:
        raw = (
            'NiNO: "Chakri! It\'s nice seeing you again! '
            'Let\'s dive into something you\'re curious about..." '
            "Do you want to chat more about Chakri?"
        )
        self.assertEqual(
            _normalize_tts_text(raw),
            "Chakri! It's nice seeing you again! "
            "Let's dive into something you're curious about... "
            "Do you want to chat more about Chakri?",
        )

    def test_strips_nino_says_label(self) -> None:
        self.assertEqual(
            _normalize_tts_text('NiNO says: "Hello Chakri!"'),
            "Hello Chakri!",
        )

    def test_leaves_nino_in_sentence(self) -> None:
        self.assertEqual(
            _normalize_tts_text("I'm NiNO, your home assistant."),
            "I'm NiNO, your home assistant.",
        )

    def test_strips_placeholders_and_latex(self) -> None:
        self.assertNotIn(
            "Insert",
            _normalize_tts_text(
                "Let's add:\n1. First number: [Insert the first number]\n"
                r"\[ 222 \times 18 = ? \]"
            ),
        )
        clean = _normalize_tts_text(
            "Let's add: [Insert the first number] and [Insert the second number]"
        )
        self.assertNotIn("[", clean)
        self.assertIn("Let's add:", clean)


if __name__ == "__main__":
    unittest.main()

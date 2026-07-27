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


if __name__ == "__main__":
    unittest.main()

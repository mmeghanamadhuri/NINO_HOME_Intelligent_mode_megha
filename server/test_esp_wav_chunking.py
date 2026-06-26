"""Tests for generic ESP WAV text chunking."""

from __future__ import annotations

import unittest

from esp_wav_chunking import chunk_text_for_esp_limit, split_spoken_sentences

_MAX = 100_000


def _mock_measure(text: str) -> int:
    """~24 KB per word — similar to espeak @ rate 135 after resample."""
    words = max(1, len(text.split()))
    return words * 24_000


class EspWavChunkingTests(unittest.TestCase):
    def test_split_sentences(self) -> None:
        parts = split_spoken_sentences(
            "Hi there! Yesterday we talked. Want to continue?"
        )
        self.assertEqual(len(parts), 3)
        self.assertTrue(parts[0].endswith("!"))

    def test_short_text_single_chunk(self) -> None:
        text = "Hello world."
        chunks = chunk_text_for_esp_limit(text, _mock_measure, _MAX)
        self.assertEqual(chunks, [text])

    def test_long_text_multiple_chunks(self) -> None:
        text = (
            "Hi RecognizedUser, good to see you! "
            "Yesterday we discussed topic alpha and their uses. "
            "Can you tell me the different types of topic alpha?"
        )
        chunks = chunk_text_for_esp_limit(text, _mock_measure, _MAX)
        self.assertGreaterEqual(len(chunks), 2)
        self.assertEqual(" ".join(chunks), text)
        for chunk in chunks:
            self.assertLessEqual(_mock_measure(chunk), _MAX)

    def test_prefers_sentence_boundaries(self) -> None:
        def measure(text: str) -> int:
            return len(text) * 1_000

        text = "First sentence here. Second sentence here. Third one?"
        chunks = chunk_text_for_esp_limit(text, measure, 25_000)
        self.assertGreaterEqual(len(chunks), 2)
        self.assertEqual(" ".join(chunks), text)
        for chunk in chunks:
            self.assertLessEqual(measure(chunk), 25_000)


if __name__ == "__main__":
    unittest.main()
